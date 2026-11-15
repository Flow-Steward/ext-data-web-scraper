"""Run page actions: read the service through the selected scraper profile.

Extension pages can reach a connection only through an action's target (a
query gets no connection; see docs/DEV-LOG.md), so the Runs and Run detail
pages are connection forms whose buttons call these actions. Each answer leads
with at most a dozen plain lines, which the platform shows as the action's
summary, and keeps the full rows under `details`.
"""

from __future__ import annotations

from typing import Any

from handlers import protection as protection_text
from handlers.common import as_dict, as_list, as_text, bounded, count, human_bytes
from handlers.context import ActionContext
from handlers.errors import ScraperError
from handlers.pages import mapping_gaps_text
from handlers.results import display_file_name
from handlers.runs import (
    result_by_starter,
    completeness_text,
    explanation_text,
    get_run,
    no_records_text,
    politeness_text,
    recollect_text,
    soft_errors_text,
    summarize_run,
    timeline_text,
)

PAGE_SIZE = 20


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f} %"
    except (TypeError, ValueError):
        return "n/a"


def _n(value: Any) -> Any:
    """A counter the service leaves out or sends as null while a run is young reads as 0."""
    return 0 if value is None else value


def _job_link(job_id: str) -> str:
    if job_id.startswith("page-"):
        # A re-collect started on Run detail (A45) has no platform job.
        return "the Run detail page"
    return f"/jobs/{job_id}" if job_id else ""


def list_runs(ctx: ActionContext) -> dict[str, Any]:
    client = ctx.profile.client()
    query: dict[str, Any] = {"limit": PAGE_SIZE}
    state = ctx.text("state_filter") or ctx.text("state")
    if state and state != "any":
        query["state"] = state
    for key in ("site", "cursor"):
        value = ctx.text(key)
        if value:
            query[key] = value
    if ctx.connection_id:
        query["connection_id"] = ctx.connection_id
    answer = as_dict(client.get("/v1/runs", query=query))
    items = [as_dict(row) for row in as_list(answer.get("items"))]
    rows = []
    lines: dict[str, Any] = {}
    for index, item in enumerate(items):
        platform = as_dict(item.get("platform"))
        row = {
            "run_id": as_text(item.get("run_id")),
            "site": as_text(item.get("site")),
            "profile": as_text(item.get("profile_name")),
            "state": as_text(item.get("state")),
            "outcome": item.get("outcome"),
            "stop_reason": item.get("stop_reason"),
            "started": item.get("created_at"),
            "finished": item.get("finished_at"),
            "pages": item.get("pages_fetched"),
            "records": item.get("records_extracted"),
            "fill_rate": _pct(item.get("required_fill_rate")),
            "events_pending": item.get("events_pending"),
            "platform_job": _job_link(as_text(platform.get("job_id"))),
            "connection_id": ctx.connection_id,
        }
        rows.append(row)
        if index < 10:
            outcome = f", {row['outcome']}" if row["outcome"] else ""
            lines[f"run_{index + 1}"] = (
                f"{row['run_id']} · {row['site']} · {row['state']}{outcome} · started "
                f"{row['started']} · {count(_n(row['pages']), 'page')} · {count(_n(row['records']), 'record')} · fill "
                f"{row['fill_rate']}"
            )
    summary = {"runs_shown": len(rows), **lines}
    if answer.get("next_cursor"):
        summary["more"] = f"More runs exist; enter cursor {answer['next_cursor']} to see them."
    if not rows:
        summary["runs_shown"] = 0
        summary["note"] = (
            "No runs for this profile yet. Start one with the 'Scrape a site' workflow."
        )
    return {**summary, "details": {"runs": rows, "next_cursor": answer.get("next_cursor")}}


def _template_line(row: dict[str, Any]) -> str:
    parts = [
        f"{as_text(row.get('template_id'))}: {as_text(row.get('status'))}",
        f"mapping v{row.get('mapping_version')}",
        count(_n(row.get("record_count")), "record"),
        f"{_n(row.get('unextracted_count'))} unextracted",
    ]
    if row.get("stored_pages_pending"):
        parts.append(f"{_n(row.get('stored_pages_pending'))} waiting for re-extraction")
    if row.get("open_mapping_request_id"):
        parts.append(f"open request {row.get('open_mapping_request_id')}")
    return ", ".join(parts)


def _event_line(row: dict[str, Any]) -> str:
    claim = as_dict(row.get("claim"))
    holder = as_text(as_dict(claim.get("claimant")).get("job_id"))
    delivery = as_dict(row.get("delivery"))
    progress = as_dict(row.get("progress"))
    line = (
        f"{as_text(row.get('event_id'))} {as_text(row.get('event_type'))}: "
        f"{as_text(row.get('state'))}, delivery #{delivery.get('generation')}"
    )
    if holder:
        line += f", claimed by job {holder} (/jobs/{holder})"
    if progress.get("last_stage"):
        line += f", last stage {progress.get('last_stage')}"
    if delivery.get("last_error"):
        line += f", last delivery error {delivery.get('last_error')}"
    return line


def _challenged_pages(protection: dict[str, Any], base_url: str) -> str:
    """The job's protection traces (first 5, `protection.traces`) in plain words (A23)."""
    traces = [as_dict(t) for t in as_list(protection.get("traces"))][:5]
    if not traces:
        if _n(protection.get("challenges_met")):
            return (
                "The service kept no trace of these challenges (it predates protection "
                "traces); 'protection' above has the counts."
            )
        return "none"
    lines = [
        f"{index}) {protection_text.describe_trace(trace)}"
        for index, trace in enumerate(traces, start=1)
    ]
    if _n(protection.get("challenges_met")) > len(traces):
        lines.append(
            f"(The first {len(traces)} of {count(protection.get('challenges_met'), 'challenged page')}.)"
        )
    if any(trace.get("screenshot_id") for trace in traces):
        lines.append(
            f"To look at a screenshot, request GET {base_url}/v1/screenshots/<id> with the "
            "profile's service token in the Authorization header; this page cannot show files."
        )
    return "\n".join(lines)


#: Run error codes in words (contract `run_error_code`); others are shown as sent.
ERROR_LABELS = {"page_fetch_failed": "page not loaded"}
_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2, "debug": 3}


def _by_severity(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Errors before warnings (such as `page_fetch_failed`, A37) before debug lines."""
    return sorted(errors, key=lambda e: _SEVERITY_ORDER.get(as_text(e.get("severity")), 2))


def _error_line(error: dict[str, Any]) -> str:
    code = as_text(error.get("code"))
    return bounded(f"{ERROR_LABELS.get(code, code)}: {as_text(error.get('message'))}", 200)


def show_run(ctx: ActionContext) -> dict[str, Any]:
    run_id = ctx.require_text("run_id", "Run id")
    run = get_run(ctx, run_id)
    progress = as_dict(run.get("progress"))
    counters = as_dict(run.get("counters"))
    protection = as_dict(run.get("protection"))
    secrets = as_dict(run.get("secrets"))
    config = as_dict(run.get("config"))
    by_class = as_dict(counters.get("pages_by_class"))
    passed = as_dict(protection.get("passed_by_step"))
    templates = [as_dict(t) for t in as_list(run.get("templates"))]
    events = [as_dict(e) for e in as_list(run.get("events"))]
    errors = [as_dict(e) for e in as_list(run.get("errors"))]
    open_requests = [
        as_dict(m)
        for m in as_list(run.get("mapping_requests"))
        if as_dict(m).get("status") == "open"
    ]
    job = as_text(as_dict(run.get("platform")).get("job_id"))
    state = as_text(run.get("state"))
    summary: dict[str, Any] = {
        # The platform shows 12 plain values: the starting job shares the state line,
        # so the result descriptor has a line of its own (A40).
        "state": state
        + (
            " — press 'Resume run' to send the profile's secrets again"
            if state == "waiting_for_credentials"
            else ""
        )
        + (
            ". Started from the Run detail page ('Collect missed pages again')"
            if job.startswith("page-")
            else f". Started by platform job {_job_link(job)}"
            if job
            else ". Not started from a workflow"
        )
        + (
            # A67: a cancel of the waiting job does not reach the scraper.
            ". If that job was cancelled, this crawl still runs: stop it with Stop on its "
            "row on Runs (Stop run)"
            if state in {"queued", "running"} and result_by_starter(run) and job
            else ""
        )
        + (f". {recollect_text(run)}" if recollect_text(run) else ""),
        # ROUND2 §6/§7: the service's sentence on why it stopped and what to do.
        "outcome": " ".join(
            part
            for part in (
                f"{run.get('outcome') or 'not final yet'} (stop reason: {run.get('stop_reason') or '—'}).",
                explanation_text(run),
            )
            if part
        ),
        "progress": (
            f"discovery {progress.get('discovery')}, {_n(progress.get('queue_pending'))} queued, "
            f"{_n(progress.get('in_progress'))} in progress, "
            f"{count(_n(progress.get('open_mapping_requests')), 'open mapping request')}, "
            f"{count(_n(progress.get('pending_reextraction')), 'page')} waiting for "
            f"re-extraction, result {progress.get('result_status')}"
            + "".join(
                f". {part}"
                for part in (
                    politeness_text(run).rstrip("."),
                    f"Timeline: {timeline_text(run)}" if timeline_text(run) else "",
                )
                if part
            )
        ),
        "pages": (
            f"{_n(counters.get('pages_fetched'))} fetched of max {config.get('max_pages')} "
            f"({_n(counters.get('http_fetches'))} HTTP, {_n(counters.get('browser_fetches'))} browser, "
            f"{count(_n(counters.get('retries')), 'retry', 'retries')}); extracted {_n(by_class.get('extracted'))}, "
            f"incomplete {_n(by_class.get('incomplete'))}, no template {_n(by_class.get('no_template'))}, "
            f"blocked {_n(by_class.get('blocked'))}, failed {_n(by_class.get('fetch_failed'))}"
            + (f". {soft_errors_text(run)}" if soft_errors_text(run) else "")
        ),
        "records": (
            (f"{completeness_text(run)} " if completeness_text(run) else "")
            + f"{count(_n(counters.get('records_extracted')), 'record')}, required-field fill "
            f"{_pct(counters.get('required_fill_rate'))}, "
            f"{count(_n(counters.get('unextracted')), 'URL')} unextracted"
            + (
                f", {counters['records_dropped_over_limit']} dropped over max_records"
                if counters.get("records_dropped_over_limit")
                else ""
            )
        ),
        # The platform shows the first 12 plain values of an answer: repairs and the
        # proxy tier share a line with templates and protection to leave room for the
        # challenged pages (A23).
        "templates": ("; ".join(_template_line(t) for t in templates) or "none yet")
        + (
            f". Repairs: {_n(counters.get('repairs_started'))} started, "
            f"{_n(counters.get('repairs_succeeded'))} succeeded; open requests: "
            f"{', '.join(as_text(m.get('mapping_request_id')) for m in open_requests) or 'none'}"
        ),
        "protection": (
            f"proxy tier now {counters.get('current_proxy_tier') or 'none'}"
            + (
                " (requests by tier "
                + ", ".join(
                    f"{k}: {v}" for k, v in as_dict(counters.get("proxy_requests_by_tier")).items()
                )
                + ")"
                if as_dict(counters.get("proxy_requests_by_tier"))
                else ""
            )
            + f"; {count(_n(protection.get('challenges_met')), 'challenge')}; passed by "
            + ", ".join(f"{k} {v}" for k, v in passed.items() if v)
            + f"; frame not found {_n(protection.get('frame_not_found'))}; failed {_n(protection.get('failed'))}; "
            f"clicks {_n(protection.get('clicks_used'))}/{_n(protection.get('clicks_limit'))}; solves "
            f"{_n(protection.get('solves_used'))}/{_n(protection.get('solves_limit'))}"
            + (
                f"; moved to the next proxy tier {count(protection['tier_escalations'], 'time')}"
                if protection.get("tier_escalations")
                else ""
            )
        ),
        "challenged_pages": _challenged_pages(protection, ctx.profile.client().base_url),
        "events": "; ".join(_event_line(e) for e in events) or "none",
        "errors": ("; ".join(_error_line(e) for e in _by_severity(errors)[:5]) or "none")
        + (f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""),
        "result": result_text(run),
    }
    # Not "secrets": the platform blanks the value of any key that looks like one.
    summary["stored_profile_data"] = (
        f"proxy/solver {secrets.get('proxy_and_solver')}, signing secret {secrets.get('signing_secret')}"
    )
    return {
        **summary,
        "details": {
            "summary": summarize_run(run),
            "templates": templates,
            "events": [
                {
                    "event_id": e.get("event_id"),
                    "event_type": e.get("event_type"),
                    "state": e.get("state"),
                    "generation": as_dict(e.get("delivery")).get("generation"),
                    "claimant_job_id": as_text(
                        as_dict(as_dict(e.get("claim")).get("claimant")).get("job_id")
                    ),
                    "last_stage": as_dict(e.get("progress")).get("last_stage"),
                }
                for e in events
            ],
            "protection": protection,
            "counters": counters,
            "explanation": as_dict(run.get("explanation")) or None,
            "completeness": as_dict(run.get("completeness")) or None,
            "comparison": as_dict(run.get("comparison")) or None,
            "timeline": as_list(run.get("timeline")),
            "profile": as_dict(run.get("profile")),
            "errors": errors[:50],
            "result": as_dict(run.get("result")) or None,
        },
    }


def show_unextracted(ctx: ActionContext) -> dict[str, Any]:
    run_id = ctx.require_text("run_id", "Run id")
    client = ctx.profile.client()
    query: dict[str, Any] = {"limit": 50}
    reason = ctx.text("reason")
    if reason and reason != "any":
        query["reason"] = reason
    cursor = ctx.text("cursor")
    if cursor:
        query["cursor"] = cursor
    answer = as_dict(client.get(f"/v1/runs/{client.segment(run_id)}/unextracted", query=query))
    items = [as_dict(row) for row in as_list(answer.get("items"))]
    summary: dict[str, Any] = {
        "unextracted_total": answer.get("total", len(items)),
        "shown": len(items),
    }
    for index, row in enumerate(items[:10]):
        key = f" [{row.get('source_key')}]" if row.get("source_key") else ""
        summary[f"url_{index + 1}"] = bounded(
            f"{row.get('url')}{key}: {row.get('reason')}"
            + (
                f" (missing {', '.join(as_list(row.get('missing_fields')))})"
                if row.get("missing_fields")
                else ""
            )
            + (f" - {mapping_gaps_text(row)}" if mapping_gaps_text(row) else ""),
            300,
        )
    if answer.get("next_cursor"):
        summary["more"] = (
            f"More URLs exist; enter cursor {answer['next_cursor']} to see the next page."
        )
    summary["download"] = (
        f"The whole list as JSON Lines: GET {client.base_url}/v1/runs/{run_id}/unextracted with "
        "'Accept: application/x-ndjson' and the profile's service token."
    )
    return {**summary, "details": {"items": items, "next_cursor": answer.get("next_cursor")}}


def _result_job(run: dict[str, Any]) -> tuple[str, bool]:
    """The platform job of 'Scraper events' that stored the result file.

    That is the job that recorded the `processed` stage of the run's
    `run_completed` event (stored right after Get result); without a recorded
    stage, the job holding or last holding its claim. Not `run.platform.job_id`:
    that is the 'Scrape a site' job that started the run.
    """
    events = [
        as_dict(e)
        for e in as_list(run.get("events"))
        if as_text(as_dict(e).get("event_type")) == "run_completed"
    ]
    for event in reversed(events):
        stages = [as_dict(s) for s in as_list(as_dict(event.get("progress")).get("stages"))]
        for stage in reversed(stages):
            if as_text(stage.get("stage")) == "processed" and as_text(stage.get("claimant_job_id")):
                return as_text(stage.get("claimant_job_id")), True
    for event in reversed(events):
        holder = as_text(as_dict(as_dict(event.get("claim")).get("claimant")).get("job_id"))
        if holder:
            return holder, False
    return "", False


def result_text(run: dict[str, Any]) -> str:
    """The result descriptor on Run detail (A40): file, records, and where to get it."""
    run_id = as_text(run.get("run_id"))
    result = as_dict(run.get("result"))
    state = as_text(run.get("state"))
    started_by = as_text(as_dict(run.get("platform")).get("job_id"))
    waits = result_by_starter(run) and bool(started_by)
    if not result:
        if state == "failed":
            return "none: the run failed (see errors)."
        if state == "completed":
            return "none: " + (no_records_text(run) or "the run has no result file.")
        if waits:
            return (
                f"not yet: the 'Scrape a site' job {_job_link(started_by)} waits for this run "
                "and stores the files when it ends."
            )
        return (
            "not yet: when the run ends, the service reports it and a 'Scraper events' job "
            "stores the result file."
        )
    name = display_file_name(as_text(result.get("file_name")), run_id)
    csv = as_dict(result.get("csv"))
    files = f"{name} ({human_bytes(result.get('size_bytes'))})"
    if csv:
        csv_name = display_file_name(as_text(csv.get("file_name")), run_id)
        files += f" and {csv_name} ({human_bytes(csv.get('size_bytes'))})"
    line = (
        f"{files}: {count(_n(result.get('record_count')), 'record')}"
        + (
            f", kept by the service until {as_text(result.get('expires_at'))[:10]}"
            if result.get("expires_at")
            else ""
        )
        + ". "
    )
    result_job, stored = _result_job(run)
    if stored:
        line += (
            f"In Flow Steward: the 'Scraper events' job {_job_link(result_job)}, panel "
            "Result ("
            + ("result_file, result_csv" if csv else "result_file")
            + " and the first rows)."
        )
    elif waits:
        line += (
            f"In Flow Steward: the 'Scrape a site' job {_job_link(started_by)} that started "
            "this run waited for it and stores the files, panel Result ("
            + ("result_file, result_csv" if csv else "result_file")
            + ", the first rows and the run summary). If that job failed or was cancelled, "
            "the service still keeps the files until the date above."
        )
    elif result_job:
        line += (
            f"The 'Scraper events' job {_job_link(result_job)} took the event but has not "
            "stored the file yet; if it failed, use 'Send again' under Events on Run detail."
        )
    else:
        line += (
            "No 'Scraper events' job has stored it yet; check the webhook, then 'Send again' "
            "under Events on Run detail."
        )
    return line


def result_download(ctx: ActionContext) -> dict[str, Any]:
    run_id = ctx.require_text("run_id", "Run id")
    client = ctx.profile.client()
    try:
        service = as_dict(client.get(f"/v1/runs/{client.segment(run_id)}/result"))
    except ScraperError as exc:
        if exc.service_code in {"result_not_ready", "result_not_available", "result_expired"}:
            return {"available": False, "message": exc.message}
        raise
    run = get_run(ctx, run_id)
    result_job, stored = _result_job(run)
    started_by = as_text(as_dict(run.get("platform")).get("job_id"))
    if not stored and result_by_starter(run) and started_by:
        # Scrape a site 0.1.23+ waited for the run and stored the files in its own job.
        result_job, stored = started_by, True
        where = (
            f"The 'Scrape a site' job {started_by} that started this run waited for it and "
            f"stored the files: open {_job_link(started_by)}, panel Result."
        )
    else:
        where = ""
    return {
        "available": True,
        "file_name": display_file_name(as_text(service.get("file_name")), run_id),
        "size": f"{int(service.get('size_bytes') or 0) / 1048576:.2f} MiB",
        "records": service.get("record_count"),
        "sha256": as_text(service.get("sha256")),
        "expires_at": as_text(service.get("expires_at")),
        "in_flow_steward": where
        or (
            f"The 'Scraper events' job {result_job} stored the file as an artifact: open "
            f"{_job_link(result_job)} and its files in Job Details."
            if stored
            else f"The 'Scraper events' job {result_job} took the run_completed event but "
            "recorded no 'processed' stage, so the file may not be stored: open "
            f"{_job_link(result_job)}. If it failed, use 'Send again' under Events on Run detail."
            if result_job
            else "No 'Scraper events' job has handled this run's run_completed event yet, "
            "so the file is not stored in Flow Steward. Resend the event (Run detail, "
            "'Send again' under Events) once the webhook works."
        ),
        "page_limits": (
            "This page cannot hand out the file itself: it has no download, and the "
            "service token is never shown again after it was saved."
        ),
        "from_the_service": (
            f"GET {client.base_url}{as_text(service.get('download_path'))} with the profile's "
            "service token (resumable with Range; check the SHA-256 above)."
            + (
                f" The CSV: GET {client.base_url}{as_text(as_dict(service.get('csv')).get('download_path'))}."
                if as_dict(service.get("csv")).get("download_path")
                else ""
            )
        ),
        "result_job": _job_link(result_job) or "none yet",
        "started_by_job": _job_link(started_by) or "not started from a workflow",
    }
