"""The result file: download, verify, store as a platform artifact (contract section 7)."""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import time
from collections.abc import Iterator
from typing import Any

from flowsteward_extension_sdk import report_progress
from flowsteward_extension_sdk.artifacts import (
    ArtifactAccessError,
    find_artifact_descriptor,
    write_artifact_stream,
)

from handlers.budget import OPERATION_TIMEOUT_ENV, budget_seconds
from handlers.common import as_dict, as_list, as_text, count
from handlers.context import ActionContext
from handlers.errors import ScraperError

RESULT_BINDING = "result_artifact_handle"
#: The CSV beside the JSON Lines file (service 2026-11-14, contract 7.5): its own step,
#: because the platform fails a step whose prepared output got no bytes, and an older
#: service has no CSV.
RESULT_CSV_BINDING = "result_csv_handle"
NDJSON = "application/x-ndjson"
CSV = "text/csv"
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_TIMEOUT_SECONDS = 120
CHUNK = 1024 * 1024

# Time budget: handlers/budget.py (T - 20 s from the start; default 280 s, killed at
# 330 s). Inside that budget get_result:
# - downloads with everything except the last UPLOAD_RESERVE_SECONDS (40 s);
# - never waits longer than READ_TIMEOUT_SECONDS (30 s) for one read nor past the
#   download deadline, checking the deadline after every chunk, so a slow link ends
#   the download at the deadline, not mid-upload;
# - gives the upload the rest (at least 40 s) as its timeout.
# When the budget runs out the step fails with the retryable `service_timeout` and
# stores nothing.
UPLOAD_RESERVE_SECONDS = 40
READ_TIMEOUT_SECONDS = 30


class OutOfTime(ScraperError):
    """The step's time budget is spent; never retried inside the step."""


def _out_of_time(size: int, budget: float) -> OutOfTime:
    return OutOfTime(
        "service_timeout",
        f"The {_mib(size)} result file could not be downloaded and stored within this "
        f"step's time limit ({int(budget)} s of the platform's operation timeout). "
        "Nothing was stored. Run the step again (Retry), or ask the operator to raise "
        f"{OPERATION_TIMEOUT_ENV} for results of this size.",
        retryable=True,
    )


def display_file_name(name: str, run_id: str) -> str:
    """The result file's name as the run id, `run_....jsonl`.

    The service names the file `<site>_<run id>.jsonl`; `com_run_01M...` after the dot
    of the site looks like a provider token to the platform's secret scanner
    (`dotted_provider_token`, core/infrastructure/security/secret_detection.py:176-183)
    and the name was shown as `books.[SECRET:...]`. The run id alone is never matched.
    """
    run = as_text(run_id)
    if not run:
        return name
    suffix = name.rsplit(".", 1)[-1] if "." in name else "jsonl"
    return f"{run}.{suffix}"


def _mib(size: int) -> str:
    return f"{size / 1048576:.1f} MiB"


def _grant_limit(payload: dict[str, Any], binding: str = RESULT_BINDING) -> int | None:
    try:
        descriptor = find_artifact_descriptor(payload, binding_key=binding, role="output")
    except ArtifactAccessError as exc:
        raise ScraperError(
            "artifact_write_failed",
            "The platform did not grant this step an output file. get_result must run as a "
            "workflow step of a project workflow.",
        ) from exc
    for value in (
        as_dict(descriptor.get("access")).get("max_size_bytes"),
        descriptor.get("size_bytes"),
    ):
        if value is None:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _download_to(
    ctx: ActionContext,
    path: str,
    download_path: str,
    expected_size: int,
    etag: str,
    *,
    deadline: float,
    budget: float,
    accept: str = NDJSON,
) -> tuple[int, str, int]:
    """Download into `path`, resuming with Range; returns (bytes, sha256, newline count).

    `deadline` is a time.monotonic() value; the download never runs past it.
    """
    client = ctx.profile.client(DOWNLOAD_TIMEOUT_SECONDS)
    received = 0
    last_error: ScraperError | None = None
    with open(path, "wb") as handle:
        for _attempt in range(DOWNLOAD_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if remaining < 1:
                raise _out_of_time(expected_size, budget)
            try:
                status, headers, chunks = client.stream(
                    download_path,
                    accept=accept,
                    offset=received,
                    etag=etag,
                    timeout=max(1, int(min(READ_TIMEOUT_SECONDS, remaining))),
                )
                if received and status == 200:
                    # The service ignored the range (or the file changed): start over.
                    handle.seek(0)
                    handle.truncate()
                    received = 0
                for chunk in chunks:
                    handle.write(chunk)
                    received += len(chunk)
                    if received < expected_size and time.monotonic() > deadline:
                        raise _out_of_time(expected_size, budget)
                    if received > expected_size:
                        raise ScraperError(
                            "result_size_mismatch",
                            f"The result download is longer than the announced {expected_size} bytes.",
                        )
                if received >= expected_size:
                    last_error = None
                    break
                last_error = ScraperError(
                    "service_download_failed",
                    f"The result download stopped after {received} of {expected_size} bytes.",
                    retryable=True,
                )
            except ScraperError as exc:
                if (
                    isinstance(exc, OutOfTime)
                    or exc.code in {"result_size_mismatch"}
                    or not exc.retryable
                ):
                    raise
                last_error = exc
            except OSError as exc:
                last_error = ScraperError(
                    "service_download_failed",
                    f"The result download was interrupted: {exc}.",
                    retryable=True,
                )
        if last_error is not None:
            raise last_error
    digest = hashlib.sha256()
    lines = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
            lines += chunk.count(b"\n")
    return received, digest.hexdigest(), lines


def _file_chunks(path: str) -> Iterator[bytes]:
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            yield chunk


def _store(
    ctx: ActionContext,
    *,
    download_path: str,
    size: int,
    expected_sha: str,
    expected_lines: int | None,
    binding: str,
    content_type: str,
    what: str,
    started: float,
    budget: float,
) -> tuple[dict[str, Any], str]:
    """Download one file of the run, check size, SHA-256 (and lines), then store it.

    A mismatch stores nothing. Returns (the artifact write answer, sha256).
    """
    report_progress(f"Downloading the {what} ({_mib(size)})")
    suffix = ".csv" if content_type == CSV else ".jsonl"
    fd, path = tempfile.mkstemp(prefix="web_scraper_result_", suffix=suffix)
    os.close(fd)
    try:
        received, sha, lines = _download_to(
            ctx,
            path,
            download_path,
            size,
            f'"{expected_sha}"',
            deadline=started + budget - UPLOAD_RESERVE_SECONDS,
            budget=budget,
            accept=content_type,
        )
        if received != size:
            raise ScraperError(
                "result_size_mismatch",
                f"The downloaded {what} has {received} bytes, the service announced {size}. "
                "Nothing was stored; run the step again.",
            )
        if sha != expected_sha:
            raise ScraperError(
                "result_checksum_mismatch",
                f"The downloaded {what} does not match the checksum the service announced. "
                "Nothing was stored; run the step again, and report it if it repeats.",
                result={"expected_sha256": expected_sha, "received_sha256": sha},
            )
        if expected_lines is not None and lines != expected_lines:
            raise ScraperError(
                "result_record_count_mismatch",
                f"The downloaded result has {count(lines, 'record')}, the service announced "
                f"{expected_lines}. Nothing was stored.",
            )
        upload_seconds = int(started + budget - time.monotonic())
        if upload_seconds < 1:
            raise _out_of_time(size, budget)
        report_progress(f"Storing the {what}")
        try:
            written = write_artifact_stream(
                ctx.payload,
                _file_chunks(path),
                binding_key=binding,
                content_type=content_type,
                timeout_seconds=upload_seconds,
            )
        except ArtifactAccessError as exc:
            message = str(exc)
            if "size" in message.lower() or "limit" in message.lower():
                raise ScraperError(
                    "result_too_large",
                    f"The platform refused the {_mib(size)} {what}: {message}. Ask the "
                    "operator to raise FS_EXTENSION_ARTIFACT_MAX_BYTES or to use S3 artifact storage.",
                ) from exc
            raise ScraperError(
                "artifact_write_failed", f"The {what} could not be stored: {message}."
            ) from exc
        if (
            int(written.get("size_bytes") or size) != size
            or as_text(written.get("sha256"), sha) != sha
        ):
            raise ScraperError(
                "artifact_write_failed",
                f"The stored artifact differs from the downloaded {what}.",
            )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
    return written, sha


def _handle(written: dict[str, Any]) -> str:
    return as_text(written.get("artifact_handle")) or (
        f"artifact:{as_text(written.get('artifact_id'))}" if written.get("artifact_id") else ""
    )


def get_result(ctx: ActionContext) -> dict[str, Any]:
    started = time.monotonic()
    budget = budget_seconds()
    run_id = ctx.require_text("run_id", "Run id")
    client = ctx.profile.client()
    service = as_dict(client.get(f"/v1/runs/{client.segment(run_id)}/result"))
    size = int(service.get("size_bytes") or 0)
    expected_sha = as_text(service.get("sha256")).lower()
    expected_records = int(service.get("record_count") or 0)
    if expected_records == 0 or size == 0:
        # A36: an empty result is a 0-byte file (contract 7.1). The platform refuses a
        # step whose output grant received no bytes (artifact_upload_missing), so there
        # is nothing to store; 'Scraper events' skips this step for such a run.
        reason = as_text(service.get("stop_reason"))
        raise ScraperError(
            "result_empty",
            "The run produced no records"
            + (f" (stop reason {reason})" if reason else "")
            + ", so there is no result file to store. Nothing to retry: 'Scraper events' "
            "skips 'Get result' for such a run (import the 0.1.12 template).",
            result={"run_id": run_id, "record_count": 0, "stop_reason": service.get("stop_reason")},
            no_external_effect=True,
        )
    download_path = (
        as_text(service.get("download_path"))
        or f"/v1/runs/{client.segment(run_id)}/result/download"
    )
    _api_path(download_path, "download")
    limit = _grant_limit(ctx.payload)
    if limit is not None and size > limit:
        raise ScraperError(
            "result_too_large",
            f"The result file is {_mib(size)}, but this step may store at most {_mib(limit)}. "
            "Ask the platform operator to raise FS_EXTENSION_ARTIFACT_MAX_BYTES (results above "
            "100 MiB also need S3 artifact storage), or lower the scraper service's "
            "max_result_bytes so runs stop in time. The whole file stays downloadable from the "
            "scraper service until its retention ends.",
            result={"size_bytes": size, "limit_bytes": limit, "run_id": run_id},
        )
    written, sha = _store(
        ctx,
        download_path=download_path,
        size=size,
        expected_sha=expected_sha,
        expected_lines=expected_records,
        binding=RESULT_BINDING,
        content_type=NDJSON,
        what="result file",
        started=started,
        budget=budget,
    )
    csv = as_dict(service.get("csv"))
    return {
        "artifact_handle": _handle(written),
        "run_id": as_text(service.get("run_id"), run_id),
        "format": as_text(service.get("format"), "jsonl"),
        "file_name": display_file_name(as_text(service.get("file_name")), run_id),
        "size_bytes": size,
        "record_count": expected_records,
        "sha256": sha,
        "templates": [
            {
                "template_id": as_text(row.get("template_id")),
                "record_count": int(row.get("record_count") or 0),
            }
            for row in (as_dict(item) for item in as_list(service.get("templates")))
        ],
        "outcome": service.get("outcome"),
        "stop_reason": service.get("stop_reason"),
        "created_at": as_text(service.get("created_at")),
        # Service 2026-11-14 (contract 7.5): the same records as CSV; an older service has none.
        "has_csv": bool(csv.get("download_path")),
    }


def _api_path(path: str, what: str) -> str:
    if not path.startswith("/v1/"):
        raise ScraperError(
            "invalid_service_response",
            f"The service named a {what} location outside its API; the extension does not follow it.",
        )
    return path


def get_result_csv(ctx: ActionContext) -> dict[str, Any]:
    """Store the run's CSV (UTF-8 with BOM, `;`, CRLF) as a second file of the job."""
    started = time.monotonic()
    budget = budget_seconds()
    run_id = ctx.require_text("run_id", "Run id")
    client = ctx.profile.client()
    service = as_dict(client.get(f"/v1/runs/{client.segment(run_id)}/result"))
    csv = as_dict(service.get("csv"))
    if not csv.get("download_path"):
        raise ScraperError(
            "result_csv_missing",
            "This scraper service writes no CSV beside the JSON Lines file (it is older than "
            "the contract of 2026-11-14). Update the service image; the JSON Lines file is "
            "stored by 'Get result' as before.",
            result={"run_id": run_id},
            no_external_effect=True,
        )
    size = int(csv.get("size_bytes") or 0)
    download_path = _api_path(as_text(csv.get("download_path")), "CSV download")
    limit = _grant_limit(ctx.payload, RESULT_CSV_BINDING)
    if limit is not None and size > limit:
        raise ScraperError(
            "result_too_large",
            f"The CSV file is {_mib(size)}, but this step may store at most {_mib(limit)}. "
            "Ask the platform operator to raise FS_EXTENSION_ARTIFACT_MAX_BYTES.",
            result={"size_bytes": size, "limit_bytes": limit, "run_id": run_id},
        )
    written, sha = _store(
        ctx,
        download_path=download_path,
        size=size,
        expected_sha=as_text(csv.get("sha256")).lower(),
        expected_lines=None,  # a quoted field may hold a line break
        binding=RESULT_CSV_BINDING,
        content_type=CSV,
        what="CSV file",
        started=started,
        budget=budget,
    )
    return {
        "artifact_handle": _handle(written),
        "run_id": as_text(service.get("run_id"), run_id),
        "file_name": display_file_name(as_text(csv.get("file_name")), run_id),
        "size_bytes": size,
        "sha256": sha,
        "record_count": int(service.get("record_count") or 0),
    }
