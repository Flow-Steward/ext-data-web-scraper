"""Run inputs, the old `Site settings` and URL lists, shaped as contract `job_settings` layers."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from typing import Any

from handlers.common import as_dict, as_list, as_text, count, is_unset
from handlers.errors import ScraperError

#: The run form of `Scrape a site` (0.1.9: every setting is a run input, design
#: "All settings are run inputs"). `urls` and `urls_file` are converted to the
#: contract's `urls` list and are not in this tuple. An empty value means "not
#: set in this layer": the profile's default, then the service's.
RUN_INPUT_KEYS = (
    "site_url",
    "extraction_instructions",
    "max_records",
    "target_page_selector",
    "url_include",
    "url_exclude",
    "discovery_mode",
    "debug",
    "crawl_include",
    "stay_in_section",
    "ai_choose_fields",
    "fields_hint",
    "fields",
    "required_fields",
    "sitemap_url",
    "max_sitemap_entries",
    "sitemap_source_include",
    "sitemap_source_exclude",
    "max_pages",
    "single_page",
    "fetch_mode",
    "browser",
    "browser_os",
    "canvas_noise",
    "use_proxy_tiers",
    "challenge_click",
    "use_solver",
    # Service rc-next12 (README 4.15, A64): a preset for the pauses, pages at once,
    # requests per minute and page timeout; the explicit fields below override it.
    "politeness",
    "delay_min_seconds",
    "delay_max_seconds",
    "max_concurrency",
    "max_requests_per_minute",
    # README 4.15 (A63): how long one page may take, 10-300 s.
    "page_timeout_seconds",
    "extraction_mode",
    "structured_data_types",
    "wait_after_load_ms",
    "wait_for_selector_ms",
    "headers",
    "respect_robots",
    "rebuild_mapping",
    # Service 2026-11-14 (README 4.11): links the crawl skips.
    "link_rules_off",
    "trap_pattern_budget",
    # README 4.13 (A65, A66): a start while a run of the same site is going on.
    "when_site_busy",
)

#: Link rules a run can switch off (`job_config.schema.json#/$defs/link_rule`), with
#: what each one skips, in plain words.
LINK_RULES = {
    "scheme": "mail, phone, JavaScript, messenger and app links",
    "messenger_social": "messenger and share buttons on other sites",
    "sensitive_path": "account, login, cart, checkout, wishlist, compare, search and review pages",
    "action_params": "links that act (add to cart, add to wishlist, one-time form keys)",
    "file_extension": "files: documents, archives, images, media, fonts, feeds",
    "nofollow": "links marked rel=nofollow",
    "facet_params": "filter and sort variants of a listing (color, size, price, sort ...)",
    "trap_budget": "URL patterns that gave no product page for many pages in a row",
    "dynamic_params": "random-looking parameters ignored when telling pages apart",
    "canonical": "duplicate addresses of a page already collected (by its canonical address)",
}

#: The `Site settings` step of project copies imported before 0.1.9 (plan 5.3;
#: `proxy_tiers` is `use_proxy_tiers` in the contract). The 0.1.9 template has no
#: such step; `validate_config` still accepts the layer, below the run inputs.
SITE_SETTING_KEYS = (
    "sitemap_url",
    "max_sitemap_entries",
    "sitemap_source_include",
    "sitemap_source_exclude",
    "url_exclude",
    "target_page_selector",
    "max_pages",
    "single_page",
    "fetch_mode",
    "browser",
    "use_proxy_tiers",
    "challenge_click",
    "use_solver",
    "delay_min_seconds",
    "delay_max_seconds",
    "max_concurrency",
    "max_requests_per_minute",
    "extraction_mode",
    "structured_data_types",
    "wait_after_load_ms",
    "wait_for_selector_ms",
    "headers",
    "respect_robots",
    "rebuild_mapping",
    "debug",
)

INTEGER_KEYS = {
    "max_sitemap_entries",
    "max_records",
    "max_pages",
    "max_concurrency",
    "max_requests_per_minute",
    "wait_after_load_ms",
    "wait_for_selector_ms",
    "trap_pattern_budget",
    "page_timeout_seconds",
}
NUMBER_KEYS = {"delay_min_seconds", "delay_max_seconds"}
BOOLEAN_KEYS = {
    "single_page",
    "challenge_click",
    "use_solver",
    "respect_robots",
    "rebuild_mapping",
    "debug",
    "stay_in_section",
    "ai_choose_fields",
}
STRING_LIST_KEYS = {
    "sitemap_source_include",
    "sitemap_source_exclude",
    "fields",
    "required_fields",
    "url_include",
    "url_exclude",
    "crawl_include",
    "structured_data_types",
    "link_rules_off",
}
INTEGER_LIST_KEYS = {"use_proxy_tiers"}
ENUM_KEYS = {
    "discovery_mode",
    "fetch_mode",
    "browser",
    "browser_os",
    "canvas_noise",
    "extraction_mode",
    "politeness",
    "when_site_busy",
}
OBJECT_KEYS = {"headers"}

URLS_FILE_MAX_BYTES = 20 * 1024 * 1024
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def _split_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [as_text(item) for item in value if as_text(item)]
    text = as_text(value)
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [as_text(item) for item in parsed if as_text(item)]
    separator = "\n" if "\n" in text else ","
    return [item.strip() for item in text.split(separator) if item.strip()]


def coerce_setting(key: str, value: Any) -> Any:
    """Turn what a person typed on the canvas into the contract type.

    Empty values stay empty: the contract reads `null`, `""`, `[]` and `{}` as
    "not set in this layer", so they fall through to the profile default.
    Anything that cannot be converted is passed on unchanged, so the service
    names the field in its answer.
    """
    if is_unset(value):
        return value
    if key in INTEGER_KEYS:
        if isinstance(value, bool):
            return value
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return value
        return int(number) if number.is_integer() else value
    if key in NUMBER_KEYS:
        if isinstance(value, bool):
            return value
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return value
        return int(number) if number.is_integer() else number
    if key in BOOLEAN_KEYS:
        if isinstance(value, bool):
            return value
        text = as_text(value).lower()
        if text in {"true", "yes", "1", "on"}:
            return True
        if text in {"false", "no", "0", "off"}:
            return False
        return value
    if key in STRING_LIST_KEYS:
        return _split_list(value)
    if key in INTEGER_LIST_KEYS:
        items = value if isinstance(value, list) else _split_list(value)
        result: list[Any] = []
        for item in items:
            try:
                result.append(int(str(item).strip()))
            except (TypeError, ValueError):
                result.append(item)
        return result
    if key in ENUM_KEYS:
        return as_text(value).lower()
    if key in OBJECT_KEYS:
        if isinstance(value, dict):
            return value
        text = as_text(value)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        headers: dict[str, str] = {}
        for line in text.splitlines():
            name, sep, header_value = line.partition(":")
            if sep and name.strip():
                headers[name.strip()] = header_value.strip()
        return headers or value
    if isinstance(value, str):
        return value.strip()
    return value


def _link_rules_off(value: Any) -> Any:
    """Rule names in lower case; an unknown name is refused before the service is asked."""
    names = [
        as_text(item).lower().replace("-", "_").replace(" ", "_") for item in _split_list(value)
    ]
    unknown = [name for name in names if name not in LINK_RULES]
    if unknown:
        raise ScraperError(
            "invalid_payload",
            f"Links not to skip: {', '.join(repr(n) for n in unknown)} "
            + ("is not a rule" if len(unknown) == 1 else "are not rules")
            + f". The rules are: {', '.join(LINK_RULES)}.",
            no_external_effect=True,
        )
    return list(dict.fromkeys(names))


def run_inputs_layer(inputs: dict[str, Any], url_entries: list[dict[str, str]]) -> dict[str, Any]:
    layer: dict[str, Any] = {}
    for key in RUN_INPUT_KEYS:
        if key in inputs:
            layer[key] = coerce_setting(key, inputs.get(key))
    if not is_unset(layer.get("link_rules_off")):
        layer["link_rules_off"] = _link_rules_off(layer["link_rules_off"])
    if url_entries:
        layer["urls"] = url_entries
    return layer


def site_settings_layer(settings: Any) -> dict[str, Any]:
    """Only the known `Site settings`; anything else is reported as unknown."""
    raw = settings
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    raw = as_dict(raw)
    unknown = sorted(set(raw) - set(SITE_SETTING_KEYS) - {"proxy_tiers"})
    if unknown:
        raise ScraperError(
            "invalid_payload",
            "The 'Site settings' step has entries the scraper does not know: "
            + ", ".join(unknown)
            + ". Remove or rename them; the setup guide lists every setting.",
            no_external_effect=True,
        )
    layer: dict[str, Any] = {}
    for key in SITE_SETTING_KEYS:
        if key in raw:
            layer[key] = coerce_setting(key, raw.get(key))
    if "proxy_tiers" in raw and "use_proxy_tiers" not in raw:
        # The task document's name for `use_proxy_tiers`.
        layer["use_proxy_tiers"] = coerce_setting("use_proxy_tiers", raw.get("proxy_tiers"))
    return layer


def parse_urls_file(content: bytes) -> list[dict[str, str]]:
    """CSV with a `url` column and an optional `key` column, or one URL per line."""
    if len(content) > URLS_FILE_MAX_BYTES:
        raise ScraperError(
            "urls_file_invalid",
            "The URL file is larger than 20 MiB. Split it into smaller files.",
            no_external_effect=True,
        )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ScraperError(
            "urls_file_invalid",
            "The URL file is not UTF-8 text. Save it as UTF-8 CSV or plain text.",
            no_external_effect=True,
        ) from exc
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    first = lines[0]
    delimiter = ","
    for candidate in (";", "\t", ","):
        if candidate in first:
            delimiter = candidate
            break
    header = [cell.strip().lower() for cell in next(csv.reader([first], delimiter=delimiter))]
    entries: list[dict[str, str]] = []
    problems: list[str] = []
    if "url" in header:
        reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter)
        reader.fieldnames = [name.strip().lower() for name in reader.fieldnames or []]
        for line_number, row in enumerate(reader, start=2):
            url = as_text(row.get("url"))
            key = as_text(row.get("key"))
            if not url:
                continue
            if not _URL_RE.match(url):
                problems.append(f"line {line_number}")
                continue
            entry = {"url": url}
            if key:
                entry["key"] = key[:200]
            entries.append(entry)
    else:
        for line_number, line in enumerate(lines, start=1):
            url = line.strip()
            if url.startswith("#"):
                continue
            if not _URL_RE.match(url):
                problems.append(f"line {line_number}")
                continue
            entries.append({"url": url})
    if problems:
        shown = ", ".join(problems[:10])
        more = f" and {len(problems) - 10} more" if len(problems) > 10 else ""
        raise ScraperError(
            "urls_file_invalid",
            f"The URL file has entries that are not http(s) URLs: {shown}{more}. Use a CSV "
            "with a 'url' column (and an optional 'key' column), or one URL per line.",
            no_external_effect=True,
        )
    return entries


def combine_urls(urls: Any, file_entries: list[dict[str, str]], *, cap: int | None) -> list[dict]:
    """`urls` first, then the file's rows; de-duplicated by (URL, key) and capped."""
    combined: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in _split_list(urls) if not isinstance(urls, list) else urls:
        if isinstance(raw, dict):
            url, key = as_text(raw.get("url")), as_text(raw.get("key"))
        else:
            url, key = as_text(raw), ""
        if not url:
            continue
        identity = (url, key)
        if identity in seen:
            continue
        seen.add(identity)
        combined.append({"url": url, **({"key": key} if key else {})})
    for entry in file_entries:
        identity = (entry["url"], entry.get("key", ""))
        if identity in seen:
            continue
        seen.add(identity)
        combined.append(dict(entry))
    if cap is not None and len(combined) > cap:
        raise ScraperError(
            "url_list_too_long",
            f"The URL list has {count(len(combined), 'entry', 'entries')}; this scraper service accepts at most "
            f"{cap} per run. Split the list into several runs.",
            no_external_effect=True,
        )
    return combined


_KEY_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def idempotency_key(job_id: str, step_id: str, loop_position: str | None = None) -> str:
    """`fs:<job id>:<step id>[:<loop position>]` (contract section 5)."""
    job = _KEY_SAFE.sub("_", as_text(job_id))
    step = _KEY_SAFE.sub("_", as_text(step_id))
    if not job or not step:
        raise ScraperError(
            "invalid_payload",
            "start_run runs only as a workflow step: the platform job id and step id are missing.",
            no_external_effect=True,
        )
    key = f"fs:{job}:{step}"
    if loop_position:
        key = f"{key}:{_KEY_SAFE.sub('_', as_text(loop_position))}"
    if len(key) > 255:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        key = f"fs:{digest}"
    return key


def loop_position(raw_context: dict[str, Any]) -> str | None:
    """The For each item position of this step entry, as the host sends it.

    The host puts `runtime_context.loop = {"for_each_id": <innermost For each>,
    "index": <0-based item position>}` into every envelope, and `{}` outside a
    For each (flow-steward/main `orchestration/runtime_identity.py:36-47, 67-83`).
    Nested For each is refused by the platform, so the index alone is unique in
    one job and step. A retry and a re-entry of the same item keep the index, so
    they keep the idempotency key (the service then returns the run it already
    started). Inside a For each without an index the envelope is not one this
    extension understands, and starting a run anyway could merge two items into
    one run, so it refuses.
    """
    raw = as_dict(raw_context)
    loop = as_dict(raw.get("loop"))
    index = loop.get("index")
    if isinstance(index, bool):
        index = None
    if isinstance(index, int) and index >= 0:
        return str(index)
    if isinstance(index, str) and index.strip().isdigit():
        return index.strip()
    if as_text(loop.get("for_each_id")) or as_text(raw.get("for_each_id")):
        raise ScraperError(
            "invalid_host_envelope",
            "This step runs inside a For each, but the platform did not send the item "
            "position (runtime_context.loop.index). The run was not started, so two items "
            "cannot share one scraper run.",
            no_external_effect=True,
        )
    return None


def numeric_loop_position(value: str | None) -> str | None:
    """The contract's `platform.loop_position` accepts only `^[0-9]+(\\.[0-9]+)*$`."""
    if value and re.fullmatch(r"[0-9]+(\.[0-9]+)*", value):
        return value
    return None


def as_url_list(value: Any) -> list[Any]:
    return as_list(value) if isinstance(value, list) else _split_list(value)
