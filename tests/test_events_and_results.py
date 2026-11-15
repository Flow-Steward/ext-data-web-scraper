"""Event claims and stages, resend, and the result artifact."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import ROOT_SCOPE, output_descriptor, profile_config

EVENT = "evt_01JCXF5T6V7W8X9Y0Z1A2B3C4D"
RUN = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"
OTHER_JOB = {
    **ROOT_SCOPE,
    "job_id": "9a9a9a9a-0000-4000-8000-000000000002",
    "step_id": "claim_event",
}


def _claim(invoke, scope=None):
    return invoke(
        "claim_event", {"event_id": EVENT}, scope=scope or {**ROOT_SCOPE, "step_id": "claim_event"}
    )


def test_claim_stage_complete(invoke, service):
    claim = _claim(invoke)
    assert claim["ok"] is True, claim
    result = claim["result"]
    assert result["claimed"] is True and result["route"] == "run_completed"
    assert result["processed_recorded"] is False
    claim_id = result["claim_id"]
    body = service.calls("POST", f"/v1/events/{EVENT}/claim")[0]["body"]
    assert body["claimant"] == {
        "job_id": ROOT_SCOPE["job_id"],
        "step_id": "claim_event",
        "workflow_id": "wf_scrape",
    }
    # The same job claims again: same claim.
    assert _claim(invoke)["result"]["claim_id"] == claim_id
    assert (
        invoke("mark_event_stage", {"event_id": EVENT, "claim_id": claim_id, "stage": "completed"})[
            "error_code"
        ]
        == "stage_out_of_order"
    )
    staged = invoke(
        "mark_event_stage", {"event_id": EVENT, "claim_id": claim_id, "stage": "processed"}
    )
    assert staged["ok"] is True and staged["result"]["last_stage"] == "processed"
    assert (
        invoke("mark_event_stage", {"event_id": EVENT, "claim_id": claim_id, "stage": "processed"})[
            "ok"
        ]
        is True
    )
    assert (
        invoke("mark_event_stage", {"event_id": EVENT, "claim_id": claim_id, "stage": "completed"})[
            "ok"
        ]
        is True
    )
    done = invoke("complete_event", {"event_id": EVENT, "claim_id": claim_id})
    assert done["ok"] is True and done["result"]["state"] == "handled"
    assert not service.schema_violations


def test_second_run_is_told_to_stop_not_failed(invoke, service):
    assert _claim(invoke)["result"]["claimed"] is True
    other = _claim(invoke, scope=OTHER_JOB)
    assert other["ok"] is True
    assert other["result"]["claimed"] is False and other["result"]["status"] == "already_claimed"
    assert other["result"]["claimant_job_id"] == ROOT_SCOPE["job_id"]


def test_handled_and_stale_events(invoke, service):
    claim_id = _claim(invoke)["result"]["claim_id"]
    invoke("complete_event", {"event_id": EVENT, "claim_id": claim_id})
    handled = _claim(invoke, scope=OTHER_JOB)
    assert handled["result"]["status"] == "already_handled"
    service.events.clear()
    _claim(invoke)
    service.events[EVENT]["live"] = False
    stale = _claim(invoke, scope=OTHER_JOB)
    assert stale["result"]["status"] == "stale"


def test_superseded_claim_after_resend_and_resume(invoke, service):
    first = _claim(invoke)["result"]["claim_id"]
    invoke("mark_event_stage", {"event_id": EVENT, "claim_id": first, "stage": "processed"})
    # The operator resends while the claim is live: refused without confirmation.
    refused = invoke("resend_event", {"event_id": EVENT, "resend_mode": "resume"})
    assert refused["error_code"] == "event_claim_live"
    assert "revoke_live_claim" in refused["error"]
    assert f"/jobs/{ROOT_SCOPE['job_id']}" in refused["error"]
    resent = invoke(
        "resend_event",
        {
            "event_id": EVENT,
            "resend_mode": "resume",
            "revoke_live_claim": True,
            "expected_generation": 1,
        },
    )
    assert resent["ok"] is True, resent
    assert resent["result"]["delivery_generation"] == 2
    body = service.calls("POST", f"/v1/events/{EVENT}/resend")[-1]["body"]
    assert body["webhook"]["signing_secret"] and body["revoke_live_claim"] is True
    # The old run's late calls are refused.
    late = invoke("complete_event", {"event_id": EVENT, "claim_id": first})
    assert late["error_code"] == "claim_superseded"
    assert (
        invoke("mark_event_stage", {"event_id": EVENT, "claim_id": first, "stage": "completed"})[
            "error_code"
        ]
        == "claim_superseded"
    )
    # The new run resumes after 'processed'.
    second = _claim(invoke, scope=OTHER_JOB)["result"]
    assert second["claimed"] is True and second["processed_recorded"] is True
    assert second["resend_mode"] == "resume"
    # A double click on Resend with the old generation is harmless.
    again = invoke(
        "resend_event",
        {
            "event_id": EVENT,
            "resend_mode": "from_start",
            "revoke_live_claim": True,
            "expected_generation": 1,
        },
    )
    assert again["error_code"] == "event_generation_conflict"
    fresh = invoke(
        "resend_event",
        {"event_id": EVENT, "resend_mode": "from the beginning", "revoke_live_claim": "true"},
    )
    assert fresh["ok"] is True
    assert _claim(invoke)["result"]["processed_recorded"] is False
    assert not service.schema_violations


def test_resend_needs_the_webhook(invoke, service):
    response = invoke(
        "resend_event", {"event_id": EVENT}, config=profile_config(service.url, webhook_url="")
    )
    assert response["error_code"] == "invalid_connection"


def _result(invoke, tmp_path: Path, **kwargs):
    descriptor = output_descriptor(tmp_path, **kwargs)
    response = invoke(
        "get_result", {"run_id": RUN}, artifacts={"inputs": [], "outputs": [descriptor]}
    )
    return response, Path(descriptor["_path"])


def test_result_is_stored_and_described(invoke, service, tmp_path):
    response, stored = _result(invoke, tmp_path)
    assert response["ok"] is True, response
    result = response["result"]
    assert stored.read_bytes() == service.result_bytes
    assert result["artifact_handle"] == "artifact:res_out"
    assert result["sha256"] == hashlib.sha256(service.result_bytes).hexdigest()
    assert result["record_count"] == service.result_bytes.count(b"\n")
    assert result["format"] == "jsonl"
    from fake_service import schema_errors

    # file_name is the run id form (see test_result_file_name_is_not_taken_for_a_secret);
    # the rest of the descriptor is the contract's.
    assert result["file_name"] == f"{RUN}.jsonl"
    contract_name = {**result, "file_name": f"lumen-shop.example_{RUN}.jsonl"}
    assert not schema_errors("result_descriptor.schema.json", contract_name)
    for line in stored.read_text().splitlines():
        json.loads(line)


def test_checksum_size_and_count_mismatch_store_nothing(invoke, service, tmp_path):
    service.result_descriptor_patch = {"sha256": "0" * 64}
    response, stored = _result(invoke, tmp_path)
    assert response["error_code"] == "result_checksum_mismatch"
    assert not stored.exists()
    service.result_descriptor_patch = {"record_count": 999}
    response, stored = _result(invoke, tmp_path)
    assert response["error_code"] == "result_record_count_mismatch"
    assert not stored.exists()
    service.result_descriptor_patch = {"size_bytes": len(service.result_bytes) + 10}
    response, stored = _result(invoke, tmp_path)
    assert response["error_code"] in {"result_size_mismatch", "service_download_failed"}
    assert not stored.exists()


def test_result_larger_than_the_grant(invoke, service, tmp_path):
    response, stored = _result(invoke, tmp_path, max_size=100)
    assert response["error_code"] == "result_too_large"
    assert "FS_EXTENSION_ARTIFACT_MAX_BYTES" in response["error"]
    assert not service.calls("GET", f"/v1/runs/{RUN}/result/download")
    assert not stored.exists()


def test_interrupted_download_resumes_with_range(invoke, service, tmp_path):
    service.result_bytes = b"".join(
        json.dumps({"n": i, "pad": "x" * 200}).encode() + b"\n" for i in range(4000)
    )
    service.download_cut_after = 50_000
    response, stored = _result(invoke, tmp_path)
    assert response["ok"] is True, response
    assert stored.read_bytes() == service.result_bytes
    ranges = [
        c["headers"].get("range") for c in service.calls("GET", f"/v1/runs/{RUN}/result/download")
    ]
    assert ranges[0] is None and ranges[-1] == "bytes=50000-"


def test_empty_result(invoke, service, tmp_path):
    """A36: a 0-byte upload is refused by the platform (artifact_upload_missing, row 71),
    so an empty result is reported, not stored; 'Scraper events' skips the step."""
    service.result_bytes = b""
    response, stored = _result(invoke, tmp_path)
    assert response["error_code"] == "result_empty" and response["retryable"] is False
    assert not stored.exists()
    assert not service.calls("GET", f"/v1/runs/{RUN}/result/download")


@pytest.mark.parametrize(
    ("configured", "budget"),
    [(None, 280.0), ("300", 280.0), ("10", 10.0), ("99999", 3580.0), ("junk", 280.0)],
)
def test_time_budget_follows_the_operation_timeout(monkeypatch, configured, budget):
    from handlers import results

    if configured is None:
        monkeypatch.delenv(results.OPERATION_TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(results.OPERATION_TIMEOUT_ENV, configured)
    assert results.budget_seconds() == budget


def test_no_time_left_fails_retryably_and_stores_nothing(invoke, service, tmp_path, monkeypatch):
    from handlers import results

    monkeypatch.setattr(results, "budget_seconds", lambda: 30.0)  # below the upload reserve
    response, stored = _result(invoke, tmp_path)
    assert response["error_code"] == "service_timeout" and response["retryable"] is True
    assert "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS" in response["error"]
    assert not service.calls("GET", f"/v1/runs/{RUN}/result/download")
    assert not stored.exists()


def test_slow_download_stops_at_the_deadline(invoke, service, tmp_path, monkeypatch):
    import time as clock

    from handlers import results

    # Download deadline = start + 41.5 - 40 s: the 3 s stall must end the step
    # cleanly within about two seconds, never after the host's kill.
    monkeypatch.setattr(results, "budget_seconds", lambda: 41.5)
    service.delays[("GET", r"/result/download$")] = 3.0
    begin = clock.monotonic()
    response, stored = _result(invoke, tmp_path)
    assert clock.monotonic() - begin < 3.0
    assert response["error_code"] == "service_timeout" and response["retryable"] is True
    assert not stored.exists()


def test_result_not_ready_failed_expired(invoke, service, tmp_path):
    from fake_service import example

    service.override("GET", r"/result$", 409, example("get_run_result.response.not_ready.json"))
    response, _ = _result(invoke, tmp_path)
    assert response["error_code"] == "result_not_ready"
    assert "Run state" in response["error"]
    service.override(
        "GET",
        r"/result$",
        409,
        {"error": {"code": "result_not_available", "message": "failed", "request_id": "r"}},
    )
    assert _result(invoke, tmp_path)[0]["error_code"] == "result_not_available"
    service.override(
        "GET",
        r"/result$",
        410,
        {"error": {"code": "result_expired", "message": "gone", "request_id": "r"}},
    )
    assert _result(invoke, tmp_path)[0]["error_code"] == "result_expired"


def test_result_outside_a_workflow_step(invoke, service):
    response = invoke("get_result", {"run_id": RUN})
    assert response["error_code"] == "artifact_write_failed"


def test_unknown_event_type_is_routed_like_a_problem(invoke, service):
    from fake_service import example

    claim = example("post_event_claim.response.json")
    claim["event"]["event_type"] = "quota_warning"  # a later, additive event type
    service.override("POST", r"/claim$", 200, claim)
    result = _claim(invoke)["result"]
    assert result["claimed"] is True
    assert result["event_type"] == "quota_warning" and result["route"] == "problem"


def test_result_file_name_is_not_taken_for_a_secret(invoke, service, tmp_path):
    """The platform's `dotted_provider_token` pattern (secret_detection.py:176-183 on
    flow-steward/main, copied here) matched `com_run_...` of `<site>_<run id>.jsonl`."""
    import re

    dotted = re.compile(
        r"(?<![A-Za-z0-9_-])(?<!process\.)([A-Za-z][A-Za-z0-9_-]{1,15}\.[A-Za-z0-9_-]{24,})"
        r"(?![A-Za-z0-9_-])"
    )
    assert dotted.search(f"books.toscrape.com_{RUN}.jsonl")  # what the service names it
    response, _stored = _result(invoke, tmp_path)
    assert response["result"]["file_name"] == f"{RUN}.jsonl"
    assert not dotted.search(response["result"]["file_name"])
