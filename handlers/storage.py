"""What the scraper service keeps on its disk, and cleaning it up (contract section 16).

`GET /v1/admin/storage` and `POST /v1/admin/cleanup` (service 2026-11-14). Each
answer leads with at most a dozen plain lines, which the platform shows as the
action's summary (the first 12 plain values, see run_page.py), and keeps the
service's whole answer under `details`.
"""

from __future__ import annotations

from typing import Any

from handlers.common import as_dict, as_text, count, human_bytes
from handlers.context import ActionContext
from handlers.errors import ScraperError

STORAGE_PATH = "/v1/admin/storage"
CLEANUP_PATH = "/v1/admin/cleanup"
MAX_DAYS = 3650
MAX_HOURS = 87600
#: Less than this share of the disk free, or less than LOW_FREE_BYTES: say so first.
LOW_FREE_SHARE = 0.10
LOW_FREE_BYTES = 2 * 1024 * 1024 * 1024

#: The clean-up's inputs in the order the forms show them.
CLEANUP_INPUTS = (
    "working_files_older_than_days",
    "result_files_older_than_days",
    "include_unfetched_results",
    "run_records_older_than_days",
    "samples_older_than_hours",
    "compact_database",
)
#: The noun one removed item of each part is counted in.
PART_NOUNS = {
    "run_working_files": "run",
    "result_files": "file",
    "run_records": "run",
    "samples": "sample",
    "protection_screenshots": "screenshot",
}


# --------------------------------------------------------------------- shared


def _when(value: Any) -> str:
    """`2026-11-14T09:59:31Z` -> `2026-11-14 09:59 UTC`."""
    text = as_text(value)
    if len(text) >= 16 and text[10] == "T":
        return f"{text[:10]} {text[11:16]} UTC"
    return text or "unknown"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _hours(hours: Any) -> str:
    number = _int(hours)
    if number and number % 24 == 0 and number >= 48:
        return count(number // 24, "day")
    return count(number, "hour")


def _call(ctx: ActionContext, method: str, path: str, body: Any = None) -> dict[str, Any]:
    client = ctx.profile.client()
    try:
        if method == "GET":
            return as_dict(client.get(path))
        return as_dict(client.post(path, body))
    except ScraperError as exc:
        if exc.service_code in {"not_found", "method_not_allowed"} or (
            exc.http_status in {404, 405} and not exc.service_code
        ):
            raise ScraperError(
                "service_route_missing",
                "This scraper service cannot report or clean up its storage: it is older than "
                "the storage routes (service contract of 2026-11-14). Update the service "
                "image, then try again.",
                result=exc.result,
                no_external_effect=True,
                http_status=exc.http_status,
                service_code=exc.service_code,
            ) from None
        if exc.service_code == "validation_failed":
            exc.message = exc.message.replace(
                "The scraper service refused the configuration.",
                "The scraper service refused the clean-up settings.",
                1,
            )
        raise


# -------------------------------------------------------------------- storage


#: How one removed part reads in a sentence: "3 runs' working files", "4 samples".
REMOVED_NOUNS = {
    "run_working_files": ("working files of {n}", "run"),
    "result_files": ("{n}", "result file"),
    "run_records": ("{n} (whole records)", "run"),
    "samples": ("{n}", "sample"),
    "protection_screenshots": ("{n}", "screenshot"),
}


def _removed_words(removed: dict[str, Any]) -> str:
    parts = []
    for key, (pattern, noun) in REMOVED_NOUNS.items():
        items = _int(as_dict(removed.get(key)).get("items"))
        if items:
            parts.append(pattern.format(n=count(items, noun)))
    return ", ".join(parts)


def _last_cleanup(summary: Any, *, never: str) -> str:
    entry = as_dict(summary)
    if not entry:
        return never
    freed = _int(entry.get("bytes_freed"))
    words = _removed_words(as_dict(entry.get("removed")))
    if not freed and not words:
        return f"{_when(entry.get('at'))}, nothing removed."
    return f"{_when(entry.get('at'))}, {human_bytes(freed)} freed" + (
        f" ({words})." if words else "."
    )


def _oldest(kind: dict[str, Any]) -> str:
    return f" Oldest {_when(kind['oldest_at'])}." if kind.get("oldest_at") else ""


def _storage_lines(answer: dict[str, Any]) -> dict[str, str]:
    volume = as_dict(answer.get("volume"))
    kinds = as_dict(answer.get("kinds"))
    runs = as_dict(answer.get("runs"))
    retention = as_dict(answer.get("retention"))
    last = as_dict(answer.get("last_cleanup"))
    work = as_dict(kinds.get("run_working_files"))
    results = as_dict(kinds.get("result_files"))
    samples = as_dict(kinds.get("samples"))
    shots = as_dict(kinds.get("protection_screenshots"))
    database = as_dict(kinds.get("database"))
    identities = as_dict(kinds.get("identities"))
    other = as_dict(kinds.get("other"))
    total, free = _int(volume.get("total_bytes")), _int(volume.get("free_bytes"))
    kept = _int(answer.get("total_bytes"))

    sizes = {
        "working files": _int(work.get("bytes")),
        "result files": _int(results.get("bytes")),
        "samples": _int(samples.get("bytes")),
        "screenshots": _int(shots.get("bytes")),
        "the database": _int(database.get("bytes")),
    }
    biggest = max(sizes, key=lambda name: sizes[name])
    low = total > 0 and (free < total * LOW_FREE_SHARE or free < LOW_FREE_BYTES)
    message = (
        f"The service keeps {human_bytes(kept)}; its disk has {human_bytes(free)} free of "
        f"{human_bytes(total)}."
    )
    if sizes[biggest]:
        message += f" The biggest part is {biggest} ({human_bytes(sizes[biggest])})."
    if low:
        message = "Low on disk space. " + message + " Clean up now."

    used_share = f"{(total - free) / total * 100:.0f} %" if total else "n/a"
    disk = (
        f"{human_bytes(free)} free of {human_bytes(total)} ({used_share} used in all; "
        f"{human_bytes(kept)} of it is the service's data in {as_text(answer.get('data_dir'), '/data')})."
    )

    working = (
        f"{human_bytes(work.get('bytes'))} in {count(_int(work.get('items')), 'run')} "
        "(the pages a run loaded, kept for re-extraction)."
    )
    if _int(work.get("active_items")):
        working += f" {count(_int(work.get('active_items')), 'run')} not finished: never touched."
    if _int(work.get("orphan_items")):
        working += f" {_int(work.get('orphan_items'))} without a run record."
    working += _oldest(work)

    result_line = (
        f"{human_bytes(results.get('bytes'))} in {count(_int(results.get('items')), 'file')}."
    )
    if _int(results.get("unfetched_items")):
        result_line += (
            f" {_int(results.get('unfetched_items'))} not fetched by Flow Steward yet "
            f"({human_bytes(results.get('unfetched_bytes'))}): a clean-up keeps them unless "
            "you include them."
        )
    if _int(results.get("active_items")):
        result_line += f" {_int(results.get('active_items'))} still being written."
    result_line += _oldest(results)

    sample_line = (
        f"{human_bytes(samples.get('bytes'))} in {count(_int(samples.get('items')), 'sample')} "
        "(pages fetched to build and test mappings)." + _oldest(samples)
    )
    shot_line = (
        f"{human_bytes(shots.get('bytes'))} in {count(_int(shots.get('items')), 'screenshot')} "
        "of protection checks." + _oldest(shots)
    )
    database_line = (
        f"{human_bytes(database.get('bytes'))} (file {human_bytes(database.get('main_bytes'))}, "
        f"write-ahead log {human_bytes(database.get('wal_bytes'))}). It shrinks only when "
        f"compacted: about {human_bytes(database.get('reclaimable_bytes'))} is free inside the "
        "file, and the log empties."
    )

    by_state = as_dict(runs.get("by_state"))
    states = ", ".join(
        f"{_int(number)} {state.replace('_', ' ')}"
        for state, number in by_state.items()
        if _int(number)
    )
    runs_line = f"{count(_int(runs.get('total')), 'run')}" + (f": {states}." if states else ".")
    if runs.get("oldest_finished_at"):
        runs_line += f" Oldest finished {_when(runs.get('oldest_finished_at'))}."

    interval = retention.get("interval_seconds")
    every = (
        f"every {count(int(interval), 'second')}"
        if isinstance(interval, (int, float)) and not isinstance(interval, bool)
        else "regularly"
    )
    retention_line = (
        f"The service cleans up by itself {every}: working files "
        f"{count(_int(retention.get('stored_pages_days')), 'day')}, result files "
        f"{count(_int(retention.get('result_days')), 'day')} and run records "
        f"{count(_int(retention.get('job_record_days')), 'day')} after a run ends; samples "
        f"and screenshots after {_hours(retention.get('samples_hours'))}."
    )

    automatic = _last_cleanup(last.get("automatic"), never="No automatic clean-up yet.")
    with_removals = as_dict(last.get("automatic_with_removals"))
    if with_removals and with_removals.get("at") != as_dict(last.get("automatic")).get("at"):
        automatic += " Last time it removed something: " + _last_cleanup(with_removals, never="")
    manual = _last_cleanup(last.get("manual"), never="Never.")

    never_removed = (
        f"Browser identities {human_bytes(identities.get('bytes'))} (identities.json); "
        f"other files {human_bytes(other.get('bytes'))}."
    )
    return {
        "message": message,
        "disk": disk,
        "working_files": working,
        "result_files": result_line,
        "samples": sample_line,
        "screenshots": shot_line,
        "database": database_line,
        "runs": runs_line,
        "retention": retention_line,
        "last_automatic_cleanup": automatic,
        "last_manual_cleanup": manual,
        "never_removed": never_removed,
    }


def service_storage(ctx: ActionContext) -> dict[str, Any]:
    answer = _call(ctx, "GET", STORAGE_PATH)
    return {**_storage_lines(answer), "details": answer}


# -------------------------------------------------------------------- cleanup


def _number(ctx: ActionContext, name: str, label: str, limit: int) -> int | None:
    raw = ctx.input.get(name)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        raise ScraperError(
            "invalid_payload", f"{label} must be a whole number.", no_external_effect=True
        )
    try:
        value = int(str(raw).strip()) if not isinstance(raw, (int, float)) else int(raw)
    except ValueError:
        raise ScraperError(
            "invalid_payload", f"{label} must be a whole number.", no_external_effect=True
        ) from None
    if isinstance(raw, float) and raw != value:
        raise ScraperError(
            "invalid_payload", f"{label} must be a whole number.", no_external_effect=True
        )
    if not 0 <= value <= limit:
        raise ScraperError(
            "invalid_payload",
            f"{label} must be between 0 and {limit}.",
            no_external_effect=True,
        )
    return value


def _flag(ctx: ActionContext, name: str, default: bool) -> bool:
    raw = ctx.input.get(name)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    raise ScraperError("invalid_payload", f"{name} must be true or false.", no_external_effect=True)


def cleanup_request(ctx: ActionContext, *, dry_run: bool) -> dict[str, Any]:
    """The body of POST /v1/admin/cleanup; an empty field leaves that part untouched."""
    working = _number(
        ctx, "working_files_older_than_days", "Working files older than (days)", MAX_DAYS
    )
    results = _number(
        ctx, "result_files_older_than_days", "Result files older than (days)", MAX_DAYS
    )
    records = _number(ctx, "run_records_older_than_days", "Run records older than (days)", MAX_DAYS)
    samples = _number(
        ctx, "samples_older_than_hours", "Samples and screenshots older than (hours)", MAX_HOURS
    )
    unfetched = _flag(ctx, "include_unfetched_results", False)
    compact = _flag(ctx, "compact_database", False)
    body: dict[str, Any] = {"dry_run": dry_run}
    if working is not None:
        body["run_working_files"] = {"older_than_days": working}
    if results is not None:
        body["result_files"] = {"older_than_days": results, "include_unfetched": unfetched}
    if records is not None:
        body["run_records"] = {"older_than_days": records, "include_unfetched": unfetched}
    if samples is not None:
        body["samples"] = {"older_than_hours": samples}
        body["protection_screenshots"] = {"older_than_hours": samples}
    if compact:
        body["compact_database"] = True
    if len(body) == 1:
        raise ScraperError(
            "invalid_payload",
            "Nothing to clean up was chosen. Fill at least one 'older than' field or turn on "
            "'Compact the database'.",
            no_external_effect=True,
        )
    return body


def _part_line(answer: dict[str, Any], request: dict[str, Any], key: str) -> str:
    noun = PART_NOUNS[key]
    if key not in request:
        return "Not part of this clean-up."
    entry = as_dict(as_dict(answer.get("removed")).get(key))
    items, size = _int(entry.get("items")), _int(entry.get("bytes"))
    verb = "would be removed" if answer.get("dry_run") else "removed"
    if not items:
        return "Nothing to remove." if answer.get("dry_run") else "Nothing removed."
    limit = as_dict(request[key])
    if "older_than_days" in limit:
        age = f"finished at least {count(limit['older_than_days'], 'day')} ago"
    else:
        age = f"last written at least {count(limit['older_than_hours'], 'hour')} ago"
    return f"{count(items, noun)}, {human_bytes(size)} {verb} ({age})."


def _database_line(answer: dict[str, Any]) -> str:
    compaction = as_dict(answer.get("compaction"))
    if not compaction:
        return "Not compacted ('Compact the database' is off)."
    checkpoint = as_text(compaction.get("checkpoint"))
    vacuum = as_text(compaction.get("vacuum"))
    log = human_bytes(compaction.get("wal_bytes_before"))
    inside = human_bytes(compaction.get("reclaimable_bytes"))
    parts = []
    if checkpoint == "would_run":
        parts.append(f"The write-ahead log ({log}) would be written back and emptied")
    elif checkpoint == "done":
        parts.append(f"The write-ahead log ({log}) was written back and emptied")
    else:
        parts.append(
            "The write-ahead log could not be emptied now (a reader held it); nothing is lost, "
            "try again later"
        )
    if vacuum == "would_run":
        parts.append(f"the file would be compacted (about {inside} free inside it)")
    elif vacuum == "done":
        parts.append("the file was compacted")
    else:
        reason = as_text(compaction.get("vacuum_skipped_reason"), "no reason given")
        parts.append(f"compacting the file is skipped: {reason}")
    line = "; ".join(parts) + "."
    if compaction.get("database_bytes_after") is not None:
        line += (
            f" Database {human_bytes(compaction.get('database_bytes_before'))} → "
            f"{human_bytes(compaction.get('database_bytes_after'))}."
        )
    return line[0].upper() + line[1:]


def _kept_line(answer: dict[str, Any]) -> str:
    kept = as_dict(answer.get("kept"))
    active, unfetched = _int(kept.get("active_runs")), _int(kept.get("unfetched_results"))
    parts = []
    if active:
        parts.append(f"{count(active, 'run')} not finished (nothing of it is touched)")
    if unfetched:
        parts.append(
            f"{count(unfetched, 'finished run')} whose result Flow Steward has not fetched yet "
            "(include them only if you no longer need those results)"
        )
    return ("Kept: " + "; ".join(parts) + ".") if parts else "Nothing was held back."


def _cleanup_result(answer: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    dry = bool(answer.get("dry_run"))
    freed = _int(answer.get("bytes_freed"))
    compaction = as_dict(answer.get("compaction"))
    if dry:
        message = (
            f"Dry run: nothing was removed. This clean-up would free {human_bytes(freed)}"
            + (
                f", and compacting would give back up to "
                f"{human_bytes(_int(compaction.get('wal_bytes_before')) + _int(compaction.get('reclaimable_bytes')))} more"
                if compaction
                else ""
            )
            + ". To remove, run it with Dry run off (on the Service page: 'Clean up now')."
        )
        freed_line = f"{human_bytes(freed)} would be freed" + (
            " (plus what compacting gives back)." if compaction else "."
        )
    else:
        message = f"Clean-up done at {_when(answer.get('time'))}: {human_bytes(freed)} freed."
        if compaction and as_text(compaction.get("vacuum")) == "skipped":
            message += " The database file was not compacted; see 'database'."
        freed_line = f"{human_bytes(freed)} freed."
    return {
        "message": message,
        "dry_run": dry,
        "working_files": _part_line(answer, request, "run_working_files"),
        "result_files": _part_line(answer, request, "result_files"),
        "run_records": _part_line(answer, request, "run_records"),
        "samples": _part_line(answer, request, "samples"),
        "screenshots": _part_line(answer, request, "protection_screenshots"),
        "database": _database_line(answer),
        "kept": _kept_line(answer),
        "freed": freed_line,
        "bytes_freed": freed,
        "details": {"request": request, "answer": answer},
    }


def clean_up_storage(ctx: ActionContext) -> dict[str, Any]:
    """Workflow step and the Service page's dry-run button: dry run unless told otherwise."""
    request = cleanup_request(ctx, dry_run=_flag(ctx, "dry_run", True))
    return _cleanup_result(_call(ctx, "POST", CLEANUP_PATH, request), request)


def clean_up_storage_now(ctx: ActionContext) -> dict[str, Any]:
    """The Service page's apply button: removes, after the operator ticked the confirmation."""
    if not _flag(ctx, "confirm_removal", False):
        raise ScraperError(
            "invalid_payload",
            "Nothing was removed. Press 'Clean up: dry run' first to see what would go, then "
            "tick 'Yes, remove these files' and press 'Clean up now'.",
            no_external_effect=True,
        )
    request = cleanup_request(ctx, dry_run=False)
    return _cleanup_result(_call(ctx, "POST", CLEANUP_PATH, request), request)
