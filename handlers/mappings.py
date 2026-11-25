"""Samples, mapping tests, mapping versions, mapping requests (contract section 6)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from handlers import protection as protection_text
from handlers.common import as_dict, as_list, as_text, bounded, count, is_unset
from handlers.context import ActionContext
from handlers.errors import TEST_CODE_TEXT, ScraperError, describe_field_problems
from handlers.runs import _config, get_run, slow_post
from handlers.table_samples import (
    TABLE_PREVIEW_CHARS,
    evidence_view,
    extracted_view,
    table_feedback,
    collect_table_obligations,
    table_preservation_failures,
    fallback_feedback,
)

EXPORT_FORMAT = "flowsteward.web-scraper.mappings.v1"
SAMPLE_HTML_FOR_AGENT = 65536
#: Distilled HTML of all samples together that goes into one agent prompt and into
#: workflow state (20 samples x 64 KiB would be 1.3 MB). Each sample gets an equal
#: share, at most SAMPLE_HTML_FOR_AGENT.
SAMPLES_HTML_FOR_AGENT = 393216
MAX_REFINEMENTS = 5


def _mappings_input(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ScraperError(
                "mapping_invalid", "The mapping is not valid JSON.", no_external_effect=True
            ) from exc
    if isinstance(value, dict):
        # One mapping, or an object that wraps the list.
        if isinstance(value.get("mappings"), list):
            value = value["mappings"]
        else:
            value = [value]
    rows = [as_dict(item) for item in as_list(value) if isinstance(item, dict)]
    return rows


_FENCE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def candidate_mappings(value: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """The agent's candidate and what is wrong with its form, phrased for a refinement.

    The agent returns the list as JSON text (`mappings_json`): a provider's strict
    structured output cannot carry free-form objects (the platform turns an object
    without declared properties into one that only `{}` satisfies,
    common_runtime_structured_utils._prepare_openai_json_schema). A list given as
    data (a test, a person on the canvas) is accepted as well.
    """
    if isinstance(value, str):
        text = value.strip()
        fenced = _FENCE.match(text)
        if fenced:
            text = fenced.group(1).strip()
        if not text:
            return [], [
                "The answer had no mapping: put the JSON list of mappings in mappings_json."
            ]
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            return [], [
                f"mappings_json is not valid JSON ({exc.msg} at line {exc.lineno}, column "
                f"{exc.colno}). Return only a JSON list of mapping objects, nothing around it."
            ]
    if isinstance(value, dict):
        value = value["mappings"] if isinstance(value.get("mappings"), list) else [value]
    if not isinstance(value, list):
        return [], [
            "mappings_json must be a JSON list of mapping objects, one per page template; it "
            f"was a {type(value).__name__}."
        ]
    rows = [item for item in value if isinstance(item, dict)]
    if len(rows) != len(value):
        return rows, ["Every item of the mappings_json list must be a mapping object."]
    return rows, []


def structural_problems(mappings: list[dict[str, Any]]) -> list[str]:
    """Shape checks that need no service, phrased for the mapping agent.

    The service validates against `mapping.schema.json` itself; this catches the
    obvious mistakes first so a refinement gets a precise message.
    """
    problems: list[str] = []
    if not mappings:
        return ["The candidate contains no mapping: return at least one template."]
    seen: set[str] = set()
    for index, mapping in enumerate(mappings):
        label = as_text(mapping.get("template_id")) or f"mapping #{index + 1}"
        if mapping.get("schema_version") != 1:
            problems.append(f"{label}: schema_version must be 1")
        template_id = as_text(mapping.get("template_id"))
        if not template_id:
            problems.append(f"{label}: template_id is missing")
        elif template_id in seen:
            problems.append(f"{label}: the template_id is used twice")
        seen.add(template_id)
        match = as_dict(mapping.get("match"))
        if not match.get("url_patterns") and not as_text(match.get("selector")):
            problems.append(f"{label}: match needs url_patterns or a selector")
        fields = as_list(mapping.get("fields"))
        if not fields:
            problems.append(f"{label}: fields is empty")
        names: set[str] = set()
        for field in fields:
            row = as_dict(field)
            name = as_text(row.get("name"))
            if not name:
                problems.append(f"{label}: a field has no name")
                continue
            if name in names:
                problems.append(f"{label}: field '{name}' appears twice")
            names.add(name)
            sources = as_list(row.get("sources"))
            if not 1 <= len(sources) <= 5:
                problems.append(f"{label}.{name}: needs between 1 and 5 sources")
            for source in sources:
                kind = as_text(as_dict(source).get("kind"))
                if kind not in {
                    "jsonld",
                    "microdata",
                    "css",
                    "meta",
                    "spec_table",
                    "browser_table",
                }:
                    problems.append(
                        f"{label}.{name}: source kind must be jsonld, microdata, css, meta or "
                        "spec_table or browser_table"
                    )
                if kind == "spec_table" and not all(
                    as_text(as_dict(source).get(key))
                    for key in ("row_selector", "key_selector", "value_selector")
                ):
                    problems.append(
                        f"{label}.{name}: a spec_table source needs row_selector, key_selector "
                        "and value_selector"
                    )
        problems += _variant_problems(label, mapping.get("variants"))
    return problems


VARIANT_SOURCE_KINDS = {"embedded_json", "shop_json", "jsonld", "microdata"}


def _variant_problems(label: str, variants: Any) -> list[str]:
    """The variants block's shape (mapping.schema.json `$defs/variants`), for the agent."""
    if variants is None:
        return []
    block = as_dict(variants)
    if not block:
        return [f"{label}: variants must be an object with sources (and fields), or be left out"]
    problems: list[str] = []
    sources = as_list(block.get("sources"))
    if not 1 <= len(sources) <= 5:
        problems.append(
            f"{label}: variants.sources needs 1 to 5 sources - copy a sample's "
            "variant_sources[].source as it is"
        )
    for index, source in enumerate(as_dict(x) for x in sources):
        kind = as_text(source.get("kind"))
        if kind not in VARIANT_SOURCE_KINDS:
            problems.append(
                f"{label}: variants.sources[{index}].kind must be embedded_json, shop_json, "
                "jsonld or microdata"
            )
    for field in (as_dict(x) for x in as_list(block.get("fields"))):
        name = as_text(field.get("name")) or "a variant field"
        if not field.get("name"):
            problems.append(f"{label}: a variants.fields entry has no name")
        if sum(bool(field.get(key)) for key in ("path", "option", "selector_template")) != 1:
            problems.append(
                f"{label}: variants field {name} needs exactly one of path (price, sku, image, "
                "availability, weight, raw.<path>), option (the option's name), or selector_template"
            )
    return problems


def _css(text: str) -> str:
    """A selector compared as text: whitespace collapsed, none around combinators."""
    return re.sub(r"\s*([,>+~])\s*", r"\1", re.sub(r"\s+", " ", as_text(text))).strip()


def selector_alternatives(selector: str) -> list[str]:
    """The parts of a selector list (`a, b`), split at commas outside brackets and quotes."""
    parts: list[str] = []
    depth, quote, current = 0, "", ""
    for char in as_text(selector):
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return [_css(part) for part in parts if _css(part)]


def target_selector_warnings(
    mappings: list[dict[str, Any]], target_page_selector: str
) -> list[dict[str, Any]]:
    """A30: a template whose match.selector does not carry the operator's product page
    selector (itself, or containing it; for a list `a, b`: one part). A warning, not a
    failure: the service skips a page without the selector even when a template matches,
    so a stored mapping need not be rebuilt when a selector is added."""
    wanted = selector_alternatives(target_page_selector)
    if not wanted:
        return []
    warnings: list[dict[str, Any]] = []
    for index, mapping in enumerate(mappings):
        label = as_text(mapping.get("template_id")) or f"mapping #{index + 1}"
        selector = as_text(as_dict(mapping.get("match")).get("selector"))
        if any(part in _css(selector) for part in wanted):
            continue
        shown = f"it is '{bounded(selector, 120)}'" if selector else "it has none"
        warnings.append(
            {
                "kind": "target_page_selector",
                "template_id": as_text(mapping.get("template_id")),
                "message": f"{label}: match.selector does not include the product page selector "
                f"'{bounded(target_page_selector, 200)}' ({shown}); the service still skips "
                "pages without it.",
            }
        )
    return warnings


_MAPPING_INDEX = re.compile(r"^mappings\[(\d+)\]")


def empty_field_warnings(
    answer: dict[str, Any], mappings: list[dict[str, Any]], templates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """A26: fields no matched sample filled. The test may still pass on its required
    fields, so the operator must see it: the service's `fields_never_filled` warning
    (its message names the fields, the sample count and the required fields), else
    the extension's own words from `templates[].empty_fields`."""
    out: list[dict[str, Any]] = []
    for row in (as_dict(w) for w in as_list(answer.get("warnings"))):
        if as_text(row.get("code")) != "fields_never_filled":
            continue
        found = _MAPPING_INDEX.match(as_text(row.get("field")))
        index = int(found.group(1)) if found else -1
        template_id = (
            as_text(as_dict(mappings[index]).get("template_id"))
            if 0 <= index < len(mappings)
            else ""
        )
        out.append(
            {
                "kind": "fields_never_filled",
                "template_id": template_id,
                "message": bounded(as_text(row.get("message")), 600).rstrip(".")
                + ". The crawl would leave these fields empty; if the samples are not product "
                "pages, set the product page selector.",
            }
        )
    named = {row["template_id"] for row in out}
    for template in templates:
        empty = as_list(template.get("empty_fields"))
        if not empty or template["template_id"] in named:
            continue
        out.append(
            {
                "kind": "fields_never_filled",
                "template_id": template["template_id"],
                "message": f"template {template['template_id']}: {', '.join(empty)} filled on "
                f"0 of {count(template.get('matched_samples'), 'matched sample')}; check the mapping's "
                "sources. The crawl would leave these fields empty; if the samples are not "
                "product pages, set the product page selector.",
            }
        )
    return out


def _variant_result(template: dict[str, Any]) -> dict[str, Any]:
    """The template's variant figures from the service's test (contract row 39)."""
    if "variant_count" not in template and not template.get("variant_fields"):
        return {}
    return {
        "variant_count": template.get("variant_count") or 0,
        "samples_with_variants": template.get("samples_with_variants") or 0,
        "variant_sources_used": as_dict(template.get("variant_sources_used")),
        "variant_fields": [
            {
                "name": as_text(row.get("name")),
                "filled": f"{row.get('filled_count')}/{row.get('variant_count')}",
                "fill_rate": row.get("fill_rate"),
                "examples": [_example(v) for v in as_list(row.get("example_values"))[:3]],
            }
            for row in (as_dict(r) for r in as_list(template.get("variant_fields")))
        ],
    }


def _variant_line(template: dict[str, Any]) -> str:
    """ "lens_cover: 6 variants on 2 of 2 samples (embedded_json 2); price 6/6, markings 6/6"."""
    if "variant_count" not in template:
        return ""
    used = ", ".join(
        f"{kind} {number}"
        for kind, number in template.get("variant_sources_used", {}).items()
        if kind != "none"
    )
    line = (
        f"{template['template_id']}: {count(template['variant_count'], 'variant')} on "
        f"{template['samples_with_variants']} of {count(template.get('matched_samples') or 0, 'sample')}"
        + (f" ({used})" if used else "")
    )
    fills = ", ".join(f"{row['name']} {row['filled']}" for row in template["variant_fields"])
    return line + (f"; {fills}" if fills else "")


def variant_warnings(templates: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    """Variant fields filled on too few variants, and a variants block that found none."""
    out: list[dict[str, Any]] = []
    for template in templates:
        if "variant_count" not in template:
            continue
        template_id = template["template_id"]
        if (
            template.get("matched_samples")
            and not template["variant_count"]
            and not template.get("samples_with_variants")
        ):
            out.append(
                {
                    "kind": "variants_not_found",
                    "template_id": template_id,
                    "message": f"template {template_id}: the variants block found no variant on "
                    f"{count(template.get('matched_samples'), 'matched sample')}; copy a source "
                    "from the samples' variant_sources, or leave the block out if the pages have "
                    "no variants.",
                }
            )
        if not template["variant_count"]:
            continue  # no emitted variants means there are no field values to measure
        for row in template["variant_fields"]:
            try:
                rate = float(row.get("fill_rate"))
            except (TypeError, ValueError):
                continue
            if rate < threshold:
                out.append(
                    {
                        "kind": "variant_fill",
                        "template_id": template_id,
                        "field": row["name"],
                        "message": f"template {template_id}: variant field {row['name']} filled on "
                        f"{row['filled']} variants; check its path or option name against the "
                        "samples' variant_sources example.",
                    }
                )
    return out


def variants_missing(
    answer: dict[str, Any], mappings: list[dict[str, Any]], with_variants: list[str]
) -> list[dict[str, Any]]:
    """A template that matched samples carrying variant data but has no variants block
    would give one record where the page has several: a failure, so the agent adds it."""
    wanted = set(with_variants)
    if not wanted:
        return []
    has_block = {
        as_text(m.get("template_id")) for m in mappings if as_dict(m.get("variants")).get("sources")
    }
    hits: dict[str, int] = {}
    for row in (as_dict(r) for r in as_list(answer.get("samples"))):
        template_id = as_text(row.get("template_id"))
        if template_id and template_id not in has_block and as_text(row.get("sample_id")) in wanted:
            hits[template_id] = hits.get(template_id, 0) + 1
    return [
        {
            "kind": "variants_missing",
            "template_id": template_id,
            "message": f"template {template_id}: {count(number, 'matched sample')} "
            f"{'carries' if number == 1 else 'carry'} variants (see variant_sources) but the "
            "mapping has no variants block; add one - copy a variant_sources[].source into "
            "variants.sources and map the options - so each variant becomes a record.",
        }
        for template_id, number in sorted(hits.items())
    ]


def _agent_samples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Samples for the agent, the distilled HTML of all of them within SAMPLES_HTML_FOR_AGENT."""
    share = min(SAMPLE_HTML_FOR_AGENT, SAMPLES_HTML_FOR_AGENT // max(len(rows), 1))
    return [_agent_sample(row, share, TABLE_PREVIEW_CHARS // max(len(rows), 1)) for row in rows]


def _agent_sample(
    sample: dict[str, Any], limit: int = SAMPLE_HTML_FOR_AGENT, table_budget: int = 4000
) -> dict[str, Any]:
    html = as_text(sample.get("distilled_html"))
    return {
        **evidence_view(sample, table_budget),
        "sample_id": as_text(sample.get("sample_id")),
        "url": as_text(sample.get("url")),
        "title": as_text(sample.get("title")),
        "template_hint": as_dict(sample.get("template_hint")),
        "jsonld": as_list(sample.get("jsonld")),
        "meta": as_dict(sample.get("meta")),
        "distilled_html": html[:limit],
        "distilled_html_truncated": bool(sample.get("distilled_html_truncated"))
        or len(html) > limit,
        **extracted_view(sample, table_budget),
        # A54 (contract row 63): shown / range / on_request / missing.
        "price_on_page": as_text(sample.get("price_on_page")) or None,
        # A54+ (contract row 70): the machine-readable price and a sale's old price.
        "price_details": as_dict(sample.get("price_details")) or None,
        # ROUND2 §2/§3: the distilled HTML drops scripts and attributes, so the
        # variant data and microdata the service found travel separately.
        "variant_sources": _variant_sources(sample),
        "variant_candidates": _variant_candidates(sample),
        "microdata": _bounded_list(as_list(sample.get("microdata")), MICRODATA_FOR_AGENT),
    }


#: Characters of one sample's variant sources / microdata in the agent's view.
VARIANT_SOURCES_FOR_AGENT = 12000
MICRODATA_FOR_AGENT = 8000


def _bounded_list(items: list[Any], limit: int) -> list[Any]:
    """Whole items while their JSON fits in `limit` characters."""
    out: list[Any] = []
    used = 0
    for item in items:
        size = len(json.dumps(item, ensure_ascii=False, default=str))
        if used + size > limit:
            break
        out.append(item)
        used += size
    return out


def _variant_sources(sample: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "source": as_dict(row.get("source")),
            "count": row.get("count"),
            "option_names": as_list(row.get("option_names")),
            "example": as_dict(row.get("example")),
            "raw_keys": as_list(row.get("raw_keys"))[:40],
        }
        for row in (as_dict(r) for r in as_list(sample.get("variant_sources")))
        if as_dict(row.get("source"))
    ]
    return _bounded_list(rows, VARIANT_SOURCES_FOR_AGENT)


def _variant_candidates(sample: dict[str, Any]) -> list[dict[str, Any]]:
    """Optional raw evidence stays distinct from confirmed variant sources."""
    rows = [
        {
            "source": as_dict(row.get("source")),
            "count": row.get("count"),
            "raw_examples": as_list(row.get("raw_examples"))[:2],
            "raw_paths": as_list(row.get("raw_paths"))[:40],
            "evidence": as_text(row.get("evidence"))[:1000],
            **(
                {
                    "parent_count": row.get("parent_count"),
                    "parent_examples": as_list(row.get("parent_examples"))[:2],
                    "context_paths": as_list(row.get("context_paths"))[:40],
                }
                if as_text(as_dict(row.get("source")).get("children_path"))
                else {}
            ),
        }
        for row in (as_dict(r) for r in as_list(sample.get("variant_candidates"))[:12])
        if as_dict(row.get("source"))
    ]
    return _bounded_list(rows, VARIANT_SOURCES_FOR_AGENT)


def variant_sample_ids(samples: list[dict[str, Any]]) -> list[str]:
    """Samples whose page carries variant data the service found."""
    return [
        as_text(row.get("sample_id"))
        for row in samples
        if any(as_dict(v).get("source") for v in as_list(row.get("variant_sources")))
    ]


def variant_summary(samples: list[dict[str, Any]]) -> str:
    """ "2 of 4 samples carry variants: embedded_json 3 (options Lens cover markings) …"."""
    lines = []
    for row in samples:
        found = [
            as_dict(v) for v in as_list(row.get("variant_sources")) if as_dict(v).get("source")
        ]
        if not found:
            continue
        parts = []
        for item in found:
            kind = as_text(as_dict(item.get("source")).get("kind"))
            options = ", ".join(as_text(o) for o in as_list(item.get("option_names")))
            parts.append(
                f"{kind} {count(item.get('count'), 'variant')}"
                + (f" (options {options})" if options else "")
            )
        lines.append(f"{bounded(as_text(row.get('url')), 120)}: " + "; ".join(parts))
    if not lines:
        return ""
    head = (
        f"{len(lines)} of {count(len(samples), 'sample')} carry variants (one record per variant)."
    )
    return head + "\n" + "\n".join(lines)


def fetch_samples(ctx: ActionContext) -> dict[str, Any]:
    profile = ctx.profile
    config = _config(ctx)
    body: dict[str, Any] = {
        "config": config,
        "profile": profile.contract_profile(with_webhook=False),
    }
    urls = [as_text(url) for url in as_list(ctx.input.get("urls")) if as_text(url)]
    if urls:
        body["urls"] = urls[:50]
    template_id = ctx.text("template_id")
    if template_id:
        body["template_id"] = template_id
    raw_table_sources = ctx.input.get("table_sources")
    if not is_unset(raw_table_sources) and not isinstance(raw_table_sources, list):
        raise ScraperError(
            "invalid_payload",
            "table_sources must be an array of browser_table source objects.",
            no_external_effect=True,
        )
    table_sources = as_list(raw_table_sources)
    if table_sources:
        body["table_sources"] = table_sources
    cause, after = _analysis_protection_hint(ctx)
    answer = as_dict(
        slow_post(ctx, "/v1/samples", body, what="the sample pages", cause=cause, after=after)
    )
    samples = [as_dict(row) for row in as_list(answer.get("samples"))]
    if (
        table_sources
        and urls
        and (len(samples) > len(urls) or {as_text(row.get("url")) for row in samples} != set(urls))
    ):
        raise ScraperError(
            "no_samples",
            "Table acquisition did not return every requested current sample URL; the candidate remains unverified.",
        )
    warnings = [as_dict(row) for row in as_list(answer.get("warnings"))]
    traces = as_list(answer.get("protection_trace"))
    stopped = as_dict(answer.get("stopped"))
    protection_summary = protection_text.summary(traces, warnings=warnings, stopped=stopped)
    if not samples:
        raw = {
            "warnings": warnings,
            "protection_trace": traces,
            "stopped": stopped or None,
            "protection_summary": protection_summary,
        }
        if protection_text.not_passed(traces, warnings=warnings, stopped=stopped):
            raise _protection_not_passed(traces, warnings, stopped, raw)
        raise _no_samples(answer, warnings, protection_summary, raw)
    jsonld_types = sorted(
        {
            ",".join(as_text(item) for item in as_list(node.get("@type")))
            if isinstance(node.get("@type"), list)
            else as_text(node.get("@type"))
            for sample in samples
            for node in (as_dict(n) for n in as_list(sample.get("jsonld")))
            if node.get("@type")
        }
    )
    return {
        "site": as_text(answer.get("site")),
        "sample_ids": [as_text(row.get("sample_id")) for row in samples],
        "samples": _agent_samples(samples),
        "sample_count": len(samples),
        "jsonld_types": jsonld_types,
        "warnings": warnings,
        "warning_text": "; ".join(describe_field_problems(warnings)),
        "protection_summary": protection_summary,
        "variant_sample_ids": variant_sample_ids(samples),
        "variant_summary": variant_summary(samples),
    }


MAX_EXPLANATION_LINES = 12


def service_explanation(answer: dict[str, Any], warnings: list[dict[str, Any]]) -> list[str]:
    """What the service says about an empty samples answer (A35): every warning's
    message in order, then `explanation.summary` (counts and the next hint in plain
    words) and each `explanation.hints[]` entry not already in the summary."""
    lines: list[str] = []
    explanation = answer.get("explanation")
    summary = as_text(explanation) if isinstance(explanation, str) else ""
    explanation = as_dict(explanation)
    summary = summary or as_text(explanation.get("summary"))
    for row in warnings:
        message = _without_summary(as_text(row.get("message")), summary)
        if message:
            lines.append(bounded(message, 600))
    if summary:
        lines.append(bounded(summary, 2000))
    for hint in (as_dict(h) for h in as_list(explanation.get("hints"))):
        message = as_text(hint.get("message"))
        value = as_text(hint.get("value"))
        if not message or message in summary:
            continue
        lines.append(
            f"Suggested: {bounded(message, 500)}"
            + (f" ({value})" if value and value not in message else "")
        )
    unique: list[str] = []
    for line in lines:
        if line not in unique:
            unique.append(line)
    extra = len(unique) - MAX_EXPLANATION_LINES
    shown = unique[:MAX_EXPLANATION_LINES]
    if extra > 0:
        shown.append(f"(+{count(extra, 'more line')} in the step's result.)")
    return shown


#: The service's `sample_count` warning: "found 0 of 4 target pages: <explanation.summary>".
_FOUND_OF = re.compile(r"^(found \d+ of \d+ target pages?): (.+)$", re.S)
#: The summary ends with the seconds the call took, measured again for each copy.
_SECONDS = re.compile(r"\b\d+ s\b")


def _without_summary(message: str, summary: str) -> str:
    """A71: the `sample_count` warning repeats `explanation.summary` after its count; keep
    the count ("found 0 of 4 target pages.") so the summary that follows is said once."""
    match = _FOUND_OF.match(message)
    if not match or not summary:
        return message
    rest, whole = (_SECONDS.sub("", text).strip() for text in (match.group(2), summary))
    shared = min(len(rest), len(whole), 300)
    if shared < 20 or rest[:shared] != whole[:shared]:
        return message
    return match.group(1) + "."


def _no_samples(
    answer: dict[str, Any],
    warnings: list[dict[str, Any]],
    protection_summary: str,
    raw: dict[str, Any],
) -> ScraperError:
    explained = service_explanation(answer, warnings)
    message = "No target page could be sampled."
    if explained:
        message += " What the scraper service tried:\n" + "\n".join(explained) + "\n"
    else:
        message += " "
    message += (
        "Check the site URL, the product page selector, Extract only URLs matching or Discovery (crawl "
        "from a category page), or whether the site blocks the scraper (run 'Analyze site')."
    )
    if protection_summary:
        message += f"\n{protection_summary}"
    if answer.get("explanation") not in (None, "", {}):
        raw["explanation"] = answer.get("explanation")
    return ScraperError("no_samples", message, result=raw)


def _analysis_protection_hint(ctx: ActionContext) -> tuple[str, str]:
    """What a timeout says when 'Analyze site' met the site's protection (A23).

    The template maps the analysis' `protection_summary` and `protection_not_passed`
    into this step; in url_list mode there was no analysis and both are empty.
    """
    seen = as_text(ctx.input.get("analysis_protection_summary"))
    if not seen:
        return "", ""
    failed = ctx.input.get("analysis_protection_not_passed") is True
    cause = (
        "The site's protection is the likely cause: the analysis met a protection check "
        + ("and did not pass it" if failed else "and needed the protection route to pass it")
        + ", and the service runs that route (wait, click, solver) on every sample page."
    )
    after = protection_text.advice() + "\nWhat the analysis saw:\n"
    return cause, after + protection_text.error_paragraphs(seen)


def _protection_not_passed(
    traces: list[Any], warnings: list[dict[str, Any]], stopped: dict[str, Any], raw: dict[str, Any]
) -> ScraperError:
    """No sample because the site's protection was not passed: say so, and what to try."""
    host = as_text(stopped.get("host"))
    if not host:
        first = next(
            (as_dict(t) for t in traces if not protection_text.trace_passed(as_dict(t))), {}
        )
        host = protection_text.display_url(first.get("url")).split("/", 1)[0]
    shown = protection_text.paragraphs(
        traces, warnings=warnings, stopped=stopped, limit=protection_text.MAX_ERROR_PARAGRAPHS
    )
    message = (
        f"The site's protection check was not passed{f' on {host}' if host else ''}, so no "
        "sample page could be fetched. "
        + protection_text.advice(traces)
        + ("\nWhat happened:\n" + "\n".join(shown) if shown else "")
    )
    return ScraperError("protection_not_passed", message, result=raw, retryable=False)


def _number(value: Any, default: float | int) -> float | int:
    if is_unset(value) or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return int(number) if number.is_integer() else number


def _as_int(value: Any) -> int:
    if value is None or value == "" or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _example(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return bounded(text, 160)


def test_mapping(ctx: ActionContext) -> dict[str, Any]:
    profile = ctx.profile
    site = ctx.require_text("site", "Site")
    raw_candidate = ctx.input.get("mappings_json")
    if raw_candidate is None:
        raw_candidate = ctx.input.get("mappings") or ctx.input.get("mapping")
    mappings, format_problems = candidate_mappings(raw_candidate)
    sample_ids = [as_text(item) for item in as_list(ctx.input.get("sample_ids")) if as_text(item)]
    if not sample_ids:
        raise ScraperError(
            "invalid_payload",
            "No sample ids were given. Map the 'sample_ids' of 'Fetch samples' or of the "
            "mapping request into this step.",
            no_external_effect=True,
        )
    threshold = _number(ctx.input.get("required_fill_threshold"), 0.9)
    attempt = int(_number(ctx.input.get("attempt"), 0)) + 1
    # The rework edge of impl_build_mapping allows 5 returns (rework_limit: 5); a
    # larger value would make the platform pause the child for a person instead of
    # returning passed: false. The contract caps it at 5 too (job_config.schema.json).
    max_refinements = min(
        max(int(_number(ctx.input.get("max_refinements"), 2)), 0), MAX_REFINEMENTS
    )
    can_refine = attempt < 1 + max_refinements
    obligations = collect_table_obligations(
        mappings,
        collect_table_obligations(
            candidate_mappings(ctx.input.get("mappings"))[0],
            as_list(ctx.input.get("table_obligations")),
        ),
    )
    prior_warnings = as_list(ctx.input.get("last_passing_warnings"))
    prior_summary = ctx.text("last_passing_summary")
    base = {
        "table_obligations": obligations,
        **fallback_feedback(prior_warnings, prior_summary, []),
        "mappings": mappings,
        "attempt": attempt,
        "attempts": attempt,
        "max_refinements": max_refinements,
        "can_refine": can_refine,
        "sample_ids": sample_ids,
        "required_fill_threshold": threshold,
        "sample_results": [],
        "variant_sample_ids": as_list(ctx.input.get("variant_sample_ids")),
        "table_resample_needed": False,
        "table_sources": [],
        "table_sample_urls": [],
    }
    selector_warnings = target_selector_warnings(mappings, ctx.text("target_page_selector"))
    base["warnings"] = selector_warnings
    problems = format_problems or structural_problems(mappings)
    downgrades = table_preservation_failures(mappings, obligations)
    table_shape_failures = (
        [
            {
                "kind": "table_unverified",
                "message": "The paginated-table candidate has structural problems and remains unverified.",
            }
        ]
        if obligations and problems
        else []
    )
    if problems or downgrades:
        return {
            **base,
            **fallback_feedback(prior_warnings, prior_summary, downgrades + table_shape_failures),
            "passed": False,
            "templates": [],
            "field_results": [],
            "failures": [
                {"kind": "format" if format_problems else "structure", "message": text}
                for text in problems
            ]
            + downgrades,
            "unmatched_sample_ids": [],
            "summary": "The mapping has structural problems: "
            + "; ".join(problems + [row["message"] for row in downgrades]),
        }
    body: dict[str, Any] = {
        "site": site,
        "mappings": mappings,
        "sample_ids": sample_ids[:50],
        "required_fill_threshold": threshold,
    }
    required_fields = [as_text(f) for f in as_list(ctx.input.get("required_fields")) if as_text(f)]
    if required_fields:
        body["required_fields"] = required_fields
    extraction_mode = ctx.text("extraction_mode")
    if extraction_mode:
        body["extraction_mode"] = extraction_mode
    if ctx.input.get("require_all_samples_matched") is False:
        body["require_all_samples_matched"] = False
    try:
        answer = as_dict(profile.client().post("/v1/mappings/test", body))
    except ScraperError as exc:
        if exc.service_code != "validation_failed":
            raise
        field_errors = as_list(exc.result.get("field_errors"))
        schema_failures: list[dict[str, Any]] = [
            {
                "kind": "schema",
                "path": as_text(as_dict(row).get("field")),
                "message": as_text(as_dict(row).get("message"))
                or as_text(as_dict(row).get("code")),
            }
            for row in field_errors
        ] or [{"kind": "schema", "message": exc.message}]
        table_schema_failures = (
            [
                {
                    "kind": "table_unverified",
                    "message": "The service refused the paginated-table candidate shape; table coverage remains unverified.",
                }
            ]
            if obligations
            else []
        )
        return {
            **base,
            **fallback_feedback(prior_warnings, prior_summary, table_schema_failures),
            "passed": False,
            "templates": [],
            "field_results": [],
            "failures": schema_failures + table_schema_failures,
            "unmatched_sample_ids": [],
            "summary": "The service refused the mapping's shape: "
            + "; ".join(
                f"{f.get('path', '')} {f['message']}".strip() for f in schema_failures[:10]
            ),
        }
    templates_out: list[dict[str, Any]] = []
    field_results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for template in (as_dict(t) for t in as_list(answer.get("templates"))):
        template_id = as_text(template.get("template_id"))
        gaps = mapping_gap_failures(template_id, template)
        templates_out.append(
            {
                "template_id": template_id,
                "matched_samples": template.get("matched_samples"),
                "passed": bool(template.get("passed")) and not gaps,
                "empty_fields": [as_text(f) for f in as_list(template.get("empty_fields"))],
                "gaps": [gap_line(row) for row in gaps],
                **_variant_result(template),
            }
        )
        failures += gaps
        for field in (as_dict(f) for f in as_list(template.get("fields"))):
            row: dict[str, Any] = {
                "template_id": template_id,
                "name": as_text(field.get("name")),
                "type": as_text(field.get("type")),
                "required": bool(field.get("required")),
                "fill_rate": field.get("fill_rate"),
                "filled": f"{field.get('filled_count')}/{field.get('sample_count')}",
                "passed": bool(field.get("passed")),
                "examples": [_example(v) for v in as_list(field.get("example_values"))[:3]],
                "source_hits": as_list(field.get("source_hits")),
                "errors": [bounded(as_text(e), 200) for e in as_list(field.get("errors"))[:5]],
            }
            field_results.append(row)
            if not row["passed"]:
                failures.append(
                    {
                        "kind": "fill",
                        "template_id": template_id,
                        "field": row["name"],
                        "required": row["required"],
                        "fill_rate": row["fill_rate"],
                        "threshold": answer.get("required_fill_threshold", threshold),
                        "source_hits": row["source_hits"],
                        "errors": row["errors"],
                    }
                )
        if not template.get("matched_samples"):
            failures.append(
                {
                    "kind": "no_match",
                    "template_id": template_id,
                    "message": "This template matched none of the samples; check its match rule.",
                }
            )
    unmatched = [as_text(x) for x in as_list(answer.get("unmatched_sample_ids"))]
    if unmatched:
        failures.append(
            {
                "kind": "unmatched_samples",
                "sample_ids": unmatched,
                "message": "No template matches these samples: add a template or widen a match rule.",
            }
        )
    samples_out = [_sample_result(as_dict(s)) for s in as_list(answer.get("samples"))]
    sample_failures: list[dict[str, Any]] = []
    mappings_by_template = {mapping["template_id"]: mapping for mapping in mappings}
    for sample in samples_out:
        template_id = as_text(sample.get("template_id"))
        mapping = mappings_by_template.get(template_id, {})
        inclusion_fields = {condition["field"] for condition in mapping.get("record_filter") or []}
        for name in dict.fromkeys(as_text(field) for field in sample["missing_required"]):
            if not name:
                continue
            reason = (
                "the value needed by the inclusion filter is missing, so inclusion cannot be decided"
                if name in inclusion_fields
                else "the required value was not extracted"
            )
            hint = (
                "; for variant fields, use a normalized contract path or raw.<path> for a source-specific value; inspect the sample's raw examples and field paths"
                if mapping.get("variants")
                else "; check the field's sources against this sample"
            )
            sample_failures.append(
                {
                    "kind": "missing_required",
                    "sample_id": sample["sample_id"],
                    "template_id": template_id,
                    "field": name,
                    "message": bounded(
                        f"sample {sample['sample_id']}, field {name}: {reason}{hint}",
                        600,
                    ),
                }
            )
    failures += sample_failures
    passed = (
        bool(answer.get("passed"))
        and not sample_failures
        and not any(t["gaps"] for t in templates_out)
    )
    missing = variants_missing(
        answer, mappings, [as_text(x) for x in as_list(ctx.input.get("variant_sample_ids"))]
    )
    if missing:
        passed = False
        failures += missing
    table = table_feedback(mappings, answer, sample_ids)
    if table["table_failures"] and ctx.text("sampling_fetch_mode") == "http":
        table.update(table_resample_needed=False, table_sources=[], table_sample_urls=[])
        table["table_failures"].append(
            {
                "kind": "table_unverified",
                "message": "Browser table evidence is unavailable in explicit HTTP mode; no acquisition was attempted.",
            }
        )
    if table["table_failures"]:
        passed = False
        failures += table["table_failures"]
    warnings = (
        empty_field_warnings(answer, mappings, templates_out)
        + variant_warnings(templates_out, threshold)
        + selector_warnings
        + [
            {"kind": "table_unavailable", "message": as_text(as_dict(row).get("message"))[:600]}
            for row in as_list(ctx.input.get("carried_table_warnings"))[:50]
            if as_dict(row).get("kind") == "table_unavailable"
        ]
    )
    lines = [
        f"{row['template_id']}.{row['name']}: {row['filled']} filled"
        + ("" if row["passed"] else " (below threshold)")
        for row in field_results
    ] + [line for line in (_variant_line(t) for t in templates_out) if line]
    lines += [f"Not passed: {row['message']}" for row in missing]
    lines += [f"Not passed: {row['message']}" for row in sample_failures]
    lines += [
        f"Not passed: {template['template_id']}: {gap}"
        for template in templates_out
        for gap in template["gaps"]
    ]
    lines += [row["message"] for row in table["table_failures"]]
    prices = price_status_text(samples_out)
    if prices:
        lines.append(prices)
    return {
        **base,
        **fallback_feedback(prior_warnings, prior_summary, table["table_failures"]),
        "can_refine": can_refine
        and (not table["table_failures"] or table["table_resample_needed"]),
        "table_resample_needed": table["table_resample_needed"],
        "table_sources": table["table_sources"],
        "table_sample_urls": table["table_sample_urls"],
        "passed": passed,
        "required_fill_threshold": answer.get("required_fill_threshold", threshold),
        "templates": templates_out,
        "field_results": field_results,
        # On a refinement the agent also reads the warnings.
        "failures": [] if passed else failures + warnings,
        "unmatched_sample_ids": unmatched,
        "sample_results": samples_out,
        "summary": ("Passed. " if passed else "Not passed. ")
        + "; ".join(lines)
        + "".join(f" Warning: {row['message']}" for row in warnings),
        "warnings": warnings,
    }


#: The service's mapping gaps (contract rows 66, 68): what to change, for the agent.
GAP_FIXES = {
    "price_not_extracted": "the page shows a price (or a range) that the mapping did not read: "
    "read the price from the product's own price block (see the sample's wss:section "
    "product-summary), and keep a price on request empty",
    "variants_not_used": "the page carries variant data the mapping ignores: add a variants "
    "block, copying a `source` from the sample's variant_sources",
    # Contract row 71 (A60, A61).
    "price_mismatch": "read the price the page states for programs first - [itemprop=price] "
    "content, JSON-LD offers.price (or lowPrice), meta product:price:amount, a data-price "
    "attribute - and the visible price text only as a fallback",
    "price_is_old_price": "read the current price (ins, .special-price, .price-new, .now, "
    "JSON-LD offers.price), never the struck-through one (del, .old-price, .was, "
    "compare-at): the service records the old price as regular_price",
}

#: Mapping test failures shown to a person in plain words before the service's details
#: (contract row 71); the older codes' service messages already read plainly.
PLAIN_GAP_CODES = ("price_mismatch", "price_is_old_price")


def gap_line(row: dict[str, Any]) -> str:
    """One mapping gap for the summary: plain words first for the price checks."""
    code = as_text(row.get("code"))
    if code in PLAIN_GAP_CODES:
        return f"{TEST_CODE_TEXT[code]} Details: {row['message']}"
    return as_text(row.get("message"))


def mapping_gap_failures(template_id: str, template: dict[str, Any]) -> list[dict[str, Any]]:
    """A54 (contract row 66): `templates[].failures[]` fail the template whatever the fill
    rates; each goes back to the agent with the service's message and what to change."""
    out: list[dict[str, Any]] = []
    for row in (as_dict(f) for f in as_list(template.get("failures"))):
        code = as_text(row.get("code"))
        message = bounded(as_text(row.get("message")) or code, 600)
        out.append(
            {
                "kind": "mapping_gap",
                "code": code,
                "template_id": template_id,
                "message": message,
                **({"plain": TEST_CODE_TEXT[code]} if code in PLAIN_GAP_CODES else {}),
                **({"what_to_change": GAP_FIXES[code]} if code in GAP_FIXES else {}),
            }
        )
    return out


#: `price_status` / `price_on_page` in plain words (contract row 65, README 6.5).
PRICE_STATUS_TEXT = {
    "shown": "price shown",
    "range": "price range",
    "on_request": "price on request",
    "missing": "no price found",
}


def _sample_result(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "sample_id": as_text(row.get("sample_id")),
        "url": as_text(row.get("url")),
        "template_id": row.get("template_id"),
        "missing_required": as_list(row.get("missing_required")),
    }
    if row.get("table_coverage") is not None:
        out["table_coverage"] = as_dict(row["table_coverage"])
    for key in ("price_on_page", "price_status", "price_max", "currency_from_default"):
        if row.get(key) is not None:
            out[key] = row.get(key)
    return out


def price_status_text(samples: list[dict[str, Any]]) -> str:
    """ "Prices on the samples: price shown 2, price on request 1; currency taken from the
    default on 1 sample." Empty when the service gives no price status (an older service,
    or no price field)."""
    counts: dict[str, int] = {}
    default_currency = 0
    for row in samples:
        status = as_text(row.get("price_status"))
        if status:
            counts[status] = counts.get(status, 0) + 1
        if row.get("currency_from_default") is True:
            default_currency += 1
    if not counts:
        return ""
    text = "Prices on the samples: " + ", ".join(
        f"{PRICE_STATUS_TEXT.get(status, status)} {number}" for status, number in counts.items()
    )
    if default_currency:
        text += f"; currency taken from the default on {count(default_currency, 'sample')}"
    return text + "."


def _site_mappings(ctx: ActionContext, site: str, *, versions: str = "active") -> dict:
    client = ctx.profile.client()
    query: dict[str, Any] = {"versions": versions, "include_content": "true"}
    template_id = ctx.text("template_id")
    if template_id:
        query["template_id"] = template_id
    return as_dict(client.get(f"/v1/sites/{client.segment(site)}/mappings", query=query))


def _active_entries(answer: dict[str, Any]) -> tuple[list[dict], dict[str, int], list[dict]]:
    templates: list[dict[str, Any]] = []
    expected: dict[str, int] = {}
    active: list[dict[str, Any]] = []
    for template in (as_dict(t) for t in as_list(answer.get("templates"))):
        template_id = as_text(template.get("template_id"))
        version = _as_int(template.get("active_version"))
        expected[template_id] = version
        templates.append({"template_id": template_id, "active_version": version})
        for row in (as_dict(v) for v in as_list(template.get("versions"))):
            if as_text(row.get("status")) == "active" and as_dict(row.get("mapping")):
                active.append(as_dict(row.get("mapping")))
    return templates, expected, active


def get_mappings(ctx: ActionContext) -> dict[str, Any]:
    site = ctx.require_text("site", "Site")
    answer = _site_mappings(ctx, site)
    templates, expected, active = _active_entries(answer)
    return {
        "site": site,
        "templates": templates,
        "mappings": active,
        "expected_versions": expected,
        "has_active_mapping": bool(active),
        "template_count": len(active),
    }


def _activate_one(
    ctx: ActionContext, site: str, mapping: dict[str, Any], expected_version: int, origin: dict
) -> tuple[str, dict[str, Any]]:
    client = ctx.profile.client()
    body: dict[str, Any] = {
        "template_id": as_text(mapping.get("template_id")),
        "expected_version": expected_version,
        "mapping": mapping,
    }
    if origin:
        body["origin"] = origin
    try:
        answer = as_dict(client.put(f"/v1/sites/{client.segment(site)}/mappings", body))
    except ScraperError as exc:
        if exc.service_code == "mapping_version_conflict":
            return "conflict", {
                "template_id": body["template_id"],
                "expected_version": expected_version,
                "active_version": as_dict(exc.result.get("details")).get("active_version"),
            }
        raise
    active = as_dict(answer.get("active"))
    return "activated", {
        "template_id": body["template_id"],
        "version": active.get("version"),
        "changed": bool(answer.get("changed")),
        "resolved_mapping_request_ids": as_list(answer.get("resolved_mapping_request_ids")),
    }


def _origin(ctx: ActionContext) -> dict[str, Any]:
    origin: dict[str, Any] = {}
    for key in ("run_id", "mapping_request_id"):
        value = ctx.text(key)
        if value:
            origin[key] = value
    if ctx.runtime.job_id:
        origin["platform_job_id"] = ctx.runtime.job_id
    note = ctx.text("note")
    if note:
        origin["note"] = note[:500]
    return origin


def activate_mapping(ctx: ActionContext) -> dict[str, Any]:
    site = ctx.require_text("site", "Site")
    mappings = _mappings_input(ctx.input.get("mappings") or ctx.input.get("mapping"))
    problems = structural_problems(mappings)
    if problems:
        raise ScraperError(
            "mapping_invalid",
            "The mapping cannot be activated: " + "; ".join(problems),
            no_external_effect=True,
        )
    expected_map = as_dict(ctx.input.get("expected_versions"))
    single = ctx.input.get("expected_version")
    activated: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    origin = _origin(ctx)
    for mapping in mappings:
        template_id = as_text(mapping.get("template_id"))
        if template_id in expected_map:
            expected = _as_int(expected_map[template_id])
        elif not is_unset(single) and len(mappings) == 1:
            expected = _as_int(single)
        else:
            expected = 0
        outcome, row = _activate_one(ctx, site, mapping, expected, origin)
        (activated if outcome == "activated" else conflicts).append(row)
    conflict = bool(conflicts)
    if conflict and ctx.input.get("fail_on_conflict") is True:
        lines = [
            f"{row['template_id']} (expected v{row['expected_version']}, active v{row['active_version']})"
            for row in conflicts
        ]
        raise ScraperError(
            "mapping_version_conflict",
            "Another change activated a newer mapping first: "
            + ", ".join(lines)
            + ". Nothing was overwritten. Start the workflow again to work on the active version.",
            result={"activated": activated, "conflicts": conflicts},
        )
    return {
        "site": site,
        "activated": activated,
        "conflicts": conflicts,
        "conflict": conflict,
        "all_activated": not conflict,
        "summary": ", ".join(
            f"{row['template_id']} v{row.get('version')}"
            + ("" if row.get("changed") else " (unchanged)")
            for row in activated
        )
        + (
            "; conflicts: " + ", ".join(row["template_id"] for row in conflicts)
            if conflicts
            else ""
        ),
    }


def export_mappings(ctx: ActionContext) -> dict[str, Any]:
    client = ctx.profile.client()
    only = ctx.text("site")
    sites: list[str] = []
    if only:
        sites = [only]
    else:
        cursor = None
        for _page in range(100):
            answer = as_dict(client.get("/v1/sites", query={"limit": 200, "cursor": cursor}))
            sites.extend(as_text(as_dict(row).get("site")) for row in as_list(answer.get("items")))
            cursor = answer.get("next_cursor")
            if not cursor:
                break
    exported: list[dict[str, Any]] = []
    template_count = 0
    for site in sites:
        if not site:
            continue
        answer = as_dict(
            client.get(
                f"/v1/sites/{client.segment(site)}/mappings",
                query={"versions": "active", "include_content": "true"},
            )
        )
        templates, _expected, _active = _active_entries(answer)
        rows = []
        for template in (as_dict(t) for t in as_list(answer.get("templates"))):
            for version in (as_dict(v) for v in as_list(template.get("versions"))):
                if as_text(version.get("status")) == "active" and version.get("mapping"):
                    rows.append(
                        {
                            "template_id": as_text(template.get("template_id")),
                            "version": version.get("version"),
                            "content_sha256": version.get("content_sha256"),
                            "mapping": version.get("mapping"),
                        }
                    )
        template_count += len(rows)
        exported.append({"site": site, "templates": rows})
    return {
        "export": {
            "format": EXPORT_FORMAT,
            "exported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sites": exported,
        },
        "site_count": len(exported),
        "template_count": template_count,
    }


def import_mappings(ctx: ActionContext) -> dict[str, Any]:
    raw = ctx.input.get("export")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ScraperError(
                "invalid_payload", "The mapping export is not valid JSON.", no_external_effect=True
            ) from exc
    export = as_dict(raw)
    if as_text(export.get("format")) != EXPORT_FORMAT:
        raise ScraperError(
            "invalid_payload",
            f"This is not a Web Scraper mapping export (format must be '{EXPORT_FORMAT}').",
            no_external_effect=True,
        )
    activated = unchanged = 0
    conflicts: list[dict[str, Any]] = []
    for site_row in (as_dict(s) for s in as_list(export.get("sites"))):
        site = as_text(site_row.get("site"))
        if not site:
            continue
        current = _site_mappings(ctx, site)
        _templates, expected, _active = _active_entries(current)
        for template in (as_dict(t) for t in as_list(site_row.get("templates"))):
            mapping = as_dict(template.get("mapping"))
            template_id = as_text(mapping.get("template_id"))
            if not template_id:
                continue
            outcome, row = _activate_one(
                ctx, site, mapping, expected.get(template_id, 0), {"note": "Imported mapping"}
            )
            if outcome == "conflict":
                conflicts.append({"site": site, **row})
            elif row.get("changed"):
                activated += 1
            else:
                unchanged += 1
    return {
        "activated": activated,
        "unchanged": unchanged,
        "conflicts": conflicts,
        "summary": f"{activated} activated, {unchanged} already identical, "
        f"{count(len(conflicts), 'conflict')}.",
    }


def request_failures(failures: Any) -> Any:
    """A repair's `failures` with `failures.gaps` (contract row 68) also in words: the
    repair reason the agent reads (what the stored pages show that the mapping misses)."""
    if not isinstance(failures, dict):
        return failures
    gaps = as_dict(failures.get("gaps"))
    if not gaps:
        return failures
    reason = [
        f"{count(number, 'stored page')}: {GAP_FIXES.get(code, code)}"
        for code, number in gaps.items()
    ]
    return {**failures, "repair_reason": reason}


def get_mapping_request(ctx: ActionContext) -> dict[str, Any]:
    request_id = ctx.require_text("mapping_request_id", "Mapping request id")
    client = ctx.profile.client()
    answer = as_dict(client.get(f"/v1/mapping-requests/{client.segment(request_id)}"))
    previous = as_dict(answer.get("previous"))
    instructions = as_dict(answer.get("instructions"))
    samples = [as_dict(row) for row in as_list(answer.get("samples"))]
    template_id = answer.get("template_id")
    bound_run_id = as_text(answer.get("run_id"))
    sampling_config = as_dict(get_run(ctx, bound_run_id).get("config")) if bound_run_id else {}
    return {
        "sampling_config": sampling_config,
        "mapping_request_id": request_id,
        "run_id": as_text(answer.get("run_id")),
        "site": as_text(answer.get("site")),
        "reason": as_text(answer.get("reason")),
        "status": as_text(answer.get("status")),
        "is_open": as_text(answer.get("status")) == "open",
        "template_id": template_id,
        "suggested_template_id": as_text(answer.get("suggested_template_id") or template_id),
        "expected_version": _as_int(answer.get("expected_version")),
        "expected_versions": {
            as_text(template_id or answer.get("suggested_template_id")): _as_int(
                answer.get("expected_version")
            )
        },
        "previous_mapping": as_dict(previous.get("mapping")) or None,
        "previous_mappings": [as_dict(previous.get("mapping"))] if previous.get("mapping") else [],
        "previous_version": previous.get("version"),
        "failures": request_failures(answer.get("failures")),
        "sample_ids": [as_text(row.get("sample_id")) for row in samples],
        "samples": _agent_samples(samples),
        "extraction_instructions": as_text(instructions.get("extraction_instructions")),
        "fields": as_list(instructions.get("fields")),
        "required_fields": as_list(instructions.get("required_fields")),
        "required_fill_threshold": instructions.get("required_fill_threshold"),
        "extraction_mode": as_text(instructions.get("extraction_mode"), "auto"),
        "max_refinements": instructions.get("max_refinements", 2),
        "target_page_selector": as_text(instructions.get("target_page_selector")),
        "variant_sample_ids": variant_sample_ids(samples),
        "ai_choose_fields": instructions.get("ai_choose_fields") is True,
        "fields_hint": as_text(instructions.get("fields_hint")),
        "instructions": instructions,
    }


def fail_mapping_request(ctx: ActionContext) -> dict[str, Any]:
    request_id = ctx.require_text("mapping_request_id", "Mapping request id")
    body: dict[str, Any] = {
        "failure_reason": bounded(ctx.text("failure_reason", "build_mapping did not pass"), 2000)
    }
    attempts = ctx.input.get("attempts")
    if not is_unset(attempts):
        try:
            body["attempts"] = _as_int(attempts)
        except (TypeError, ValueError):
            pass
    if ctx.runtime.job_id:
        body["platform_job_id"] = ctx.runtime.job_id
    client = ctx.profile.client()
    try:
        answer = as_dict(
            client.post(f"/v1/mapping-requests/{client.segment(request_id)}/fail", body)
        )
    except ScraperError as exc:
        if exc.service_code != "mapping_request_closed":
            raise
        status = as_text(as_dict(exc.result.get("details")).get("status"))
        return {
            "mapping_request_id": request_id,
            "status": status,
            "already_closed": True,
            "message": f"The mapping request was already {status or 'closed'}; nothing to report.",
        }
    return {
        "mapping_request_id": request_id,
        "status": as_text(answer.get("status")),
        "already_closed": False,
        "message": "The service was told that no passing mapping was built; the template "
        "is given up for this run and the run goes on with the rest.",
    }
