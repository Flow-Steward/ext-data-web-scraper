"""Error codes of the extension and their mapping from the service contract.

Every service error body is `{"error": {"code", "message", "request_id", "fields"?,
"details"?}}` (contract `error.schema.json`). The extension turns each machine
code into one of its own error codes with a sentence a workflow author can act
on, keeps the service's own message (it never carries a secret, and the
extension scrubs it again), and adds the field list of a `validation_failed`.
"""

from __future__ import annotations

from typing import Any

from handlers.common import as_dict, as_list, as_text, bounded

#: Codes the service may answer with (contract `error.schema.json#/$defs/error_code`,
#: the request-level part) -> (extension code, message, retryable).
SERVICE_ERROR_MAP: dict[str, tuple[str, str, bool]] = {
    "invalid_json": (
        "service_rejected_request",
        "The scraper service could not read the request. This is a defect in the "
        "extension; report it with the request id.",
        False,
    ),
    "idempotency_key_required": (
        "service_rejected_request",
        "The scraper service needs an idempotency key for a start; the extension did not "
        "send one. Report this with the request id.",
        False,
    ),
    "idempotency_key_invalid": (
        "service_rejected_request",
        "The idempotency key built from the platform job and step was refused by the "
        "service. Report this with the request id.",
        False,
    ),
    "invalid_cursor": (
        "invalid_cursor",
        "The list position has expired. Load the list again from the start.",
        False,
    ),
    "unauthorized": (
        "service_unauthorized",
        "The scraper service refused the token. Check the service token of this "
        "scraper profile and save the connection again.",
        False,
    ),
    "not_found": (
        "service_route_missing",
        "The scraper service does not know this API route. Check that the service URL "
        "points at the scraper service and that it supports API v1.",
        False,
    ),
    "run_not_found": (
        "run_not_found",
        "The scraper service has no run with this id (or its record has expired). "
        "Check that the step uses the same scraper profile that started the run.",
        False,
    ),
    "event_not_found": (
        "event_not_found",
        "The scraper service has no event with this id. Check that the webhook belongs "
        "to the scraper profile chosen on this step.",
        False,
    ),
    "mapping_request_not_found": (
        "mapping_request_not_found",
        "The scraper service has no mapping request with this id.",
        False,
    ),
    "screenshot_not_found": (
        "screenshot_not_found",
        "The screenshot is unknown or has expired on the scraper service.",
        False,
    ),
    "sample_not_found": (
        "sample_not_found",
        "The page samples have expired on the scraper service. Fetch samples again "
        "and test the mapping on the new ones.",
        False,
    ),
    "method_not_allowed": (
        "service_route_missing",
        "The scraper service does not accept this call. Check that the service version "
        "matches the extension.",
        False,
    ),
    "not_acceptable": (
        "service_rejected_request",
        "The scraper service cannot answer in the requested format.",
        False,
    ),
    "idempotency_key_reused": (
        "idempotency_key_reused",
        "This workflow step already started a run with a different configuration. "
        "A step starts at most one run; start a new workflow run to scrape again.",
        False,
    ),
    "mapping_version_conflict": (
        "mapping_version_conflict",
        "The mapping of this template was changed by someone else since it was read. "
        "Nothing was overwritten; start again to build on the active version.",
        False,
    ),
    "event_already_claimed": (
        "event_already_claimed",
        "Another platform run is handling this event.",
        False,
    ),
    "event_already_handled": (
        "event_already_handled",
        "This event has already been handled.",
        False,
    ),
    "event_stale": (
        "event_stale",
        "The claim on this event timed out. Only the run that held it may continue; "
        "use Resend on the run page to hand it to a new run.",
        False,
    ),
    "claim_superseded": (
        "claim_superseded",
        "This run no longer holds the event: it was resent to another run. "
        "This run stops without changing anything.",
        False,
    ),
    "event_claim_live": (
        "event_claim_live",
        "A platform run is still working on this event. Resend it only if that run "
        "is stuck: send it again with revoke_live_claim on (Run detail's 'Send again' "
        "does that).",
        False,
    ),
    "event_generation_conflict": (
        "event_generation_conflict",
        "The event was resent by someone else a moment ago. Reload the run and decide again.",
        False,
    ),
    "stage_out_of_order": (
        "stage_out_of_order",
        "The stage 'completed' can only be recorded after 'processed'.",
        False,
    ),
    "mapping_request_closed": (
        "mapping_request_closed",
        "The mapping request is no longer open (resolved, expired or cancelled).",
        False,
    ),
    "run_not_finished": (
        "run_not_finished",
        "The run has not finished yet, so its missed pages cannot be collected again. Wait "
        "until it is completed or failed (the Runs page shows its state), then try again.",
        False,
    ),
    "nothing_to_recollect": (
        "nothing_to_recollect",
        "The run has no missed pages with these reasons, so there is nothing to collect "
        "again. Choose other reasons, or see 'Missed pages' on Run detail.",
        False,
    ),
    "result_not_ready": (
        "result_not_ready",
        "The run has not completed yet, so it has no result file.",
        False,
    ),
    "result_not_available": (
        "result_not_available",
        "The run failed, so it has no result file. See the run's errors on the run page.",
        False,
    ),
    "result_expired": (
        "result_expired",
        "The result file has been deleted by the service's retention. Run the scrape again.",
        False,
    ),
    "request_too_large": (
        "request_too_large",
        "The request is larger than the scraper service accepts (for example a very "
        "long URL list). Split the list into smaller runs.",
        False,
    ),
    "unsupported_media_type": (
        "service_rejected_request",
        "The scraper service refused the request format. Report this with the request id.",
        False,
    ),
    "range_not_satisfiable": (
        "service_download_failed",
        "The result download could not be resumed. Run the step again.",
        True,
    ),
    "validation_failed": (
        "config_invalid",
        "The scraper service refused the configuration.",
        False,
    ),
    # Service CHANGES row 75 (A65, A66): a run of the same site is queued, running or
    # stopping and the start asked `when_site_busy: skip` (the service default). Nothing
    # was stored; `from_service_error` builds the sentence from `error.details`.
    "site_busy": (
        "site_busy",
        "Skipped: this site is already being scraped, so no new crawl was started.",
        False,
    ),
    "queue_full": (
        "service_busy",
        "The scraper service has too many queued runs. Try again later.",
        True,
    ),
    "internal_error": (
        "service_error",
        "The scraper service failed unexpectedly. Its log has the details under the request id.",
        True,
    ),
    "service_unavailable": (
        "service_unavailable",
        "The scraper service is starting or shutting down. Try again in a minute.",
        True,
    ),
    "secrets_key_unavailable": (
        "service_secrets_key_missing",
        "The scraper service has no key to store job secrets, so it cannot start or "
        "resume a run. Ask the service operator to configure the job-secret key.",
        False,
    ),
}

#: Codes the service uses inside `fields[]` / `errors[]`, and in `ok: false` test results.
FIELD_CODE_TEXT: dict[str, str] = {
    "required": "is required",
    "invalid_type": "has the wrong type",
    "invalid_value": "has a value the service does not accept",
    "unknown_field": "is not a known setting",
    "pattern_invalid": "is not a valid regular expression",
    "pattern_timeout": "exceeded the URL regular-expression execution budget; simplify the configured pattern",
    "limit_exceeded": "is above the service limit",
    "browser_not_available": "names a browser that is not installed in the scraper service",
    "target_pages_guessed": (
        "were guessed by URL shape: the site has no structured data or hints for its target "
        "pages. Check that the samples are the pages you want; if not, set target_page_selector "
        "or url_include"
    ),
    "rotating_proxy_with_challenge_handling": (
        "cannot be used with a rotating proxy tier; use a sticky tier for challenge handling"
    ),
    "private_address_refused": "points at a private or local address, which is refused",
    "proxy_tier_not_configured": "names a proxy tier this profile does not have",
    "duplicate_proxy_tier": "lists the same proxy tier twice",
    "solver_not_configured": "needs a challenge solver, but the profile has none",
    "solver_not_allowed": "was switched off: the service does not allow solvers",
    "challenge_click_not_allowed": "was switched off: the service does not allow the checkbox click",
    "robots_override_not_allowed": "was switched back on: the service does not allow ignoring robots.txt",
    "header_not_allowed": "contains a header that may not be set",
    "required_field_not_in_fields": "names a required field that is not in the field list",
    "duplicate_field_name": "has two fields with the same name",
    "template_id_mismatch": "names another template than the mapping",
    "template_limit_reached": "would exceed the number of templates allowed per site",
    "secret_missing": "needs a secret that is not stored in the profile",  # nosec B105
    "delay_range_invalid": "has a minimum delay above the maximum delay",
    # Warning of /v1/analyze and /v1/samples (service change of 2026-11-13, finding A22).
    "protection_not_passed": "could not be fetched: the site's protection check was not passed",
    # Warning of /v1/mappings/test (service change of 2026-11-13, finding A26).
    "fields_never_filled": "was filled on none of the samples",
}

TEST_CODE_TEXT: dict[str, str] = {
    "proxy_connect_failed": "The proxy endpoint could not be reached.",
    "proxy_auth_failed": (
        "The proxy refused the credentials, or the service's IP address is not on the "
        "provider's allow-list."
    ),
    "proxy_timeout": "The proxy did not answer within 20 seconds.",
    "proxy_geo_mismatch": "The proxy's exit IP is not in the configured country.",
    "solver_auth_failed": "The solver refused the API key.",
    "solver_unreachable": "The solver's API could not be reached.",
    "solver_provider_error": "The solver answered with an error.",
    # Mapping test failures (contract row 66, A54); the service's message says more.
    "price_not_extracted": "A sample page shows a price that the mapping did not read.",
    "variants_not_used": "The sample pages carry variant data that the mapping does not use.",
    # Mapping test failures (contract row 71, A60/A61).
    "price_mismatch": (
        "The price the mapping reads differs from the price the page states for programs "
        "(its structured data), so the mapping reads the wrong amount."
    ),
    "price_is_old_price": (
        "On a sale page the mapping reads the old, struck-through price instead of the "
        "current price."
    ),
}

#: Error codes the extension itself raises, with their default sentence.
EXTENSION_ERRORS: dict[str, str] = {
    "invalid_host_envelope": "The platform sent an invocation this extension does not understand.",
    "invalid_payload": "The action received an input it cannot use.",
    "invalid_connection": "The scraper profile is incomplete.",
    "network_blocked": "The service URL is not allowed.",
    "service_unreachable": "The scraper service could not be reached.",
    "service_timeout": "The scraper service did not answer in time.",
    "invalid_service_response": "The scraper service answered with something the extension cannot read.",
    "service_api_unsupported": "The scraper service does not support API v1.",
    "urls_file_invalid": "The URL file could not be read.",
    "url_list_too_long": "The URL list is longer than the scraper service accepts.",
    "result_too_large": "The result file is larger than this platform lets the step store.",
    "result_empty": "The run produced no records, so there is no result file.",
    "run_not_completed": "The scraper run did not complete.",
    "result_csv_missing": "The scraper service writes no CSV beside the JSON Lines file.",
    "result_checksum_mismatch": "The downloaded result file does not match its checksum.",
    "result_size_mismatch": "The downloaded result file does not have the announced size.",
    "result_record_count_mismatch": "The downloaded result file does not have the announced number of records.",
    "artifact_write_failed": "The result file could not be stored as a platform artifact.",
    "proxy_check_failed": "At least one proxy tier failed its check.",
    "solver_check_failed": "The challenge solver failed its check.",
    "mapping_invalid": "The mapping does not have the shape the scraper service expects.",
    "protection_not_passed": "The site's protection check was not passed.",
    "composite_action_direct_call": "build_mapping runs only as a workflow step.",
    "unknown_action": "This extension has no such action.",
    "internal_error": "The extension failed unexpectedly.",
}


class ScraperError(Exception):
    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        result: dict[str, Any] | None = None,
        retryable: bool = False,
        no_external_effect: bool | None = None,
        http_status: int | None = None,
        service_code: str = "",
    ) -> None:
        self.code = code
        self.message = message or EXTENSION_ERRORS.get(code, code)
        self.result = dict(result or {})
        self.retryable = retryable
        self.no_external_effect = no_external_effect
        self.http_status = http_status
        self.service_code = service_code
        super().__init__(f"{code}: {self.message}")


def describe_field_problems(rows: Any, *, limit: int = 20) -> list[str]:
    lines: list[str] = []
    for row in as_list(rows)[:limit]:
        item = as_dict(row)
        field = as_text(item.get("field"), "configuration")
        code = as_text(item.get("code"))
        text = FIELD_CODE_TEXT.get(code, as_text(item.get("message")) or code)
        if code == "limit_exceeded" and item.get("limit") is not None:
            text = f"{text} ({item.get('limit')})"
        service_message = as_text(item.get("message"))
        line = f"{field} {text}"
        if service_message and service_message not in line and code not in {"limit_exceeded"}:
            line = f"{line}: {service_message}"
        lines.append(bounded(line, 300))
    extra = len(as_list(rows)) - limit
    if extra > 0:
        lines.append(f"…and {extra} more")
    return lines


def _when(value: Any) -> str:
    """'2026-11-15T08:10:12Z' -> '2026-11-15 08:10 UTC'; other text as it is."""
    text = as_text(value)
    if len(text) >= 16 and text[10] == "T":
        return f"{text[:10]} {text[11:16]} UTC"
    return text


def site_busy_text(details: dict[str, Any]) -> str:
    """Contract row 75: "Skipped: <site> is already being scraped (run <id>, since <time>)."."""
    site = as_text(details.get("site")) or "this site"
    run_id = as_text(details.get("run_id"))
    queued = as_text(details.get("state")) == "queued" or not details.get("started_at")
    since = _when(details.get("started_at") or details.get("created_at"))
    inner = ", ".join(
        part
        for part in (
            f"run {run_id}" if run_id else "",
            (("queued since " if queued else "since ") + since) if since else "",
        )
        if part
    )
    return (
        f"Skipped: {site} is already being scraped" + (f" ({inner})" if inner else "") + ". "
        "No new crawl was started; follow that run on Web Scraper > Runs."
    )


def from_service_error(status: int, body: Any) -> ScraperError:
    """Map one service error response to a ScraperError with a clear message."""
    error = as_dict(as_dict(body).get("error"))
    service_code = as_text(error.get("code"))
    service_message = bounded(as_text(error.get("message")), 500)
    request_id = as_text(error.get("request_id"))
    details = as_dict(error.get("details"))
    mapped = SERVICE_ERROR_MAP.get(service_code)
    if mapped is None:
        # Unknown code: treat it by its HTTP status (contract section 1).
        if status in (401, 403):
            mapped = SERVICE_ERROR_MAP["unauthorized"]
        elif status == 404:
            mapped = (
                "service_not_found",
                "The scraper service did not find what was asked for.",
                False,
            )
        elif status == 409:
            mapped = (
                "service_conflict",
                "The scraper service refused the call because of a conflict.",
                False,
            )
        elif status == 422:
            mapped = SERVICE_ERROR_MAP["validation_failed"]
        elif status == 429:
            mapped = SERVICE_ERROR_MAP["queue_full"]
        elif status >= 500:
            mapped = SERVICE_ERROR_MAP["internal_error"]
        else:
            mapped = ("service_rejected_request", "The scraper service refused the request.", False)
    code, message, retryable = mapped
    problems = describe_field_problems(error.get("fields"))
    parts = [message]
    if service_code == "site_busy":
        # A plain sentence of our own; the service's says the same in other words.
        parts = [site_busy_text(details)]
        service_message = ""
        request_id = ""
    if problems:
        parts.append("Problems: " + "; ".join(problems) + ".")
    elif service_message and service_message not in message:
        parts.append(f"Service says: {service_message}")
    if service_code == "event_already_claimed" and details.get("claimant"):
        claimant = as_dict(details.get("claimant"))
        job = as_text(claimant.get("job_id"))
        if job:
            parts.append(f"Claimed by platform job {job} (/jobs/{job}).")
    if service_code == "event_claim_live" and details.get("claimant"):
        job = as_text(as_dict(details.get("claimant")).get("job_id"))
        if job:
            parts.append(f"The claim is held by platform job {job} (/jobs/{job}).")
    if service_code == "mapping_version_conflict" and "active_version" in details:
        parts.append(f"Active version: {details.get('active_version')}.")
    if service_code == "result_not_ready" and details.get("state"):
        parts.append(f"Run state: {details.get('state')}.")
    if service_code == "run_not_finished" and details.get("state"):
        parts.append(f"Run state: {details.get('state')}.")
    if service_code == "nothing_to_recollect":
        missed = as_dict(details.get("unextracted_by_reason"))
        if missed:
            parts.append(
                "Its missed pages by reason: "
                + ", ".join(
                    f"{reason.replace('_', ' ')} {number}" for reason, number in missed.items()
                )
                + "."
            )
        else:
            parts.append("The run has no missed pages at all.")
    if request_id:
        parts.append(f"(request {request_id})")
    result: dict[str, Any] = {"service_error_code": service_code or None, "http_status": status}
    if problems:
        result["field_errors"] = [
            {
                "field": as_text(as_dict(row).get("field")),
                "code": as_text(as_dict(row).get("code")),
                "message": bounded(as_text(as_dict(row).get("message")), 300),
                **(
                    {"limit": as_dict(row).get("limit")}
                    if as_dict(row).get("limit") is not None
                    else {}
                ),
            }
            for row in as_list(error.get("fields"))
        ]
    if details:
        safe_details: dict[str, Any] = {
            key: details[key]
            for key in (
                "active_version",
                "run_id",
                "state",
                "status",
                "expires_at",
                "sample_ids",
                "reasons",
                "unextracted_by_reason",
                "started_at",
                "created_at",
                "site",
            )
            if key in details
        }
        if "claimant" in details:
            claimant = as_dict(details.get("claimant"))
            safe_details["claimant"] = {
                key: claimant.get(key) for key in ("job_id", "workflow_id", "step_id")
            }
        if safe_details:
            result["details"] = safe_details
    no_effect = status in (400, 401, 404, 405, 406, 409, 413, 415, 422) or None
    return ScraperError(
        code,
        " ".join(part for part in parts if part),
        result=result,
        retryable=retryable,
        no_external_effect=no_effect,
        http_status=status,
        service_code=service_code,
    )
