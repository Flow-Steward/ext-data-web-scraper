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
        cells = as_list(row.get("rows"))
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
                        len(as_list(row.get("headers"))),
                        *[len(as_list(cells_row)) for cells_row in cells[:20]],
                    ]
                ),
            )
        )
        preview["headers"] = [as_text(cell)[:200] for cell in as_list(row.get("headers"))[:64]]
        preview_rows: list[list[str]] = []
        preview["rows"] = preview_rows
        for cells_row in cells[:20]:
            values = [str(cell)[:500] for cell in as_list(cells_row)[:64]]
            cost = len(json.dumps(values, ensure_ascii=False))
            if cost > min(4000, remaining - 700):
                break
            preview_rows.append(values)
            remaining -= cost
        preview["rows_shortened"] = (
            row.get("rows_shortened") is True
            or row.get("preview_truncated") is True
            or len(preview_rows) < preview["row_count"]
        )
        preview["preview_truncated"] = preview["rows_shortened"]
        cost = len(json.dumps({k: v for k, v in preview.items() if k != "rows"}))
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


def collect_table_obligations(mappings: list[dict], previous: list) -> list[dict[str, str]]:
    """Keep paginated-table meaning sticky across the entire authoring loop."""
    obligations = []
    for row in previous[:2500]:
        row = as_dict(row)
        if row.get("template_id") and row.get("field"):
            item = {
                "template_id": as_text(row["template_id"])[:64],
                "field": as_text(row["field"])[:64],
            }
            if item not in obligations:
                obligations.append(item)
    for mapping in mappings[:50]:
        for field in (as_dict(row) for row in as_list(mapping.get("fields"))[:50]):
            if any(
                as_dict(source).get("kind") == "browser_table"
                for source in as_list(field.get("sources"))
            ):
                item = {
                    "template_id": as_text(mapping.get("template_id")),
                    "field": as_text(field.get("name")),
                }
                if item not in obligations:
                    obligations.append(item)
    return obligations


def table_preservation_failures(mappings: list[dict], obligations: list[dict]) -> list[dict]:
    current = collect_table_obligations(mappings, [])
    return [
        {
            "kind": "table_downgrade",
            "field": item["field"],
            "message": f"Paginated table {item['template_id']}.{item['field']} cannot be removed or replaced with first-page CSS/scalar data; preserve browser_table semantics and honest unverified coverage.",
        }
        for item in obligations
        if item not in current
    ]


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
