"""Bounded table evidence views and offline acquisition planning; never fetches pages."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from handlers.common import as_dict, as_list, as_text

REACQUIRE_REASONS = {"uncaptured", "fingerprint_mismatch", "evidence_unavailable"}
TABLE_PREVIEW_CHARS = 48000


def _reason(value: Any) -> str:
    text = as_text(value)
    return text if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", text) else "unknown"


def evidence_view(sample: dict[str, Any], budget: int) -> dict[str, Any]:
    """Readable previews only: private replay receipts never enter an agent prompt."""
    result: dict[str, Any] = {}
    receipt = as_dict(sample.get("table_evidence"))
    if receipt:
        result["table_evidence"] = {
            "retained": receipt.get("retained") is True,
            "reason": _reason(receipt.get("reason")),
        }
    for key in ("table_fields_omitted", "extracted_preview_omitted"):
        names = [
            as_text(name)
            for name in as_list(sample.get(key))[:50]
            if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", as_text(name))
        ]
        if names:
            result[key] = names
    previews = []
    remaining = max(0, budget)
    captures = as_list(sample.get("table_captures"))
    for raw in captures[:55]:
        row = as_dict(raw)
        key = as_text(row.get("source_key"))
        if not re.fullmatch(r"[a-f0-9]{64}", key) or remaining < 700:
            continue
        preview: dict[str, Any] = {
            "source_key": key,
            "complete": row.get("complete") is True,
            "pages": row.get("pages")
            if type(row.get("pages")) is int and 0 <= row["pages"] <= 64
            else 0,
            "bytes": row.get("bytes")
            if type(row.get("bytes")) is int and 0 <= row["bytes"] <= 4194304
            else 0,
        }
        preview["reason"] = _reason(row.get("reason"))
        cells = as_list(row.get("rows_preview") if "rows_preview" in row else row.get("rows"))
        preview["row_count"] = (
            min(10000, max(len(cells), row.get("row_count", 0)))
            if type(row.get("row_count", 0)) is int
            else len(cells)
        )
        preview["column_count"] = (
            min(64, max(0, row["column_count"]))
            if type(row.get("column_count")) is int
            else min(
                64,
                max(
                    [
                        len(
                            as_list(
                                row.get("headers_preview")
                                if "headers_preview" in row
                                else row.get("headers")
                            )
                        ),
                        *[len(as_list(cells_row)) for cells_row in cells[:20]],
                    ]
                ),
            )
        )
        raw_headers = as_list(
            row.get("headers_preview") if "headers_preview" in row else row.get("headers")
        )
        cells_shortened = len(raw_headers) > 16 or any(
            len(as_text(cell)) > 256 for cell in raw_headers[:16]
        )
        preview["headers_preview"] = [
            as_text(cell)[:256]
            for cell in as_list(
                row.get("headers_preview") if "headers_preview" in row else row.get("headers")
            )[:16]
        ]
        preview_rows: list[list[str]] = []
        preview["rows_preview"] = preview_rows
        for cells_row in cells[:3]:
            raw_cells = as_list(cells_row)
            cells_shortened = (
                cells_shortened
                or len(raw_cells) > 16
                or any(len(str(cell)) > 256 for cell in raw_cells[:16])
            )
            values = [str(cell)[:256] for cell in raw_cells[:16]]
            cost = len(json.dumps(values, ensure_ascii=False))
            if cost > min(4000, remaining - 700):
                break
            preview_rows.append(values)
            remaining -= cost
        preview["rows_shortened"] = (
            cells_shortened
            or row.get("rows_shortened") is True
            or row.get("preview_truncated") is True
            or len(preview_rows) < preview["row_count"]
        )
        preview["preview_truncated"] = preview["rows_shortened"]
        cost = len(json.dumps({k: v for k, v in preview.items() if k != "rows_preview"}))
        if cost > remaining:
            break
        remaining -= cost
        previews.append(preview)
    if previews:
        result["table_captures"] = previews
    if len(previews) < len(captures) or sample.get("table_captures_shortened"):
        result["table_captures_shortened"] = True
    return result


def table_feedback(mappings: list[dict], answer: dict, sample_ids: list[str]) -> dict[str, Any]:
    """Use only service-tested samples and exact, service-validated source objects."""
    fields = {
        as_text(mapping.get("template_id")): {
            as_text(field.get("name")): [
                source
                for source in as_list(field.get("sources"))
                if as_dict(source).get("kind") == "browser_table"
            ]
            for field in (as_dict(row) for row in as_list(mapping.get("fields")))
            if any(
                as_dict(source).get("kind") == "browser_table"
                for source in as_list(field.get("sources"))
            )
        }
        for mapping in mappings
    }
    failures, urls, sources = [], [], []
    seen_sources: set[str] = set()
    for template in fields.values():
        for requested in template.values():
            for source in requested:
                fingerprint_json = json.dumps(source, sort_keys=True, separators=(",", ":"))
                if fingerprint_json not in seen_sources:
                    seen_sources.add(fingerprint_json)
                    sources.append(source)
    eligible = True
    served, seen_fields = set(), set()
    wanted = set(sample_ids)
    for raw in as_list(answer.get("samples")):
        sample = as_dict(raw)
        sid = as_text(sample.get("sample_id"))
        if sid not in wanted:
            continue
        if sid in served:
            eligible = False
        served.add(sid)
        url = as_text(sample.get("url"))
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            eligible = False
        elif url not in urls:
            urls.append(url)
        for name, requested in fields.get(as_text(sample.get("template_id")), {}).items():
            seen_fields.add((as_text(sample.get("template_id")), name))
            coverage = as_dict(as_dict(sample.get("table_coverage")).get(name))
            if (
                coverage.get("complete") is True
                and coverage.get("reason") == "finished"
                and type(coverage.get("pages")) is int
                and coverage["pages"] >= 1
            ):
                continue
            reason = as_text(coverage.get("reason"), "uncaptured")[:64]
            if coverage.get("complete") is True:
                reason = "malformed_capture"
            failures.append(
                {
                    "kind": "table_unverified",
                    "sample_id": sid,
                    "field": name,
                    "message": f"Table {name} on sample {sid} remains unverified ({reason}); complete guarded capture evidence is required, including for an optional table.",
                }
            )
            eligible = eligible and reason in REACQUIRE_REASONS

    for template_id, names in fields.items():
        for name in names:
            if (template_id, name) not in seen_fields:
                failures.append(
                    {
                        "kind": "table_unverified",
                        "field": name,
                        "message": f"Table {template_id}.{name} has no matched offline sample evidence.",
                    }
                )
                eligible = False
    if sources and wanted - served:
        failures.append(
            {
                "kind": "table_unverified",
                "message": "Offline table verification omitted requested sample IDs; the candidate remains unverified.",
            }
        )
        eligible = False
    eligible = (
        bool(failures)
        and eligible
        and served == wanted
        and 0 < len(urls) <= len(wanted)
        and 0 < len(sources) <= 5
    )
    return {
        "table_failures": failures,
        "table_resample_needed": eligible,
        "table_sources": sources if eligible else [],
        "table_sample_urls": urls if eligible else [],
    }


# Mirrors the service variant_field_specs defaults only for the new table rule.
DEFAULT_VARIANT_FIELD_NAMES = {
    "price",
    "currency",
    "sku",
    "barcode",
    "gtin",
    "availability",
    "weight",
    "description",
    "image",
    "image_url",
}


def conflicting_variant_table_fields(mapping: dict) -> set[str]:
    block = as_dict(mapping.get("variants"))
    if not block:
        return set()
    explicit = as_list(block.get("fields"))
    variant_names = (
        {as_text(as_dict(field).get("name")) for field in explicit}
        if explicit
        else DEFAULT_VARIANT_FIELD_NAMES
    )
    return {
        as_text(field.get("name"))
        for field in (as_dict(row) for row in as_list(mapping.get("fields")))
        if as_text(field.get("name")) in variant_names
        and any(
            as_dict(source).get("kind") == "browser_table"
            for source in as_list(field.get("sources"))
        )
    }


def collect_table_obligations(mappings: list[dict], previous: list) -> list[dict[str, Any]]:
    """Preserve table meaning and known pagination; never latch ambiguous proposals."""
    obligations: dict[tuple[str, str], dict[str, Any]] = {}

    def retain(template: str, field: str, paginated: bool) -> None:
        if not template or not field:
            return
        key = (template, field)
        item = obligations.setdefault(key, {"template_id": template, "field": field})
        if paginated:
            item["requires_pagination"] = True

    for raw in previous[:2500]:
        row = as_dict(raw)
        retain(
            as_text(row.get("template_id"))[:64],
            as_text(row.get("field"))[:64],
            row.get("requires_pagination") is True,
        )
    for mapping in mappings[:50]:
        ambiguous = conflicting_variant_table_fields(mapping)
        for field in (as_dict(row) for row in as_list(mapping.get("fields"))[:50]):
            sources = [
                as_dict(source)
                for source in as_list(field.get("sources"))
                if as_dict(source).get("kind") == "browser_table"
            ]
            if sources and as_text(field.get("name")) not in ambiguous:
                paginated = any(
                    as_text(source.get("next_selector"))
                    or as_text(source.get("current_page_selector"))
                    for source in sources
                )
                retain(
                    as_text(mapping.get("template_id")), as_text(field.get("name")), bool(paginated)
                )
    return list(obligations.values())


def table_preservation_failures(mappings: list[dict], obligations: list[dict]) -> list[dict]:
    current = {
        (row["template_id"], row["field"]) for row in collect_table_obligations(mappings, [])
    }
    paginated = {
        (as_text(mapping.get("template_id")), as_text(field.get("name")))
        for mapping in mappings
        for field in (as_dict(row) for row in as_list(mapping.get("fields")))
        if any(
            as_dict(source).get("kind") == "browser_table"
            and as_text(as_dict(source).get("next_selector"))
            and as_text(as_dict(source).get("current_page_selector"))
            for source in as_list(field.get("sources"))
        )
    }
    failures = []
    for item in obligations:
        key = (item["template_id"], item["field"])
        if key not in current:
            message = f"Paginated table {key[0]}.{key[1]} cannot be removed or replaced with first-page CSS/scalar data; preserve browser_table semantics and honest unverified coverage."
        elif item.get("requires_pagination") is True and key not in paginated:
            message = f"Known pagination for table {key[0]}.{key[1]} requires both next_selector and current_page_selector; repair selectors without removing pagination or claiming first-page data is complete."
        else:
            continue
        failures.append({"kind": "table_downgrade", "field": item["field"], "message": message})
    return failures


def fallback_feedback(warnings: list, summary: str, failures: list[dict]) -> dict:
    unavailable = [
        {"kind": "table_unavailable", "message": as_text(row.get("message"))[:600]}
        for row in failures
        if row.get("kind") in {"table_unverified", "table_downgrade"}
    ][:50]
    warnings = [*warnings, *unavailable]
    return {
        "fallback_warnings": warnings,
        "fallback_summary": summary
        + "".join(" Warning: " + row["message"] for row in unavailable[:10]),
    }


def extracted_view(sample: dict[str, Any], budget: int) -> dict[str, Any]:
    """Preserve known non-table values; never transport full generated tables."""
    extracted = as_dict(sample.get("extracted"))
    if not sample.get("table_captures") and not sample.get("table_evidence"):
        return {"extracted": extracted or None}
    omitted = {as_text(name) for name in as_list(sample.get("table_fields_omitted"))[:50]}
    modern = "table_fields_omitted" in sample or any(
        "rows_preview" in as_dict(row) for row in as_list(sample.get("table_captures"))
    )
    if not modern:
        return {"extracted": None}  # Older captures may duplicate full HTML here.
    known, shortened = {}, []
    remaining = max(0, budget)
    for name, value in extracted.items():
        if name in omitted:
            continue
        if isinstance(value, str) and len(value) > remaining:
            shortened.append(name)
            continue
        cost = len(json.dumps({name: value}, ensure_ascii=False))
        if cost > remaining:
            shortened.append(name)
            continue
        known[name] = value
        remaining -= cost
    return {
        "extracted": known or None,
        **({"extracted_preview_omitted": shortened[:50]} if shortened else {}),
    }
