"""Configuration, analysis and the run life cycle: validate, analyze, start, stop, resume, read."""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any
from urllib.parse import urlparse

from flowsteward_extension_sdk.artifacts import ArtifactAccessError, read_artifact_bytes

from handlers import protection as protection_text
from handlers.budget import slow_call_error, slow_call_timeout
from handlers.common import as_dict, as_list, as_text, bounded, count
from handlers.context import ActionContext
from handlers.errors import ScraperError, describe_field_problems
from handlers.settings import (
    combine_urls,
    idempotency_key,
    loop_position,
    numeric_loop_position,
    parse_urls_file,
    run_inputs_layer,
    site_settings_layer,
)

URLS_FILE_BINDING = "urls_file"
#: The step of Scrape a site (0.1.23 and later) that starts a run and then waits for it
#: (Flow Steward "Wait for an event", ADR 0071): that job stores the run's files itself,
#: so Scraper events only wakes it. The service keeps the starting step id
#: (`platform.step_id`); older copies start from `start_run` and do not wait.
WAITING_START_STEP = "start_crawl"


def site_key(url: str) -> str:
    """Contract "Site key": IDNA host, lower case, no trailing dot, no `www.`, non-default port."""
    parsed = urlparse(as_text(url))
    host = (parsed.hostname or "").rstrip(".")
    if not host:
        return ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    port = parsed.port
    default = 443 if parsed.scheme == "https" else 80
    if port and port != default:
        host = f"{host}:{port}"
    return host


def _artifact_id(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("artifact_handle") or value.get("artifact_id") or value.get("handle")
    return as_text(value).removeprefix("artifact:")


def _read_urls_file(ctx: ActionContext) -> list[dict[str, str]]:
    artifact_id = _artifact_id(ctx.input.get("urls_file"))
    if not artifact_id:
        return []
    try:
        content = read_artifact_bytes(
            ctx.payload, artifact_id=artifact_id, binding_key=URLS_FILE_BINDING, timeout_seconds=30
        )
    except ArtifactAccessError as exc:
        raise ScraperError(
            "urls_file_invalid",
            f"The URL file could not be read from the platform: {exc}.",
            no_external_effect=True,
        ) from exc
    return parse_urls_file(content)


def _url_list_hint(inputs: dict[str, Any], errors: Any, url_entries: list[Any]) -> str:
    """A39: the pages of a url_list run typed into the URL filter instead of the page list."""
    urls_missing = any(
        as_text(as_dict(row).get("field")) == "urls"
        and as_text(as_dict(row).get("code")) == "required"
        for row in as_list(errors)
    )
    if not urls_missing or url_entries or not as_list(inputs.get("url_include")):
        return ""
    return (
        " The addresses are in 'Extract only URLs matching (regex)', which is a filter for"
        " a crawl. In url_list mode put them into 'Pages to scrape (url_list)' and leave"
        " the filter empty."
    )


def validate_config(ctx: ActionContext) -> dict[str, Any]:
    profile = ctx.profile
    client = profile.client()
    health = as_dict(client.get("/health"))
    cap = as_dict(health.get("limits")).get("max_url_list_entries")
    file_entries = _read_urls_file(ctx)
    url_entries = combine_urls(
        ctx.input.get("urls") or [], file_entries, cap=int(cap) if cap else None
    )
    run_inputs = run_inputs_layer(ctx.input, url_entries)
    site_settings = site_settings_layer(ctx.input.get("site_settings"))
    body = {
        "run_inputs": run_inputs,
        "site_settings": site_settings,
        "profile": profile.contract_profile(with_defaults=True, with_webhook=True),
    }
    answer = as_dict(client.post("/v1/config/validate", body))
    warnings = [as_dict(row) for row in as_list(answer.get("warnings"))]
    if not answer.get("valid"):
        problems = describe_field_problems(answer.get("errors"))
        raise ScraperError(
            "config_invalid",
            "The scrape settings were refused: " + "; ".join(problems) + ". Change these "
            "values in the run form or the scraper profile (or, in a copy imported before 0.1.9, its 'Site settings' step)."
            + _url_list_hint(ctx.input, answer.get("errors"), url_entries),
            result={
                "valid": False,
                "errors": [as_dict(row) for row in as_list(answer.get("errors"))],
                "warnings": warnings,
            },
            no_external_effect=True,
        )
    config = as_dict(answer.get("config"))
    urls = as_list(config.get("urls"))
    discovery_mode = as_text(config.get("discovery_mode"), "auto")
    site = site_key(as_text(config.get("site_url"))) or (
        site_key(as_text(as_dict(urls[0]).get("url"))) if urls else ""
    )
    return {
        "valid": True,
        "config": config,
        "warnings": warnings,
        "warning_text": "; ".join(describe_field_problems(warnings)) or "",
        "sources": as_dict(answer.get("sources")),
        "site": site,
        "discovery_mode": discovery_mode,
        "is_url_list": discovery_mode == "url_list",
        "url_count": len(urls),
        "rebuild_mapping": bool(config.get("rebuild_mapping")),
        "required_fill_threshold": config.get("required_fill_threshold"),
        "max_refinements": config.get("max_refinements"),
        "extraction_instructions": as_text(config.get("extraction_instructions")),
        "fields": as_list(config.get("fields")),
        "required_fields": as_list(config.get("required_fields")),
        "extraction_mode": as_text(config.get("extraction_mode"), "auto"),
        "target_page_selector": as_text(config.get("target_page_selector")),
        "ai_choose_fields": config.get("ai_choose_fields") is True,
        "fields_hint": as_text(config.get("fields_hint")),
    }


def _config(ctx: ActionContext) -> dict[str, Any]:
    config = as_dict(ctx.input.get("config"))
    if not config:
        raise ScraperError(
            "invalid_payload",
            "The merged configuration is missing. Map the 'config' output of the "
            "'Validate settings' step into this step.",
            no_external_effect=True,
        )
    return config


def slow_post(
    ctx: ActionContext,
    path: str,
    body: dict[str, Any],
    *,
    what: str,
    cause: str = "",
    after: str = "",
) -> Any:
    """A call that may run the site's protection route: bounded by the action's deadline.

    `cause` and `after` let the timeout name what is known about the site's protection.
    """
    seconds = slow_call_timeout(ctx.started, what, cause, after)
    try:
        return ctx.profile.client(seconds).post(path, body, timeout=seconds)
    except ScraperError as exc:
        if exc.code == "service_timeout":
            raise slow_call_error(what, seconds, cause, after) from exc
        raise


def analyze_site(ctx: ActionContext) -> dict[str, Any]:
    profile = ctx.profile
    config = _config(ctx)
    answer = as_dict(
        slow_post(
            ctx,
            "/v1/analyze",
            {"config": config, "profile": profile.contract_profile(with_webhook=False)},
            what="the site analysis",
        )
    )
    recommended = as_dict(answer.get("recommended"))
    sitemap = as_dict(answer.get("sitemap"))
    protection = as_dict(answer.get("protection"))
    traces = as_list(answer.get("protection_trace"))
    warnings = as_list(answer.get("warnings"))
    site = as_text(answer.get("site"))
    requested = as_text(config.get("discovery_mode"), "auto")
    chosen = requested
    if requested == "auto":
        chosen = as_text(recommended.get("discovery_mode"), "auto")
    return {
        "analysis": answer,
        "recommended_discovery_mode": as_text(recommended.get("discovery_mode")),
        "discovery_mode": chosen,
        "sitemap_found": bool(sitemap.get("found")),
        "sitemap_url_count": sitemap.get("url_count"),
        "js_required": bool(as_dict(answer.get("rendering")).get("js_required")),
        "protection": as_text(protection.get("kind"), "none"),
        "needs_proxy": bool(recommended.get("needs_proxy")),
        "reasons": "; ".join(as_text(r) for r in as_list(recommended.get("reasons"))),
        "warnings": warnings,
        "protection_summary": protection_text.summary(
            traces, warnings=warnings, protection=protection, site=site
        ),
        "protection_not_passed": protection_text.not_passed(
            traces, warnings=warnings, protection=protection
        ),
        "link_rules": link_rules_text(answer),
    }


def start_run(ctx: ActionContext) -> dict[str, Any]:
    profile = ctx.profile
    config = _config(ctx)
    webhook = profile.require_webhook()
    runtime = ctx.runtime
    position = loop_position({**runtime.raw, "for_each_id": runtime.for_each_id})
    key = idempotency_key(runtime.job_id, runtime.step_id or "", position)
    platform: dict[str, Any] = {
        "job_id": runtime.job_id,
        "step_id": runtime.step_id or "",
        "project_id": runtime.project_id,
        "connection_id": profile.connection_id or ctx.connection_id,
    }
    if runtime.workflow_id:
        platform["workflow_id"] = runtime.workflow_id
    if runtime.account_id:
        platform["account_id"] = runtime.account_id
    numeric = numeric_loop_position(position)
    if numeric:
        platform["loop_position"] = numeric
    if not platform["project_id"] or not platform["connection_id"]:
        raise ScraperError(
            "invalid_payload",
            "start_run needs the platform project and the scraper profile of the step.",
            no_external_effect=True,
        )
    body = {
        "config": config,
        "profile": {**profile.contract_profile(with_defaults=False), "webhook": webhook},
        "platform": platform,
    }
    try:
        status, answer, _headers = profile.client().request(
            "POST", "/v1/runs", body, headers={"Idempotency-Key": key}
        )
    except ScraperError as exc:
        if exc.service_code == "site_busy" and ctx.input.get("skipped_start_succeeds") is True:
            return _skipped_start(exc, key)
        raise
    run = as_dict(answer)
    run_id = as_text(run.get("run_id"))
    if not run_id:
        raise ScraperError("invalid_service_response", "The service did not return a run id.")
    return {
        "run_id": run_id,
        "state": as_text(run.get("state")),
        "site": as_text(run.get("site")),
        "created": status == 201,
        "idempotency_key": key,
        "run_page_hint": (f"{_queued(run)} " if _queued(run) else "")
        + run_page_hint(
            profile.profile_name,
            run_id,
            state=as_text(run.get("state")),
            stop_reason=as_text(run.get("stop_reason")),
        ),
        "skipped": False,
        "skipped_reason": "",
        "busy_run_id": "",
    }


def _skipped_start(exc: ScraperError, key: str) -> dict[str, Any]:
    """A66: `409 site_busy` (when_site_busy skip, contract row 75) as a skipped start.

    Only with `skipped_start_succeeds: true` (Scrape a site from 0.1.24): the step ends
    successfully without a run id, and the workflow ends 'Skipped'. Without it the step
    fails with `site_busy` and the same sentence (older copies, a person's own step)."""
    details = as_dict(exc.result.get("details"))
    return {
        "run_id": "",
        "state": "skipped",
        "site": as_text(details.get("site")),
        "created": False,
        "idempotency_key": key,
        "run_page_hint": exc.message,
        "skipped": True,
        "skipped_reason": exc.message,
        "busy_run_id": as_text(details.get("run_id")),
    }


def _queued(run: dict[str, Any]) -> str:
    """README 4.13: a run of a site that is already running waits; say so."""
    if as_text(run.get("state")) != "queued":
        return ""
    message = as_text(as_dict(run.get("queued_reason")).get("message"))
    return (message if message.endswith(".") else message + ".") if message else ""


def link_rules_text(analysis: dict[str, Any]) -> str:
    """The analysis's `link_rules` (README 4.11) in plain words."""
    rules = [as_dict(r) for r in as_list(as_dict(analysis.get("link_rules")).get("rules"))]
    if not rules:
        return ""
    on = [as_text(r.get("description")) or as_text(r.get("name")) for r in rules if r.get("on")]
    off = [as_text(r.get("name")) for r in rules if not r.get("on")]
    budget = as_dict(analysis.get("link_rules")).get("trap_pattern_budget")
    text = "The crawl skips: " + "; ".join(on) + "." if on else "The crawl skips no links."
    if off:
        text += f" Switched off for this run: {', '.join(off)}."
    if budget is not None:
        text += (
            " Trap budget: off."
            if not budget
            else f" Trap budget: a URL pattern stops being followed after {budget} pages "
            "without a product page."
        )
    return text


#: The service's default reasons for a re-collect (contract README 4.10).
RECOLLECT_DEFAULT_REASONS = (
    "fetch_failed",
    "blocked",
    "incomplete",
    "no_template",
    "template_failed",
    "stopped",
)
RECOLLECT_REASONS = (
    *RECOLLECT_DEFAULT_REASONS,
    "not_found",
    "robots_disallowed",
    "private_address_refused",
)
#: A page has no job: presses within this many seconds reuse one key, so a double
#: click starts one run; a later press starts a new one.
PAGE_KEY_WINDOW_SECONDS = 600


def _reasons(ctx: ActionContext) -> list[str] | None:
    raw = ctx.input.get("reasons")
    if raw is None or raw == "" or raw == []:
        return None
    if isinstance(raw, str):
        raw = [part for part in re.split(r"[\s,]+", raw) if part]
    if not isinstance(raw, list):
        raise ScraperError(
            "invalid_payload", "Reasons must be a list of reasons.", no_external_effect=True
        )
    reasons: list[str] = []
    for item in raw:
        reason = as_text(item).lower()
        if reason not in RECOLLECT_REASONS:
            raise ScraperError(
                "invalid_payload",
                f"'{reason}' is not an unextracted reason. Use: {', '.join(RECOLLECT_REASONS)}.",
                no_external_effect=True,
            )
        if reason not in reasons:
            reasons.append(reason)
    return reasons


def _by_reason_text(by_reason: dict[str, Any]) -> str:
    return ", ".join(
        f"{reason.replace('_', ' ')} {number}" for reason, number in by_reason.items() if number
    )


def recollect_text(run: dict[str, Any]) -> str:
    """Run detail: the parent of a re-collect run, and the re-collect runs of a parent (A45)."""
    parts = []
    parent = as_dict(run.get("recollect_of"))
    if parent:
        parts.append(
            f"Collects again {count(parent.get('url_count'), 'missed page')} of run "
            f"{as_text(parent.get('run_id'))}"
            + (
                f" ({_by_reason_text(as_dict(parent.get('by_reason')))})"
                if as_dict(parent.get("by_reason"))
                else ""
            )
            + "."
        )
    children = [as_dict(c) for c in as_list(run.get("recollected_by"))]
    if children:
        parts.append(
            "Missed pages collected again by "
            + ", ".join(f"{as_text(c.get('run_id'))} ({as_text(c.get('state'))})" for c in children)
            + "."
        )
    return " ".join(parts)


def recollect_run(ctx: ActionContext) -> dict[str, Any]:
    """POST /v1/runs/{id}/recollect: a new run over the parent's missed pages (A45)."""
    parent_id = ctx.require_text("run_id", "Run id")
    reasons = _reasons(ctx)
    profile = ctx.profile
    webhook = profile.require_webhook()
    runtime = ctx.runtime
    connection_id = profile.connection_id or ctx.connection_id
    if runtime.job_id and runtime.step_id:
        # A workflow step: one run per job, step and loop item, like start_run.
        position = loop_position({**runtime.raw, "for_each_id": runtime.for_each_id})
        key = idempotency_key(runtime.job_id, runtime.step_id, position)
        job_id, step_id = runtime.job_id, runtime.step_id
        numeric = numeric_loop_position(position)
    else:
        # The Run detail page: no job. The key covers the profile, the run, the
        # reasons and a ten-minute window.
        window = int(time.time()) // PAGE_KEY_WINDOW_SECONDS
        seed = f"{connection_id}|{parent_id}|{','.join(reasons or [])}|{window}"
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        key = f"fs:page:{digest}"
        job_id, step_id, numeric = f"page-{digest}", "run_detail_recollect", None
    platform: dict[str, Any] = {
        "job_id": job_id,
        "step_id": step_id,
        "project_id": runtime.project_id,
        "connection_id": connection_id,
    }
    if runtime.workflow_id:
        platform["workflow_id"] = runtime.workflow_id
    if runtime.account_id:
        platform["account_id"] = runtime.account_id
    if numeric:
        platform["loop_position"] = numeric
    if not platform["project_id"] or not platform["connection_id"]:
        raise ScraperError(
            "invalid_payload",
            "Re-collect needs the platform project and the scraper profile.",
            no_external_effect=True,
        )
    body: dict[str, Any] = {
        "profile": {**profile.contract_profile(with_defaults=False), "webhook": webhook},
        "platform": platform,
    }
    if reasons:
        body["reasons"] = reasons
    client = profile.client()
    status, answer, _headers = client.request(
        "POST",
        f"/v1/runs/{client.segment(parent_id)}/recollect",
        body,
        headers={"Idempotency-Key": key},
    )
    run = as_dict(answer)
    run_id = as_text(run.get("run_id"))
    if not run_id:
        raise ScraperError("invalid_service_response", "The service did not return a run id.")
    parent = as_dict(run.get("recollect_of"))
    urls = int(parent.get("url_count") or 0)
    by_reason = _by_reason_text(as_dict(parent.get("by_reason")))
    message = (
        f"Run {run_id} collects again {count(urls, 'missed page')} of run {parent_id}"
        + (f" ({by_reason})" if by_reason else "")
        + ". It has its own result: when it ends, a 'Scraper events' job stores it. Follow it on "
        "Run detail."
    )
    if status != 201:
        message = f"This re-collect was already started a moment ago: {message}"
    return {
        "message": message,
        "run_id": run_id,
        "parent_run_id": parent_id,
        "state": as_text(run.get("state")),
        "url_count": urls,
        "by_reason": by_reason,
        "reasons": list(as_list(parent.get("reasons")) or reasons or RECOLLECT_DEFAULT_REASONS),
        "created": status == 201,
    }


def run_page_hint(
    profile_name: str, run_id: str, *, state: str = "running", stop_reason: str = ""
) -> str:
    """Where the operator follows the crawl and finds its result (A28, A40, A55).

    A queued run (README 4.13, 4.6) "is queued", not "has started" (A70)."""
    status = {
        "queued": "is queued",
        "running": "has started",
        "completed": "has completed",
        "failed": "has failed",
    }.get(state, f"is {state.replace('_', ' ')}")
    if stop_reason:
        status += f", stop reason {stop_reason}"
    return (
        f"Scraper run {run_id} {status}. "
        "Scrape a site (0.1.23 and later) waits for it "
        "and, when the crawl ends, shows the files result.jsonl (result_file) and result.csv "
        "(result_csv), the first rows (result_preview) and the run summary in its own "
        "Result panel; an older copy of Scrape a site ends here, and a separate 'Scraper "
        "events' job stores the files. To follow the crawl: Web Scraper > Runs (profile "
        f"'{profile_name}') lists it; open it there (Run detail) for how far it is, why it "
        "stopped and which job holds the result."
    )


def _run_id(ctx: ActionContext) -> str:
    return ctx.require_text("run_id", "Run id")


def stop_run(ctx: ActionContext) -> dict[str, Any]:
    run_id = _run_id(ctx)
    note = ctx.text("note", "Stopped from Flow Steward")[:500]
    run = as_dict(
        ctx.profile.client().post(
            f"/v1/runs/{ctx.profile.client().segment(run_id)}/stop", {"note": note}
        )
    )
    return {"run_id": run_id, "state": as_text(run.get("state")), "message": _state_line(run)}


def resume_run(ctx: ActionContext) -> dict[str, Any]:
    profile = ctx.profile
    run_id = _run_id(ctx)
    body = {"profile": profile.contract_profile(with_defaults=False)}
    run = as_dict(
        profile.client().post(f"/v1/runs/{profile.client().segment(run_id)}/resume", body)
    )
    return {
        "run_id": run_id,
        "state": as_text(run.get("state")),
        "message": "The current secrets of this profile were sent to the service. "
        + _state_line(run),
    }


def _state_line(run: dict[str, Any]) -> str:
    state = as_text(run.get("state"))
    outcome = as_text(run.get("outcome"))
    stop_reason = as_text(run.get("stop_reason"))
    line = f"Run is {state}"
    if outcome:
        line += f", outcome {outcome}"
    if stop_reason:
        line += f", stop reason {stop_reason}"
    return line + "."


#: Why a run stopped, in plain words (contract `stop_reason`); unknown values are shown as sent.
STOP_REASON_TEXT = {
    "finished": "it finished without extracting a record (see unextracted URLs on Run detail)",
    "stopped": "it was stopped by hand",
    "max_pages": "it reached its maximum pages",
    "max_records": "it reached its maximum records",
    "max_duration": "it ran out of its time limit",
    "max_result_size": "its result reached the size limit",
    "blocked": "the site blocked it (see the protection on Run detail)",
    "site_unavailable": "the site stopped answering (server errors or timeouts, after growing pauses)",
    "stalled": "nothing moved for too long (no page finished, no record written), so the service "
    "stopped it; run it again, and if it stalls again check the site and the proxies",
}


#: States in which the run has not ended yet: the service compares it with the previous
#: full run already, but a comparison means something only at the end (A70).
UNFINISHED_STATES = {"queued", "running", "stopping", "waiting_for_credentials"}


def explanation_text(run: dict[str, Any]) -> str:
    """ROUND2 §6: the service's own sentence on why the run stopped (or how it goes), with
    its advice."""
    explanation = as_dict(run.get("explanation"))
    sentence = as_text(explanation.get("sentence"))
    # The service appends the comparison's warning to its sentence also while the run
    # still crawls (A70): leave it out until the run has ended.
    warning = as_text(as_dict(run.get("comparison")).get("warning"))
    if warning and as_text(run.get("state")) in UNFINISHED_STATES and warning in sentence:
        sentence = sentence.replace(warning, "", 1).strip()
    advice = as_text(explanation.get("advice"))
    if advice:
        sentence = f"{sentence} What to do: {advice}" if sentence else f"What to do: {advice}"
    return sentence


def completeness_text(run: dict[str, Any]) -> str:
    """ROUND2 §7: the progress while running, else what was collected of what was found;
    with the comparison's warning against the previous full run."""
    completeness = as_dict(run.get("completeness"))
    progress = as_dict(completeness.get("progress"))
    text = (
        as_text(progress.get("text")) if as_text(run.get("state")) in {"queued", "running"} else ""
    )
    text = text or as_text(completeness.get("sentence"))
    return " ".join(part for part in (text, comparison_warning(run)) if part)


def comparison_warning(run: dict[str, Any]) -> str:
    """The comparison's warning against the previous full run, once the run has ended."""
    if as_text(run.get("state")) in UNFINISHED_STATES:
        return ""
    return as_text(as_dict(run.get("comparison")).get("warning"))


def timeline_text(run: dict[str, Any], last: int = 8) -> str:
    """The last notable moments, "10:02 discovery decided: …; 10:15 stop: …"."""
    rows = [as_dict(r) for r in as_list(run.get("timeline"))][-last:]
    return "; ".join(
        bounded(
            f"{as_text(r.get('at'))[11:16]} {as_text(r.get('message')) or as_text(r.get('kind'))}",
            160,
        )
        for r in rows
    )


#: `soft_errors_by_reason` keys (README 4.16) in a few words.
SOFT_ERROR_WORDS = {
    "empty": "empty",
    "truncated": "cut off",
    "error_page": "error or maintenance page",
    "too_small": "far too small",
    "lost_template": "without the page's usual layout",
}


def politeness_text(run: dict[str, Any]) -> str:
    """README 4.15 (A63, A64): the run's effective politeness and any host it slowed down.

    "Politeness: normal: 2-5 s, 2 at a time, 30/min, page timeout 60 s. Slowed down:
    shop.example (x2.0 since 10:15: 6 errors in the last 20 pages)." Empty with a
    service older than rc-next12."""
    politeness = as_dict(run.get("politeness"))
    text = as_text(politeness.get("text"))
    if not text and as_text(politeness.get("preset")):
        text = as_text(politeness.get("preset"))
    hosts = []
    for host, row in as_dict(politeness.get("slowed_hosts")).items():
        item = as_dict(row)
        detail = []
        factor = item.get("factor")
        if isinstance(factor, (int, float)) and not isinstance(factor, bool):
            detail.append(f"x{float(factor):.1f}")
        since = as_text(item.get("since"))
        if since:
            detail.append(f"since {since[11:16] or since}")
        reason = as_text(item.get("reason"))
        line = host + (f" ({' '.join(detail)}" if detail else "")
        if reason:
            line += (": " if detail else " (") + bounded(reason, 160)
        if detail or reason:
            line += ")"
        hosts.append(line)
    if not text and not hosts:
        return ""
    out = f"Politeness: {text}." if text else ""
    if hosts:
        out += (" " if out else "") + "Slowed down: " + "; ".join(hosts[:5]) + "."
    return out


def soft_errors_text(run: dict[str, Any]) -> str:
    """README 4.16 (A62): answers with status 200 that were not the page, retried; and
    repairs not opened because the pages were fine when fetched again."""
    counters = as_dict(run.get("counters"))
    parts = []
    soft = counters.get("soft_errors")
    if isinstance(soft, int) and soft > 0:
        by_reason = {k: v for k, v in as_dict(counters.get("soft_errors_by_reason")).items() if v}
        parts.append(
            f"{count(soft, 'unusable answer')} from the site (status 200 but not the page) "
            "retried"
            + (
                " ("
                + ", ".join(
                    f"{SOFT_ERROR_WORDS.get(k, k.replace('_', ' '))} {v}"
                    for k, v in by_reason.items()
                )
                + ")"
                if by_reason
                else ""
            )
        )
    avoided = counters.get("repairs_avoided")
    if isinstance(avoided, int) and avoided > 0:
        parts.append(
            f"{count(avoided, 'mapping repair')} avoided: the pages were fine when fetched again"
        )
    return ("; ".join(parts) + ".") if parts else ""


def record_count_of(run: dict[str, Any]) -> int:
    """Records of a run: the result descriptor's count, else the extraction counter."""
    result = as_dict(run.get("result"))
    value = result.get("record_count") if result else None
    if value is None:
        value = as_dict(run.get("counters")).get("records_extracted")
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def no_records_text(run: dict[str, Any]) -> str:
    """A36: the plain reason a finished run has no result file; empty otherwise."""
    if as_text(run.get("state")) != "completed" or record_count_of(run) > 0:
        return ""
    reason = as_text(run.get("stop_reason"))
    pages = as_dict(run.get("counters")).get("pages_fetched")
    text = "The run produced no records"
    if pages is not None:
        text += f" from {count(pages, 'page')} loaded"
    if reason:
        text += f": {STOP_REASON_TEXT.get(reason, f'stop reason {reason}')}"
    return text + ". There is no result file; nothing was stored."


def get_run(ctx: ActionContext, run_id: str) -> dict[str, Any]:
    client = ctx.profile.client()
    return as_dict(client.get(f"/v1/runs/{client.segment(run_id)}"))


def summarize_run(run: dict[str, Any]) -> dict[str, Any]:
    """The descriptor-sized summary a workflow carries: no records, no HTML, no secrets."""
    counters = as_dict(run.get("counters"))
    templates = [
        {
            "template_id": as_text(row.get("template_id")),
            "status": as_text(row.get("status")),
            "mapping_version": row.get("mapping_version"),
            "record_count": row.get("record_count"),
            "unextracted_count": row.get("unextracted_count"),
            "required_fill_rate": row.get("required_fill_rate"),
        }
        for row in (as_dict(item) for item in as_list(run.get("templates")))
    ]
    errors = [
        {
            "code": as_text(row.get("code")),
            "message": bounded(as_text(row.get("message")), 300),
            "severity": as_text(row.get("severity")),
            "template_id": row.get("template_id"),
        }
        for row in (as_dict(item) for item in as_list(run.get("errors"))[:20])
    ]
    error = as_dict(run.get("error")) or None
    return {
        "run_id": as_text(run.get("run_id")),
        "state": as_text(run.get("state")),
        "outcome": run.get("outcome"),
        "stop_reason": run.get("stop_reason"),
        "site": as_text(run.get("site")),
        "terminal": as_text(run.get("state")) in {"completed", "failed"},
        "templates": templates,
        "counters": {
            "pages_fetched": counters.get("pages_fetched"),
            "records_extracted": counters.get("records_extracted"),
            "unextracted": counters.get("unextracted"),
            "required_fill_rate": counters.get("required_fill_rate"),
            "repairs_started": counters.get("repairs_started"),
            "repairs_succeeded": counters.get("repairs_succeeded"),
            "repairs_avoided": counters.get("repairs_avoided"),
            "soft_errors": counters.get("soft_errors"),
            "soft_errors_by_reason": as_dict(counters.get("soft_errors_by_reason")) or None,
            "result_bytes": counters.get("result_bytes"),
        },
        "politeness": as_dict(run.get("politeness")) or None,
        "errors": errors,
        "error": (
            {
                "code": as_text(error.get("code")),
                "message": bounded(as_text(error.get("message")), 500),
            }
            if error
            else None
        ),
        "platform_job_id": as_text(as_dict(run.get("platform")).get("job_id")),
        "finished_at": run.get("finished_at"),
        "record_count": record_count_of(run),
        "has_records": record_count_of(run) > 0,
        "no_records_message": no_records_text(run),
        "explanation": explanation_text(run),
        "completeness": completeness_text(run),
        "comparison_warning": comparison_warning(run),
        "timeline": timeline_text(run),
    }


def _not_repeated(completeness: str, explanation: str, run: dict[str, Any]) -> str:
    """A38: drop the completeness sentence when the explanation already says it."""
    sentence = as_text(as_dict(run.get("completeness")).get("sentence")).strip()
    if not sentence or sentence.rstrip(".").lower() not in explanation.lower():
        return completeness
    return completeness.replace(sentence, "", 1).strip()


def result_by_starter(run: dict[str, Any]) -> bool:
    """Whether the job that started the run waits for it and stores its files."""
    return as_text(as_dict(run.get("platform")).get("step_id")) == WAITING_START_STEP


def _not_completed(run: dict[str, Any], message: str) -> ScraperError:
    """`require_completed`: the run did not complete; the error carries its summary."""
    run_id = as_text(run.get("run_id"))
    still = as_text(run.get("state")) not in {"completed", "failed"}
    return ScraperError(
        "run_not_completed",
        f"The scraper run {run_id} did not complete. {message}"
        + (
            " It is still going on: follow it on Web Scraper > Runs (Run detail), or stop it there."
            if still
            else ""
        ),
        no_external_effect=True,
    )


def get_run_summary(ctx: ActionContext) -> dict[str, Any]:
    run = get_run(ctx, _run_id(ctx))
    summary = summarize_run(run)
    summary["result_by_starter"] = result_by_starter(run)
    summary["run_page_hint"] = (f"{_queued(run)} " if _queued(run) else "") + run_page_hint(
        ctx.profile.profile_name,
        summary["run_id"],
        state=summary["state"],
        stop_reason=as_text(run.get("stop_reason")),
    )
    summary["message"] = " ".join(
        part
        for part in (
            _state_line(run),
            summary["explanation"],
            _not_repeated(summary["completeness"], summary["explanation"], run),
            summary["no_records_message"],
            _queued(run),
            soft_errors_text(run),
            politeness_text(run),
            recollect_text(run),
        )
        if part
    )
    if ctx.input.get("require_completed") is True and summary["state"] != "completed":
        raise _not_completed(run, summary["message"])
    return summary
