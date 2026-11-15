"""Request router: one JSON envelope in, one canonical envelope out."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from flowsteward_extension_sdk import runtime_context_from_payload

from handlers import (
    connection,
    events,
    mappings,
    results,
    run_page,
    runs,
    samples_file,
    storage,
)
from handlers.common import Scrubber, as_dict, as_text, fail, ok
from handlers.context import ActionContext
from handlers.errors import ScraperError
from handlers.pages import MARKDOWN_QUERIES, PAGE_QUERY_HANDLERS, _connection
from handlers.profile import profile_from_connection
from handlers.queries import QUERY_HANDLERS

ActionHandler = Callable[[ActionContext], dict[str, Any]]

ACTION_HANDLERS: dict[str, ActionHandler] = {
    # Connection form checks
    "test_connection": connection.test_connection,
    "test_proxy": connection.test_proxy,
    "test_captcha_solver": connection.test_captcha_solver,
    # Configuration and analysis
    "validate_config": runs.validate_config,
    "analyze_site": runs.analyze_site,
    # Mappings
    "fetch_samples": mappings.fetch_samples,
    "test_mapping": mappings.test_mapping,
    "get_mappings": mappings.get_mappings,
    "activate_mapping": mappings.activate_mapping,
    "export_mappings": mappings.export_mappings,
    "import_mappings": mappings.import_mappings,
    "get_mapping_request": mappings.get_mapping_request,
    "fail_mapping_request": mappings.fail_mapping_request,
    # Runs
    "start_run": runs.start_run,
    "stop_run": runs.stop_run,
    "resume_run": runs.resume_run,
    "get_run_summary": runs.get_run_summary,
    "recollect_run": runs.recollect_run,
    # Events
    "claim_event": events.claim_event,
    "mark_event_stage": events.mark_event_stage,
    "complete_event": events.complete_event,
    "resend_event": events.resend_event,
    # Result
    "get_result": results.get_result,
    "get_result_csv": results.get_result_csv,
    # The mapping agent's samples file
    "write_samples_file": samples_file.write_samples_file,
    # Run page
    "list_runs": run_page.list_runs,
    "show_run": run_page.show_run,
    "show_unextracted": run_page.show_unextracted,
    "result_download": run_page.result_download,
    # Service page: what the service keeps on its disk, and cleaning it up
    "service_storage": storage.service_storage,
    "clean_up_storage": storage.clean_up_storage,
    "clean_up_storage_now": storage.clean_up_storage_now,
}

#: Implemented by the bundled workflow `impl_build_mapping` (ADR 0065).
COMPOSITE_ACTIONS = {"build_mapping"}


def _mode(payload: dict[str, Any]) -> str:
    mode = as_text(payload.get("mode")).lower()
    if mode:
        return mode
    if isinstance(payload.get("query"), dict):
        return "query"
    if isinstance(payload.get("action"), dict):
        return "action"
    return ""


def _page_query(query_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A page query with the page's profile (ADR 0067): reads only, never commands or events.

    A failing service is a domain failure (`ok: false`, exit 2) so the platform's
    circuit breaker is not tripped by a service outage.
    """
    connection = _connection(payload)
    try:
        scrubber = profile_from_connection(connection).scrubber
    except Exception:  # noqa: BLE001 - an unusable profile: scrub its raw secret values
        secrets = as_dict(connection.get("secrets"))
        scrubber = Scrubber(as_text(value) for value in secrets.values())
    try:
        return scrubber.response(ok(PAGE_QUERY_HANDLERS[query_id](payload)))
    except ScraperError as exc:
        if query_id in MARKDOWN_QUERIES:
            # A markdown component shows no load error: the sentence is its content.
            return scrubber.response(
                ok({"content": f"This could not be loaded. {exc.message}", "error_code": exc.code})
            )
        return scrubber.response(
            fail(exc.code, exc.message, retryable=exc.retryable, no_external_effect=True)
        )
    except Exception as exc:  # noqa: BLE001 - the envelope must always be answered
        print(f"web-scraper: unexpected {type(exc).__name__} in {query_id}", file=sys.stderr)
        return scrubber.response(
            fail("internal_error", f"The page could not be loaded ({type(exc).__name__}).")
        )


def handle_request(payload: dict[str, Any]) -> dict[str, Any]:
    # The host envelope is checked before anything is read or sent.
    try:
        runtime = runtime_context_from_payload(payload)
    except ValueError as exc:
        response = fail("invalid_host_envelope", str(exc))
        response["definitely_no_external_effect"] = True
        return response
    mode = _mode(payload)
    if mode == "query":
        query = as_dict(payload.get("query"))
        query_id = as_text(query.get("query_id") or payload.get("query_id"))
        if query_id in PAGE_QUERY_HANDLERS:
            return _page_query(query_id, payload)
        handler = QUERY_HANDLERS.get(query_id)
        if handler is None:
            return fail("invalid_payload", f"Unknown query '{query_id}'.")
        return handler(query)
    if mode != "action":
        return fail("invalid_payload", f"Unsupported request mode '{mode}'.")
    ctx = ActionContext.from_payload(payload, runtime)
    if ctx.action_id in COMPOSITE_ACTIONS:
        return fail(
            "composite_action_direct_call",
            "build_mapping is implemented by a workflow and runs only as a workflow step.",
            no_external_effect=True,
        )
    handler_fn = ACTION_HANDLERS.get(ctx.action_id)
    if handler_fn is None:
        return fail("unknown_action", f"This extension has no action '{ctx.action_id}'.")
    scrubber: Scrubber = ctx.scrubber
    try:
        result = handler_fn(ctx)
        # Events for runs waiting on this extension (ADR 0071) go at the top of a
        # successful response, never into the result.
        resume_waits = result.pop(events.RESUME_WAITS_FIELD, None)
        response = ok(result)
        if resume_waits:
            response["resume_waits"] = resume_waits
        return scrubber.response(response)
    except ScraperError as exc:
        scrubber = ctx.scrubber
        return scrubber.response(
            fail(
                exc.code,
                exc.message,
                result=exc.result,
                retryable=exc.retryable,
                no_external_effect=exc.no_external_effect,
            )
        )
    except Exception as exc:  # noqa: BLE001 - the envelope must always be answered
        print(f"web-scraper: unexpected {type(exc).__name__}", file=sys.stderr)
        return scrubber.response(
            fail(
                "internal_error",
                f"The extension failed unexpectedly ({type(exc).__name__}: {exc}).",
            )
        )
