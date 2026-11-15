"""Every service error code maps to a clear extension error; run page actions; the subprocess."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import SECRETS, assert_no_secrets, make_payload

from handlers.errors import SERVICE_ERROR_MAP

ROOT = Path(__file__).resolve().parents[1]
RUN = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"

#: HTTP status of each request-level code (contract README section 11).
STATUS = {
    "invalid_json": 400,
    "idempotency_key_required": 400,
    "idempotency_key_invalid": 400,
    "invalid_cursor": 400,
    "unauthorized": 401,
    "not_found": 404,
    "run_not_found": 404,
    "event_not_found": 404,
    "mapping_request_not_found": 404,
    "sample_not_found": 404,
    "screenshot_not_found": 404,
    "method_not_allowed": 405,
    "not_acceptable": 406,
    "idempotency_key_reused": 409,
    "run_not_finished": 409,
    "nothing_to_recollect": 409,
    "site_busy": 409,
    "mapping_version_conflict": 409,
    "event_already_claimed": 409,
    "event_already_handled": 409,
    "event_stale": 409,
    "claim_superseded": 409,
    "event_claim_live": 409,
    "event_generation_conflict": 409,
    "stage_out_of_order": 409,
    "mapping_request_closed": 409,
    "result_not_ready": 409,
    "result_not_available": 409,
    "result_expired": 410,
    "request_too_large": 413,
    "unsupported_media_type": 415,
    "range_not_satisfiable": 416,
    "validation_failed": 422,
    "queue_full": 429,
    "internal_error": 500,
    "service_unavailable": 503,
    "secrets_key_unavailable": 503,
}


def test_status_table_covers_every_mapped_code():
    assert set(STATUS) == set(SERVICE_ERROR_MAP)


@pytest.mark.parametrize(
    # site_busy has a sentence of its own built from the details (tests/test_0_1_24.py).
    "code",
    sorted(set(SERVICE_ERROR_MAP) - {"site_busy"}),
)
def test_each_service_error_code(invoke, service, code):
    leaky = f"something went wrong near {SECRETS['proxy_tier2_password']}"
    service.override(
        "GET",
        r"^/v1/runs/run_",
        STATUS[code],
        {"error": {"code": code, "message": leaky, "request_id": "req_77"}},
    )
    response = invoke("get_run_summary", {"run_id": RUN})
    expected_code, expected_text, retryable = SERVICE_ERROR_MAP[code]
    assert response["ok"] is False
    assert response["error_code"] == expected_code
    assert response["error"].startswith(expected_text)
    assert "req_77" in response["error"]
    assert response.get("retryable") is retryable
    assert "[redacted]" in response["error"]  # the service's message, scrubbed
    assert response["result"]["service_error_code"] == code


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, "service_unauthorized"),
        (404, "service_not_found"),
        (409, "service_conflict"),
        (422, "config_invalid"),
        (429, "service_busy"),
        (502, "service_error"),
        (418, "service_rejected_request"),
    ],
)
def test_unknown_codes_are_read_by_status(invoke, service, status, expected):
    service.override(
        "GET",
        r"^/v1/runs/run_",
        status,
        {"error": {"code": "brand_new_code", "message": "m", "request_id": "r"}},
    )
    assert invoke("get_run_summary", {"run_id": RUN})["error_code"] == expected


def test_non_json_answers(invoke, service):
    service.override(
        "GET", r"^/v1/runs/run_", 200, b"<html>proxy page</html>", {"Content-Type": "text/html"}
    )
    assert invoke("get_run_summary", {"run_id": RUN})["error_code"] == "invalid_service_response"
    service.override("GET", r"^/v1/runs/run_", 502, b"Bad gateway", {"Content-Type": "text/plain"})
    response = invoke("get_run_summary", {"run_id": RUN})
    assert response["error_code"] == "service_error" and response["retryable"] is True


def test_run_page_actions(invoke, service):
    listed = invoke("list_runs", {"state_filter": "completed"})
    assert listed["ok"] is True, listed
    result = listed["result"]
    assert result["runs_shown"] >= 1 and result["run_1"].startswith("run_")
    query = service.calls("GET", "/v1/runs")[0]["query"]
    assert query["state"] == "completed" and query["connection_id"] == "conn_scraper_1"
    shown = invoke("show_run", {"run_id": RUN})
    assert shown["ok"] is True, shown
    summary = shown["result"]
    assert "product" in summary["templates"] and "clicks" in summary["protection"]
    assert "Started by platform job /jobs/" in summary["state"]
    assert summary["result"].startswith(
        "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1.jsonl (2.7 MB): 1318 records"
    )
    assert summary["events"] != "none"
    plain = [k for k, v in summary.items() if not isinstance(v, (dict, list))]
    assert len(plain) <= 13
    unextracted = invoke("show_unextracted", {"run_id": RUN, "reason": "incomplete"})
    assert unextracted["ok"] is True and "url_1" in unextracted["result"]
    download = invoke("result_download", {"run_id": RUN})
    assert download["ok"] is True and download["result"]["available"] is True
    # M4: the job of 'Scraper events' that recorded `processed` (it stored the
    # file), not the 'Scrape a site' job that started the run.
    assert download["result"]["result_job"] == "/jobs/job_e2a8d4410b"
    assert download["result"]["started_by_job"] == "/jobs/job_7f3c2a91e4b84d0f9c1a"
    assert "stored the file" in download["result"]["in_flow_steward"]
    assert "cannot hand out the file" in download["result"]["page_limits"]
    assert service.calls("GET", "/v1/runs")[0]["headers"]["authorization"]
    assert not service.schema_violations


def test_result_download_before_the_run_ends(invoke, service):
    from fake_service import example

    service.override("GET", r"/result$", 409, example("get_run_result.response.not_ready.json"))
    response = invoke("result_download", {"run_id": RUN})
    assert response["ok"] is True and response["result"]["available"] is False


def test_build_mapping_is_never_run_by_the_extension(invoke):
    response = invoke("build_mapping", {})
    assert response["error_code"] == "composite_action_direct_call"


def test_unknown_action_and_bad_envelope(service):
    from handlers.router import handle_request

    payload = make_payload("nope", {}, service_url=service.url)
    assert handle_request(payload)["error_code"] == "unknown_action"
    payload["contract_version"] = "extension_host_v1"
    response = handle_request(payload)
    assert response["error_code"] == "invalid_host_envelope"
    assert response["definitely_no_external_effect"] is True
    payload = make_payload("test_connection", {}, service_url=service.url)
    del payload["runtime_context"]["step_execution_id"]
    assert handle_request(payload)["error_code"] == "invalid_host_envelope"


def test_missing_connection(service):
    from handlers.router import handle_request

    response = handle_request(
        make_payload("test_connection", {}, service_url=service.url, connection=False)
    )
    assert response["error_code"] == "invalid_connection"
    assert "Scraper profile" in response["error"]


def test_subprocess_entrypoint_and_health(service):
    payload = make_payload("test_connection", {}, service_url=service.url)
    env = {"FS_ALLOW_PRIVATE_REMOTE_URLS": "1", "PATH": "/usr/local/bin:/usr/bin:/bin"}
    done = subprocess.run(
        [sys.executable, "main.py"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    response = json.loads(done.stdout)
    assert response["ok"] is True
    assert_no_secrets(response)
    assert SECRETS["service_token"] not in done.stderr
    bad = subprocess.run(
        [sys.executable, "main.py"],
        input="{oops",
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )
    assert bad.returncode == 2 and json.loads(bad.stdout)["error_code"] == "invalid_payload"
    health = subprocess.run(
        [sys.executable, "health.py"], capture_output=True, text=True, cwd=ROOT, env=env
    )
    assert json.loads(health.stdout)["ok"] is True


def test_health_query(service):
    from handlers.router import handle_request

    payload = make_payload("x", {}, service_url=service.url)
    payload["query"] = {"query_id": "extension_health"}
    del payload["action"]
    response = handle_request(payload)
    assert response["ok"] is True and response["result"]["status"] == "healthy"


def test_result_job_without_a_processed_stage_is_not_called_the_store(invoke, service):
    from fake_service import example

    run = example("get_run.response.json")
    for event in run["events"]:
        event["progress"] = {"stages": [], "last_stage": None}
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    download = invoke("result_download", {"run_id": RUN})["result"]
    assert download["result_job"] == "/jobs/job_e2a8d4410b"
    assert "may not be stored" in download["in_flow_steward"]
    run["events"] = [e for e in run["events"] if e["event_type"] != "run_completed"]
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    download = invoke("result_download", {"run_id": RUN})["result"]
    assert download["result_job"] == "none yet" and "not stored" in download["in_flow_steward"]


@pytest.mark.parametrize("state", ["queued", "running", "waiting_for_credentials", "completed"])
def test_show_run_reads_well_in_every_state(invoke, service, state):
    """A13: a young run leaves counters out or null; no line may print None, and the
    profile-data line must not be named like a secret (the platform blanks such keys)."""
    from fake_service import example

    run = example("get_run.response.json")
    run["state"] = state
    if state != "completed":
        run["outcome"] = None
        run["stop_reason"] = None
        run["result"] = None
        for group in ("counters", "protection"):
            for key in list(run[group]):
                if isinstance(run[group][key], (dict, list)):
                    continue
                if state == "queued":
                    del run[group][key]  # not sent yet
                else:
                    run[group][key] = None  # sent as null
        run["counters"]["pages_by_class"] = {}
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    response = invoke("show_run", {"run_id": RUN})
    assert response["ok"], response
    summary = response["result"]
    lines = {k: v for k, v in summary.items() if isinstance(v, str)}
    for key, line in lines.items():
        assert "None" not in line, (key, line)
        assert "secret" not in key.lower() and "token" not in key.lower(), key
    assert "stored_profile_data" in lines
    assert lines["pages"].startswith(
        ("0 fetched", f"{run['counters'].get('pages_fetched')} fetched")
    )
