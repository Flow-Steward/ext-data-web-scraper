"""Page queries that read the scraper service through the page's profile (ADR 0067).

The Runs, Run detail and Service pages declare `connection`; the platform shows its
own profile selector beside the project selector and sends the chosen scraper
profile with every query (root `target.connection`). These queries only read.
A failing service is a domain failure (`ok: false`), so the platform's circuit
breaker is not tripped by a service outage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from handlers.common import EXTENSION_ID, as_dict, as_list, as_text, bounded, count, human_bytes
from handlers.errors import ScraperError
from handlers.profile import Profile, profile_from_connection
from handlers.results import display_file_name
from handlers.runs import (
    STOP_REASON_TEXT,
    completeness_text,
    explanation_text,
    no_records_text,
    politeness_text,
    record_count_of,
    result_by_starter,
    soft_errors_text,
    timeline_text,
)
from handlers.storage import STORAGE_PATH, _storage_lines

#: A query runs at most 30 seconds on the platform; each service call gets less.
QUERY_TIMEOUT_SECONDS = 20
RUNS_LIMIT = 100
SITE_PARAM = re.compile(r"^[a-z0-9.-]{1,253}(:[0-9]{1,5})?$")
MISSED_LIMIT = 100
RUN_ID = re.compile(r"^run_[A-Za-z0-9]{1,64}$")
ACTIVE_STATES = {"queued", "running", "waiting_for_credentials", "stopping"}
STATE_WORDS = {
    "queued": "queued",
    "running": "running",
    "waiting_for_credentials": "waiting for the profile's secrets",
    "stopping": "stopping",
    "completed": "completed",
    "failed": "failed",
}
#: Why it stopped, short, for the table.
STOP_SHORT = {
    "finished": "finished",
    "stopped": "stopped by hand",
    "max_pages": "page limit",
    "max_records": "record limit",
    "max_duration": "time limit",
    "max_result_size": "size limit",
    "blocked": "blocked by the site",
    "site_unavailable": "site stopped answering",
    "stalled": "stalled",
}
EVENT_WORDS = {
    "run_completed": "run completed",
    "mapping_required": "mapping repair",
    "problem": "problem",
    "run_failed": "run failed",
}
#: A claim this old whose job has not finished the event is offered to send again.
STUCK_CLAIM_MINUTES = 30


def _connection(payload: dict[str, Any]) -> dict[str, Any]:
    """The page's connection: SDK 0.5.0 `connection_from_payload`, else the root target."""
    try:
        from flowsteward_extension_sdk import connection_from_payload
    except ImportError:  # an SDK before 0.5.0: read the envelope as ADR 0067 defines it
        return as_dict(as_dict(payload.get("target")).get("connection"))
    connection = connection_from_payload(payload)
    if connection is None:
        return {}
    return {
        "connection_id": connection.connection_id,
        "config": dict(connection.config),
        "secrets": dict(connection.secrets),
    }


@dataclass
class PageQuery:
    payload: dict[str, Any]
    profile: Profile
    connection_id: str
    project_id: str
    params: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> PageQuery:
        connection = _connection(payload)
        if not connection:
            raise ScraperError(
                "invalid_connection",
                "Choose a scraper profile in the selector at the top of the page. Profiles are "
                "created on the Profiles page.",
                no_external_effect=True,
            )
        query = as_dict(payload.get("query"))
        context = as_dict(query.get("context"))
        runtime = as_dict(payload.get("runtime_context"))
        return cls(
            payload=payload,
            profile=profile_from_connection(connection),
            connection_id=as_text(connection.get("connection_id")),
            project_id=as_text(
                context.get("project_id") or runtime.get("project_id") or payload.get("project_id")
            ),
            params=as_dict(query.get("params")),
        )

    def client(self):
        return self.profile.client(QUERY_TIMEOUT_SECONDS)

    def page_link(self, page: str, **params: str) -> str:
        """A link to one of this extension's pages with the same project and profile."""
        query = {"project_id": self.project_id, "extension_connection": self.connection_id}
        query = {key: value for key, value in query.items() if value}
        query["extension_page"] = page
        query.update({f"p.{key}": value for key, value in params.items()})
        return f"/extensions/{EXTENSION_ID}?{urlencode(query, quote_via=quote)}"

    def run_link(self, run_id: str) -> str:
        return self.page_link("run_detail", run_id=run_id)

    def run_id_param(self) -> str:
        """`p.run_id` of the page URL: untrusted, so only a run id shape is taken."""
        value = as_text(self.params.get("run_id"))
        return value if RUN_ID.match(value) else ""


def _when(value: Any) -> str:
    text = as_text(value)
    return f"{text[:10]} {text[11:16]} UTC" if len(text) >= 16 and text[10] == "T" else text


def _n(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ----------------------------------------------------------------------- Runs


def _filters(page: PageQuery) -> dict[str, str]:
    """`p.state` / `p.site` of the page URL (the filter links); untrusted, so checked."""
    state = as_text(page.params.get("state")).lower()
    site = as_text(page.params.get("site")).lower()
    filters = {}
    if state in STATE_WORDS:
        filters["state"] = state
    if SITE_PARAM.match(site):
        filters["site"] = site
    return filters


def _list_runs(page: PageQuery) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"limit": RUNS_LIMIT, **_filters(page)}
    if page.connection_id:
        query["connection_id"] = page.connection_id
    answer = as_dict(page.client().get("/v1/runs", query=query))
    return [as_dict(row) for row in as_list(answer.get("items"))]


#: Why a queued run waits (`queued_reason.code`, README 4.13), short, for the table.
QUEUED_SHORT = {
    "same_site_running": "waiting: another run of this site is running",
    "no_free_slot": "waiting for a free slot",
}


def _missed(item: dict[str, Any]) -> int:
    return _n(as_dict(item.get("completeness")).get("not_collected"))


def _why_stopped(item: dict[str, Any]) -> str:
    state = as_text(item.get("state"))
    if state == "queued":
        code = as_text(as_dict(item.get("queued_reason")).get("code"))
        return QUEUED_SHORT.get(code, "")
    if state in ACTIVE_STATES:
        return ""
    missed = _missed(item)
    tail = f", {count(missed, 'page')} missed" if missed else ""
    if state == "failed":
        return "failed" + tail
    reason = as_text(item.get("stop_reason"))
    words = STOP_SHORT.get(reason, reason.replace("_", " "))
    outcome = as_text(item.get("outcome"))
    return (f"{words}, {outcome}" if outcome and words else words or outcome) + tail


def _progress(item: dict[str, Any]) -> str:
    """'M of N' from the run list's completeness (service 2026-11-14, README 4.9)."""
    state = as_text(item.get("state"))
    completeness = as_dict(item.get("completeness"))
    text = as_text(as_dict(completeness.get("progress")).get("text"))
    if state in ACTIVE_STATES and text:
        return text
    found = completeness.get("target_pages_found")
    if found:
        return f"{_n(completeness.get('pages_collected'))} of {_n(found)}"
    pages = _n(item.get("pages_fetched"))
    return count(pages, "page") + (" so far" if state in ACTIVE_STATES else "")


def _run_row(item: dict[str, Any]) -> dict[str, Any]:
    state = as_text(item.get("state"))
    missed = _missed(item)
    return {
        "run_id": as_text(item.get("run_id")),
        "site": as_text(item.get("site")),
        "state": state,
        "progress": _progress(item),
        "records": _n(item.get("records_extracted")),
        "why_stopped": _why_stopped(item),
        "started": as_text(item.get("created_at")),
        "finished": as_text(item.get("finished_at")),
        "outcome": as_text(item.get("outcome")),
        "missed": missed,
        # Row buttons (visible_if): only where the action applies to the run's state.
        "can_stop": state in {"queued", "running"},
        # The service's only resumable state: it restarted without the run's secrets.
        "can_resume": state == "waiting_for_credentials",
        # From the list itself (`completeness.not_collected`): no read per run.
        "can_recollect": state in {"completed", "failed"} and missed > 0,
    }


def runs_table(payload: dict[str, Any]) -> dict[str, Any]:
    """Runs grid: newest first, one row per run of the selected profile, one service call."""
    page = PageQuery.from_payload(payload)
    rows = [_run_row(item) for item in _list_runs(page)]
    return {"items": rows, "total": len(rows)}


def _get_run(page: PageQuery, run_id: str) -> dict[str, Any] | None:
    client = page.client()
    try:
        return as_dict(client.get(f"/v1/runs/{client.segment(run_id)}"))
    except ScraperError as exc:
        if exc.service_code == "run_not_found":
            return None
        raise


def _no_run(page: PageQuery) -> str:
    return (
        "Open a run from the [Runs]("
        + page.page_link("runs")
        + ") page: each run there links here."
    )


def _result_section(page: PageQuery, run: dict[str, Any]) -> list[str]:
    from handlers.run_page import _result_job
    from handlers.runs import result_by_starter

    result = as_dict(run.get("result"))
    state = as_text(run.get("state"))
    run_id = as_text(run.get("run_id"))
    starter = as_text(as_dict(run.get("platform")).get("job_id"))
    waits = result_by_starter(run) and bool(starter)
    if not result:
        if state == "failed":
            return ["No result: the run failed."]
        if state == "completed":
            return [no_records_text(run) or "The run has no result file."]
        if waits:
            return [
                f"Not yet. [The Scrape a site job](/jobs/{quote(starter)}) waits for this run "
                "and stores the files when it ends."
            ]
        return [
            "Not yet. When the run ends, the service reports it and a *Scraper events* job "
            "stores the result."
        ]
    lines = [
        f"- **{display_file_name(as_text(result.get('file_name')), run_id)}** (JSON Lines, "
        f"{human_bytes(result.get('size_bytes'))})"
    ]
    csv = as_dict(result.get("csv"))
    if csv:
        lines.append(
            f"- **{display_file_name(as_text(csv.get('file_name')), run_id)}** (CSV for Excel, "
            f"{human_bytes(csv.get('size_bytes'))})"
        )
    lines.append(f"- {count(_n(result.get('record_count')), 'record')}")
    job, stored = _result_job(run)
    if stored:
        lines.append(
            f"- In Flow Steward: [the Scraper events job](/jobs/{quote(job)}), panel *Result* "
            "(open or download the files there)."
        )
    elif waits:
        lines.append(
            f"- In Flow Steward: [the Scrape a site job](/jobs/{quote(starter)}) that started "
            "this run waited for it and stores the files, panel *Result* (open or download "
            "them there)."
        )
    elif job:
        lines.append(
            f"- [The Scraper events job](/jobs/{quote(job)}) took the event but has not stored "
            "the files; see *Events* below."
        )
    else:
        lines.append("- No Scraper events job has stored the files yet; see *Events* below.")
    if result.get("expires_at"):
        lines.append(
            f"- The service keeps its own copy until {as_text(result.get('expires_at'))[:10]}."
        )
    return lines


def _missed_section(run: dict[str, Any]) -> list[str]:
    completeness = as_dict(run.get("completeness"))
    by_reason = as_dict(completeness.get("not_collected_by_reason"))
    total = _n(as_dict(run.get("counters")).get("unextracted"))
    if not total and not by_reason:
        return ["None."]
    lines = [f"{count(total, 'page')} ended without a record."] if total else []
    if by_reason:
        lines.append(
            "By reason: "
            + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in by_reason.items() if v)
            + "."
        )
    if as_text(run.get("state")) in {"completed", "failed"}:
        lines.append(
            "To try them again, press *Re-collect missed pages* on this run's row on the Runs "
            "page. The table *Missed pages* below lists them."
        )
    return lines


#: `counters.links_skipped` keys (README 4.11) in a few words, for one short line.
LINK_RULE_SHORT = {
    "sensitive_path": "account/cart/wishlist",
    "facet_params": "filters",
    "action_params": "action links",
    "file_extension": "files",
    "scheme": "mail/phone/app links",
    "messenger_social": "share buttons",
    "nofollow": "nofollow",
    "trap_budget": "trap patterns",
    "canonical": "duplicates",
    "dynamic_params": "random parameters",
}


def _thousands(number: int) -> str:
    return f"{number:,}".replace(",", " ")


def links_skipped_text(run: dict[str, Any]) -> str:
    """'15 500 account/cart/wishlist, 11 340 filters, …', largest first; empty for none."""
    skipped = {
        rule: _n(number)
        for rule, number in as_dict(as_dict(run.get("counters")).get("links_skipped")).items()
        if _n(number) and rule != "canonical"  # duplicate pages have their own line
    }
    ordered = sorted(skipped.items(), key=lambda item: -item[1])
    return ", ".join(
        f"{_thousands(number)} {LINK_RULE_SHORT.get(rule, rule.replace('_', ' '))}"
        for rule, number in ordered
    )


def queued_text(run: dict[str, Any]) -> str:
    """Why a queued run waits (README 4.13), in the service's words or ours."""
    if as_text(run.get("state")) != "queued":
        return ""
    reason = as_dict(run.get("queued_reason"))
    message = as_text(reason.get("message"))
    if message:
        return message if message.endswith(".") else message + "."
    return {
        "same_site_running": "It waits: another run of this site is running.",
        "no_free_slot": "It waits for a free slot.",
    }.get(as_text(reason.get("code")), "")


def cancelled_job_hint(page: PageQuery) -> str:
    """A67: Flow Steward 0.9.18 tells an extension nothing when a job is cancelled, so a
    crawl whose waiting Scrape a site job was cancelled goes on until it ends."""
    return (
        "If that job (or the workflow that called it) was cancelled, this crawl still runs: "
        "a cancel does not reach the scraper. Stop it with *Stop* on its row on the "
        f"[Runs]({page.page_link('runs')}) page."
    )


def run_report(payload: dict[str, Any]) -> dict[str, Any]:
    """Run detail: everything about one run, without pressing anything.

    Sections in the owner's order: Status, Progress, Why it stopped / what to do,
    Results, Missed pages, Timeline (Events and the missed-page list are tables below).
    """
    page = PageQuery.from_payload(payload)
    run_id = page.run_id_param()
    if not run_id:
        return {"content": _no_run(page)}
    run = _get_run(page, run_id)
    if run is None:
        return {
            "content": f"The service no longer has this run (its records are removed 30 days "
            f"after it ends, or by a clean-up). {_no_run(page)}"
        }
    state = as_text(run.get("state"))
    counters = as_dict(run.get("counters"))
    progress = as_dict(run.get("progress"))
    config = as_dict(run.get("config"))
    job = as_text(as_dict(run.get("platform")).get("job_id"))

    status = [
        f"**{as_text(run.get('site'))}** — {STATE_WORDS.get(state, state)}"
        + (f", {as_text(run.get('outcome'))}" if run.get("outcome") else "")
        + ".",
        f"Started {_when(run.get('created_at'))}"
        + (f", finished {_when(run.get('finished_at'))}" if run.get("finished_at") else "")
        + ".",
    ]
    waiting = queued_text(run)
    if waiting:
        status.append(waiting)
    if job.startswith("page-"):
        status.append("Started from the Runs page (re-collect).")
    elif job:
        status.append(f"Started by [the Scrape a site job](/jobs/{quote(job)}).")
        if state in ACTIVE_STATES and result_by_starter(run):
            status.append(cancelled_job_hint(page))
    parent = as_dict(run.get("recollect_of"))
    if parent:
        parent_id = as_text(parent.get("run_id"))
        status.append(
            f"It collects again {count(parent.get('url_count'), 'missed page')} of "
            f"[an earlier run]({page.run_link(parent_id)})."
        )
    children = [as_dict(c) for c in as_list(run.get("recollected_by"))]
    if children:
        status.append(
            "Its missed pages were collected again: "
            + ", ".join(
                f"[run started {_when(c.get('created_at'))}]({page.run_link(as_text(c.get('run_id')))})"
                f" ({STATE_WORDS.get(as_text(c.get('state')), as_text(c.get('state')))})"
                for c in children
            )
            + "."
        )

    pages_line = f"{count(_n(counters.get('pages_fetched')), 'page')} loaded"
    if config.get("max_pages"):
        pages_line += f" of at most {config.get('max_pages')}"
    progress_lines = [
        completeness_text(run) or f"{pages_line}.",
        f"{pages_line}; {count(record_count_of(run), 'record')}"
        + (
            f"; {_n(progress.get('queue_pending'))} pages still waiting"
            if state in ACTIVE_STATES
            else ""
        )
        + ".",
    ]
    duplicates = _n(as_dict(counters.get("pages_by_class")).get("duplicate"))
    if duplicates:
        progress_lines.append(
            f"{count(duplicates, 'page')} skipped as duplicates: another address of a page "
            "already collected (by its canonical address)."
        )
    skipped = links_skipped_text(run)
    if skipped:
        progress_lines.append(f"Skipped links: {skipped}.")
    for line in (soft_errors_text(run), politeness_text(run)):
        if line:
            progress_lines.append(line[0].upper() + line[1:])

    explanation = explanation_text(run)
    stop = as_text(run.get("stop_reason"))
    if explanation:
        why = explanation
    elif stop:
        why = f"It stopped because {STOP_REASON_TEXT.get(stop, stop)}."
    elif state in ACTIVE_STATES:
        why = "Still running."
    else:
        why = "The service gave no reason."
    if state == "waiting_for_credentials":
        why += " Press *Resume* on its row on the Runs page to send the profile's secrets again."
    errors = [
        as_dict(e) for e in as_list(run.get("errors")) if as_dict(e).get("severity") == "error"
    ]
    if state == "failed" and as_dict(run.get("error")):
        why += f" {bounded(as_text(as_dict(run.get('error')).get('message')), 300)}"

    out = ["### Status", "", *[f"- {line}" for line in status]]
    out += ["", "### Progress", "", *[f"- {line}" for line in progress_lines]]
    out += ["", "### Why it stopped / what to do", "", why]
    if errors:
        out += [""] + [
            f"- {bounded(as_text(e.get('message')) or as_text(e.get('code')), 300)}"
            for e in errors[:5]
        ]
    out += ["", "### Results", "", *_result_section(page, run)]
    out += ["", "### Missed pages", "", " ".join(_missed_section(run))]
    timeline = timeline_text(run)
    if timeline:
        out += ["", "### Timeline", "", timeline]
    out += ["", f"[Back to Runs]({page.page_link('runs')})"]
    return {"content": "\n".join(out)}


def _event_row(page: PageQuery, event: dict[str, Any], now: str) -> dict[str, Any]:
    state = as_text(event.get("state"))
    delivery = as_dict(event.get("delivery"))
    claim = as_dict(event.get("claim"))
    job = as_text(as_dict(claim.get("claimant")).get("job_id"))
    claimed_at = as_text(claim.get("claimed_at"))
    stuck = (
        state == "claimed"
        and bool(claimed_at)
        and _minutes_between(claimed_at, now) >= STUCK_CLAIM_MINUTES
    )
    if state == "handled":
        words = "handled"
    elif state == "stale":
        words = "failed: the job that took it did not finish"
    elif state == "claimed":
        words = f"being handled by job {job}" + (
            "; if that job failed, send it again" if stuck else ""
        )
    elif state == "pending" and delivery.get("last_error"):
        words = f"not delivered: {bounded(as_text(delivery.get('last_error')), 120)}"
    elif state == "pending":
        words = "waiting to be sent"
    else:
        words = "delivered, not taken by a job yet"
    can_send = (
        state == "stale" or stuck or (state == "pending" and bool(delivery.get("last_error")))
    )
    if state == "delivered" and as_text(delivery.get("delivered_at")):
        can_send = (
            _minutes_between(as_text(delivery.get("delivered_at")), now) >= STUCK_CLAIM_MINUTES
        )
    return {
        # 0.9.18 grid link (path_field): the Scraper events job that took the event.
        "job": "Scraper events job" if job else "",
        "job_path": f"/jobs/{quote(job)}" if job else "",
        "event": EVENT_WORDS.get(
            as_text(event.get("event_type")), as_text(event.get("event_type"))
        ),
        "created": as_text(event.get("created_at")),
        "state": words,
        "attempts": _n(delivery.get("attempts")),
        "can_send_again": can_send,
        # What 'Send again' sends (resend_event): no typed ids on the page.
        "event_id": as_text(event.get("event_id")),
        "resend_mode": "resume",
        "revoke_live_claim": state == "claimed",
        "expected_generation": delivery.get("generation"),
    }


def _minutes_between(earlier: str, later: str) -> float:
    from datetime import datetime

    try:
        a = datetime.fromisoformat(earlier.replace("Z", "+00:00"))
        b = datetime.fromisoformat(later.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (b - a).total_seconds() / 60


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_events(payload: dict[str, Any]) -> dict[str, Any]:
    """Run detail: the run's events, read-only, with 'Send again' where one is stuck."""
    page = PageQuery.from_payload(payload)
    run_id = page.run_id_param()
    run = _get_run(page, run_id) if run_id else None
    if not run:
        return {"items": [], "total": 0}
    now = _now()
    rows = [_event_row(page, as_dict(e), now) for e in as_list(run.get("events"))]
    return {"items": rows, "total": len(rows)}


#: What a page shows that the active mapping does not read (contract row 68).
MAPPING_GAP_TEXT = {
    "variants_not_used": "has variants the mapping does not read",
    "price_not_extracted": "shows a price the mapping did not read",
    # Mapping test failures (contract row 71); named here should a repair carry them.
    "price_mismatch": "states another price for programs than the mapping reads",
    "price_is_old_price": "is on sale, and the mapping reads the old price",
}


def mapping_gaps_text(item: dict[str, Any]) -> str:
    """The service's own sentence (`last_error`) for a page kept for a mapping repair,
    else its `mapping_gaps` codes in words; empty for other pages."""
    gaps = [as_text(g) for g in as_list(item.get("mapping_gaps")) if as_text(g)]
    last_error = as_text(item.get("last_error"))
    if gaps and last_error:
        return bounded(last_error, 300)
    if gaps:
        return "The page " + " and ".join(MAPPING_GAP_TEXT.get(g, g) for g in gaps) + "."
    return bounded(last_error, 300)


def run_missed(payload: dict[str, Any]) -> dict[str, Any]:
    """Run detail: the pages that ended without a record, with the reason."""
    page = PageQuery.from_payload(payload)
    run_id = page.run_id_param()
    if not run_id:
        return {"items": [], "total": 0}
    client = page.client()
    try:
        answer = as_dict(
            client.get(
                f"/v1/runs/{client.segment(run_id)}/unextracted", query={"limit": MISSED_LIMIT}
            )
        )
    except ScraperError as exc:
        if exc.service_code == "run_not_found":
            return {"items": [], "total": 0}
        raise
    rows = [
        {
            "url": as_text(item.get("url")),
            "reason": as_text(item.get("reason")).replace("_", " "),
            "missing": ", ".join(as_text(f) for f in as_list(item.get("missing_fields"))),
            "gaps": mapping_gaps_text(item),
            "key": as_text(item.get("source_key")),
        }
        for item in (as_dict(i) for i in as_list(answer.get("items")))
    ]
    return {"items": rows, "total": len(rows)}


# ------------------------------------------------------------------- Service


def storage_report(payload: dict[str, Any]) -> dict[str, Any]:
    """Service page: what the service keeps on its disk, loaded with the page."""
    page = PageQuery.from_payload(payload)
    client = page.client()
    try:
        answer = as_dict(client.get(STORAGE_PATH))
    except ScraperError as exc:
        if exc.service_code in {"not_found", "method_not_allowed"}:
            return {
                "content": "This scraper service cannot report its storage: it is older than "
                "the storage routes. Update the service image."
            }
        raise
    lines = _storage_lines(answer)
    labels = [
        ("disk", "Disk"),
        ("working_files", "Working files"),
        ("result_files", "Result files"),
        ("samples", "Samples"),
        ("screenshots", "Screenshots"),
        ("database", "Database"),
        ("runs", "Runs"),
        ("retention", "Automatic clean-up"),
        ("last_automatic_cleanup", "Last automatic clean-up"),
        ("last_manual_cleanup", "Last manual clean-up"),
        ("never_removed", "Never removed"),
    ]
    out = [lines["message"], ""] + [f"- **{label}:** {lines[key]}" for key, label in labels]
    return {"content": "\n".join(out)}


#: Queries whose answer is a markdown component's content.
MARKDOWN_QUERIES = {"run_report", "storage_report"}

PAGE_QUERY_HANDLERS = {
    "runs_table": runs_table,
    "run_report": run_report,
    "run_events": run_events,
    "run_missed": run_missed,
    "storage_report": storage_report,
}
