"""A run's final workflow output must replace its initial queue explanation."""

import pytest
from fake_service import example
from test_0_1_23 import SCRAPES, SCRAPE_INPUTS, _scrape

RUN = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"
BUSY = "All run slots are busy"


@pytest.mark.parametrize("state,stop_reason", [("running", None), ("completed", "finished"), ("failed", None), ("completed", "stopped")])
def test_summary_hint_uses_current_state_not_stale_queued_reason(invoke, service, state, stop_reason):
    run = example("get_run.response.json")
    run.update(state=state, stop_reason=stop_reason)
    run["queued_reason"] = {"code": "no_free_slot", "run_id": None, "message": BUSY}
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    result = invoke("get_run_summary", {"run_id": RUN})["result"]
    hint = result["run_page_hint"]
    assert BUSY not in hint and "is queued" not in hint
    assert RUN in hint and "Runs" in hint and "Run detail" in hint
    if state in ("completed", "failed"):
        assert state in hint and "has started" not in hint
    if stop_reason == "stopped":
        assert "stopped" in hint


def test_summary_hint_preserves_current_queue_blocker(invoke, service):
    run = example("get_run.response.json")
    run.update(state="queued", outcome=None, stop_reason=None, result=None)
    run["queued_reason"] = {"code": "no_free_slot", "run_id": None, "message": BUSY}
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    hint = invoke("get_run_summary", {"run_id": RUN})["result"]["run_page_hint"]
    assert BUSY in hint and "is queued" in hint


def test_failed_completion_check_reports_current_failure_without_queue_hint(invoke, service):
    run = example("get_run.response.json")
    run.update(state="failed", outcome=None, stop_reason=None, result=None)
    run["queued_reason"] = {"code": "no_free_slot", "run_id": None, "message": BUSY}
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    response = invoke("get_run_summary", {"run_id": RUN, "require_completed": True})
    assert response["ok"] is False
    assert response["error_code"] == "run_not_completed"
    assert "Run is failed" in response["error"] and BUSY not in response["error"]
    assert "run_page_hint" not in response.get("result", {})


@pytest.mark.parametrize("variant", sorted(SCRAPES))
def test_workflow_replaces_queued_hint_after_wait(service, tmp_path, variant):
    started = example("post_runs.response.json")
    started.update(run_id=RUN, state="queued", outcome=None, stop_reason=None, result=None)
    started["queued_reason"] = {"code": "no_free_slot", "run_id": None, "message": BUSY}
    service.override("POST", r"^/v1/runs$", 201, started)
    responses = []
    walk = _scrape(service, tmp_path, SCRAPES[variant], None, responses)
    outcome, state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_success", walk.failures
    initial = next(r["result"]["run_page_hint"] for sid, _op, r in responses if sid == "start_crawl")
    assert BUSY in initial
    hint = state["run_page_hint"]
    assert "completed" in hint and BUSY not in hint and "is queued" not in hint
    assert RUN in hint and "Run detail" in hint and "result.jsonl" in hint
