"""Event claims, stages, completion and resend (contract section 8)."""

from __future__ import annotations

from typing import Any

from handlers.common import as_dict, as_list, as_text
from handlers.context import ActionContext
from handlers.errors import ScraperError
from handlers.runs import get_run, record_count_of

#: Claim answers that end the handler run quietly instead of failing it.
_NOT_OURS = {
    "event_already_claimed": "already_claimed",
    "event_already_handled": "already_handled",
    "event_stale": "stale",
}
KNOWN_EVENT_TYPES = {"run_completed", "mapping_required", "problem", "run_failed"}
#: The events that end a run: a Scrape a site run that waits for this run (Flow Steward
#: "Wait for an event", ADR 0071) is woken by them, keyed by the run id.
RESUME_EVENT_TYPES = ("run_completed", "run_failed")
#: Where a handler leaves the items the router puts at the top of the response as
#: `resume_waits`; never part of the action's result.
RESUME_WAITS_FIELD = "_resume_waits"


def resume_wait_item(
    event_type: str, run_id: str, run: dict[str, Any], event: dict[str, Any]
) -> dict[str, Any]:
    """One `resume_waits` item: wakes the run that waits for `event_type` of `run_id`.

    The payload is small and holds no secrets (the host redacts it anyway): what the
    waiting workflow needs to decide its next step before it reads the run itself.
    `run` is the service's run (may be empty when it could not be read); the event's
    own outcome and stop reason fill the gaps.
    """
    state = as_text(run.get("state")) or (
        "completed" if event_type == "run_completed" else "failed"
    )
    return {
        "correlation_key": run_id,
        "event_type": event_type,
        "payload": {
            "run_id": run_id,
            "state": state,
            "outcome": as_text(run.get("outcome") or event.get("outcome")) or None,
            "stop_reason": as_text(run.get("stop_reason") or event.get("stop_reason")) or None,
            "record_count": record_count_of(run) if run else None,
        },
    }


def _resume_waits(
    ctx: ActionContext, event_type: str, run_id: str, event: dict[str, Any]
) -> list[dict[str, Any]]:
    """The `resume_waits` of a claimed run_completed / run_failed event; [] otherwise."""
    if event_type not in RESUME_EVENT_TYPES or not run_id:
        return []
    try:
        run = get_run(ctx, run_id)
    except ScraperError:
        # The wake must not depend on a second read: the event alone says how it ended.
        run = {}
    return [resume_wait_item(event_type, run_id, run, event)]


def _event_path(ctx: ActionContext, suffix: str) -> str:
    event_id = ctx.require_text("event_id", "Event id")
    return f"/v1/events/{ctx.profile.client().segment(event_id)}/{suffix}"


def claim_event(ctx: ActionContext) -> dict[str, Any]:
    runtime = ctx.runtime
    if not runtime.job_id:
        raise ScraperError(
            "invalid_payload",
            "claim_event runs only as a workflow step: the platform job id is missing.",
            no_external_effect=True,
        )
    claimant: dict[str, Any] = {
        "job_id": runtime.job_id,
        "step_id": runtime.step_id or "claim_event",
    }
    if runtime.workflow_id:
        claimant["workflow_id"] = runtime.workflow_id
    event_id = ctx.require_text("event_id", "Event id")
    try:
        answer = as_dict(
            ctx.profile.client().post(_event_path(ctx, "claim"), {"claimant": claimant})
        )
    except ScraperError as exc:
        status = _NOT_OURS.get(exc.service_code)
        if status is None:
            raise
        details = as_dict(exc.result.get("details"))
        holder = as_text(as_dict(details.get("claimant")).get("job_id"))
        return {
            "claimed": False,
            "status": status,
            "event_id": event_id,
            "claim_id": "",
            "event_type": "",
            "route": "",
            "run_id": "",
            "resource_id": "",
            "outcome": None,
            "stop_reason": None,
            "problem_code": None,
            "template_id": None,
            "event_message": "",
            "delivery_generation": None,
            "resend_mode": None,
            "last_stage": None,
            "stages": [],
            "processed_recorded": False,
            "completed_recorded": False,
            "expires_at": as_text(details.get("expires_at")) or None,
            "claimant_job_id": holder,
            "message": exc.message,
        }
    event = as_dict(answer.get("event"))
    progress = as_dict(answer.get("progress"))
    last_stage = as_text(progress.get("last_stage"))
    event_type = as_text(event.get("event_type"))
    run_id = as_text(event.get("run_id"))
    return {
        RESUME_WAITS_FIELD: _resume_waits(ctx, event_type, run_id, event),
        "claimed": True,
        "status": "claimed",
        "event_id": event_id,
        "claim_id": as_text(answer.get("claim_id")),
        "event_type": event_type,
        # Contract section 1: an unknown event type is handled like `problem`.
        "route": event_type if event_type in KNOWN_EVENT_TYPES else "problem",
        "run_id": run_id,
        "resource_id": as_text(event.get("resource_id")),
        "outcome": event.get("outcome"),
        "stop_reason": event.get("stop_reason"),
        "problem_code": event.get("problem_code"),
        "template_id": event.get("template_id"),
        "event_message": as_text(event.get("message")),
        "delivery_generation": answer.get("delivery_generation"),
        "resend_mode": answer.get("resend_mode"),
        "last_stage": last_stage or None,
        "stages": [as_text(as_dict(s).get("stage")) for s in as_list(progress.get("stages"))],
        "processed_recorded": last_stage in {"processed", "completed"},
        "completed_recorded": last_stage == "completed",
        "expires_at": answer.get("expires_at"),
        "claimant_job_id": "",
        "message": "Event claimed"
        + (f"; resuming after stage '{last_stage}'" if last_stage else "")
        + ".",
    }


def _claim_id(ctx: ActionContext) -> str:
    return ctx.require_text("claim_id", "Claim id")


def mark_event_stage(ctx: ActionContext) -> dict[str, Any]:
    stage = ctx.require_text("stage", "Stage").lower()
    if stage not in {"processed", "completed"}:
        raise ScraperError(
            "invalid_payload", "Stage must be 'processed' or 'completed'.", no_external_effect=True
        )
    answer = as_dict(
        ctx.profile.client().post(
            _event_path(ctx, "stage"), {"claim_id": _claim_id(ctx), "stage": stage}
        )
    )
    progress = as_dict(answer.get("progress"))
    return {
        "event_id": as_text(answer.get("event_id")),
        "state": as_text(answer.get("state")),
        "stage": stage,
        "last_stage": progress.get("last_stage"),
        "message": f"Stage '{stage}' recorded.",
    }


def complete_event(ctx: ActionContext) -> dict[str, Any]:
    answer = as_dict(
        ctx.profile.client().post(_event_path(ctx, "complete"), {"claim_id": _claim_id(ctx)})
    )
    return {
        "event_id": as_text(answer.get("event_id")),
        "state": as_text(answer.get("state")),
        "handled_at": answer.get("handled_at"),
        "message": "Event marked as handled.",
    }


def resend_event(ctx: ActionContext) -> dict[str, Any]:
    """Run page action: revoke the current claim and deliver the event again."""
    profile = ctx.profile
    mode = ctx.text("resend_mode", "resume").lower()
    if mode in {"from_beginning", "from the beginning", "restart"}:
        mode = "from_start"
    if mode not in {"resume", "from_start"}:
        raise ScraperError(
            "invalid_payload",
            "Resend mode must be 'resume' or 'from_start'.",
            no_external_effect=True,
        )
    body: dict[str, Any] = {"mode": mode, "webhook": profile.require_webhook()}
    revoke = ctx.input.get("revoke_live_claim")
    if revoke is True or as_text(revoke).lower() in {"true", "yes", "1", "on"}:
        body["revoke_live_claim"] = True
    generation = ctx.input.get("expected_generation")
    if generation is not None and generation != "":
        try:
            body["expected_generation"] = int(generation)
        except (TypeError, ValueError):
            pass
    answer = as_dict(profile.client().post(_event_path(ctx, "resend"), body))
    delivery = as_dict(answer.get("delivery"))
    return {
        "event_id": as_text(answer.get("event_id")),
        "event_type": as_text(answer.get("event_type")),
        "state": as_text(answer.get("state")),
        "delivery_generation": delivery.get("generation"),
        "resend_mode": mode,
        "message": (
            "The event is being delivered again; the next run resumes after the recorded stages."
            if mode == "resume"
            else "The event is being delivered again; the next run repeats every step."
        ),
    }
