"""0.1.23: Scrape a site waits for its own run (Flow Steward 0.9.18 "Wait for an event",
ADR 0071) and returns the files; Scraper events wakes it with `resume_waits`. A54 (service
7d98f20): mapping gaps fail the mapping test, price status in the result and on Run detail.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import (
    ROOT_SCOPE,
    csv_output_descriptor,
    make_payload,
    output_descriptor,
    samples_descriptor,
)
from fake_service import example
from test_templates import EVENT_INPUTS, IMPL, SCRAPE_INPUTS, _action, _tabular
from workflow_sim import StepFailed, Walk

ROOT = Path(__file__).resolve().parents[1]
EXT = "flowsteward.web-scraper"
COPIES = {
    name: json.loads((ROOT / "templates" / f"{name}.bundle.json").read_text())["workflows"][0]
    for name in ("scrape_site", "scraper_events")
}
PUBLISHED = {
    wid: yaml.safe_load((ROOT / "artifacts" / "workflows" / f"{wid}.yaml").read_text())
    for wid in ("scrape_a_site", "scraper_events")
}
SCRAPES = {"copy": COPIES["scrape_site"], "published": PUBLISHED["scrape_a_site"]}
EVENTS = {"copy": COPIES["scraper_events"], "published": PUBLISHED["scraper_events"]}
OPERATIONS = {
    op["operation_id"]: op
    for op in yaml.safe_load((ROOT / "contracts/operation_manifest.yaml").read_text())["operations"]
}
#: The Wait step exactly as both variants of Scrape a site carry it.
WAIT = {
    "event": {
        "source": EXT,
        "event_types": ["run_completed", "run_failed"],
        "correlation_key": "state.scraper_run_id",
    },
    "timeout_ms": 3 * 24 * 60 * 60 * 1000,
    "output_mapping": {
        "event_type": "state.run_event.event_type",
        "payload": "state.run_event.payload",
    },
}
WAITING_PLATFORM = {
    "job_id": "3f1c2a90-1111-4222-8333-444455556666",
    "step_id": "start_crawl",
    "loop_position": None,
    "workflow_id": "wf_scrape",
    "project_id": "prj_1",
    "account_id": "acc_1",
    "connection_id": "conn_scraper_1",
}


def _step(workflow: dict, step_id: str) -> dict:
    return next(s for s in workflow["steps"] if s["step_id"] == step_id)


def _edges(workflow: dict) -> set[tuple[str, str, str]]:
    graph = workflow.get("execution_graph") or workflow["settings"]["execution_graph"]
    return {(e["source"]["id"], e["outcome"]["name"], e["target"]["id"]) for e in graph["edges"]}


def _recording(service, tmp_path, responses: list, scope=None, suppress_start=False):
    """A connector like test_templates._connector that keeps every full response
    (the top-level `resume_waits` is not part of a step's result) and fails a step with
    the extension's own message."""
    from handlers.router import handle_request

    def run(step, inputs):
        operation = step["override"]["connector"]["operation_id"]
        if suppress_start and operation == "start_run":
            return {"test_mode_status": "suppressed", "definitely_no_external_effect": True}
        artifacts = None
        if operation == "get_result":
            artifacts = {"inputs": [], "outputs": [output_descriptor(tmp_path)]}
        if operation == "get_result_csv":
            artifacts = {"inputs": [], "outputs": [csv_output_descriptor(tmp_path)]}
        if operation == "write_samples_file":
            artifacts = {"inputs": [], "outputs": [samples_descriptor(tmp_path)]}
        payload = make_payload(
            operation, inputs, service_url=service.url, artifacts=artifacts, scope=scope
        )
        response = handle_request(payload)
        responses.append((step["step_id"], operation, response))
        if not response["ok"]:
            raise StepFailed(f"{step['step_id']}: {response['error_code']}: {response['error']}")
        declared = {row["name"] for row in OPERATIONS[operation]["outputs"]}
        assert declared <= set(response["result"]), declared - set(response["result"])
        assert "_resume_waits" not in response["result"]
        return response["result"]

    return run


def _host_checks(item: dict[str, Any]) -> None:
    """What 0.9.18 `checked_wait_event` refuses (workflow_wait_contract.py)."""
    key = item["correlation_key"]
    assert isinstance(key, str) and key.strip() and len(key) <= 256
    assert re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", item["event_type"])
    assert isinstance(item["payload"], dict)
    assert len(json.dumps(item["payload"], separators=(",", ":")).encode()) <= 64 * 1024


def _scraper_events(service, tmp_path, workflow, event_type: str, run_id: str, responses):
    """Scraper events taking the service's event of `run_id`; returns the resume_waits
    of its responses (what the host stores in its inbox)."""
    event_id = f"evt_01{event_type.upper().replace('_', '')[:8]}{run_id[-16:]}"
    service.event_bodies[event_id] = {
        "event_type": event_type,
        "run_id": run_id,
        "resource_id": run_id,
        **({"outcome": None, "stop_reason": None} if event_type == "run_failed" else {}),
    }
    scope = {**ROOT_SCOPE, "job_id": "9e9e9e9e-0000-4000-8000-00000000e001"}
    walk = Walk(
        workflow,
        connector=_recording(service, tmp_path, responses, scope),
        tabular=_tabular([]),
    )
    inputs = {
        **EVENT_INPUTS,
        "event_id": event_id,
        "event_type": event_type,
        "run_id": run_id,
        "resource_id": run_id,
    }
    outcome, _state = walk.run(inputs)
    assert outcome == "stop_success", walk.failures
    items = [item for _sid, _op, r in responses for item in r.get("resume_waits") or []]
    for item in items:
        _host_checks(item)
    return walk, items


def _scrape(service, tmp_path, workflow, wakes, responses, **kwargs):
    return Walk(
        workflow,
        connector=_recording(service, tmp_path, responses, **kwargs),
        invoke=_action(service, tmp_path, []),
        child=IMPL,
        wait=wakes,
        # tabular_feed read: the dataset and its first rows (tabular_feed_step_runtime).
        tabular=lambda _step, _inputs: {
            "dataset_handle": "dataset:x",
            "row_count": 2,
            "preview_rows": [{"data": {"title": "Pendelleuchte Aurora 60"}}],
        },
    )


# ------------------------------------------------------------------- the shape


@pytest.mark.parametrize("variant", sorted(SCRAPES))
def test_scrape_a_site_waits_for_its_own_run(variant):
    workflow = SCRAPES[variant]
    wait = _step(workflow, "wait_for_run")
    assert wait["step_kind"] == "wait" and wait["override"]["wait"] == WAIT
    assert wait["error_policy"] == {"on_error": "route_to_step"}
    edges = _edges(workflow)
    assert ("crawl_started", "true", "wait_for_run") in edges
    assert ("crawl_started", "false", "test_run_only") in edges
    # Woken or timed out: the run is read either way.
    assert {("wait_for_run", "success", "check_run"), ("wait_for_run", "error", "check_run")} <= (
        edges
    )
    assert ("check_run", "success", "has_records") in edges
    assert not [e for e in edges if e[0] == "check_run" and e[1] == "error"]
    check = _step(workflow, "check_run")
    assert check["input_mapping_jsonb"] == {
        "run_id": "state.scraper_run_id",
        "require_completed": "literal:true",
    }
    start = _step(workflow, "start_crawl")
    assert start["output_mapping_jsonb"]["run_id"] == "state.scraper_run_id"
    from handlers.runs import WAITING_START_STEP

    assert WAITING_START_STEP == start["step_id"]
    assert "started" not in {s["step_id"] for s in workflow["steps"]}


@pytest.mark.parametrize("variant", sorted(SCRAPES))
def test_scrape_a_site_returns_the_files_and_the_summary(variant):
    callable_ = SCRAPES[variant]["settings"]["callable"]
    assert callable_["output_mode"] == "variables_and_artifacts"
    assert [r["name"] for r in callable_["expected_outputs"]] == [
        "scraper_run_id",
        "run_page_hint",
        "mapping_summary",
        "run_summary",
        "result",
        "result_preview",
        "skipped",  # 0.1.24 (A66): a start on a busy site
        "skipped_reason",
    ]
    assert [r["name"] for r in callable_["expected_artifacts"]] == ["result_file", "result_csv"]
    assert not any(r.get("required") for r in callable_["expected_outputs"])
    returned = {
        s["step_id"]: s["override"]["returned_files"]
        for s in SCRAPES[variant]["steps"]
        if s.get("override", {}).get("returned_files")
    }
    assert returned == {
        "get_result": [{"result": "artifact_handle", "key": "result_file"}],
        "get_result_csv": [{"result": "artifact_handle", "key": "result_csv"}],
    }


@pytest.mark.parametrize("variant", sorted(SCRAPES))
def test_run_title_and_run_name(variant):
    workflow = SCRAPES[variant]
    template = workflow["settings"]["run_title"]
    assert template == "{{ inputs.run_name }} {{ inputs.site_url }}"
    declared = {r["name"]: r for r in workflow["settings"]["callable"]["expected_inputs"]}
    for name in re.findall(r"\{\{\s*inputs\.([a-z_]+)\s*\}\}", template):
        assert name in declared and declared[name]["type"] == "string"
    assert declared["run_name"]["required"] is False
    # Only a title: the run name is never sent to the scraper.
    assert "inputs.run_name" not in json.dumps(workflow["steps"])


def test_scraper_events_leaves_the_files_to_a_waiting_starter():
    for workflow in EVENTS.values():
        edges = _edges(workflow)
        assert ("run_summary", "success", "starter_fetches") in edges
        assert ("starter_fetches", "true", "complete_event") in edges
        assert ("starter_fetches", "false", "already_processed") in edges
        mapping = _step(workflow, "run_summary")["output_mapping_jsonb"]
        assert mapping["result_by_starter"] == "state.run.result_by_starter"
    # The published one keeps on_error for problems only.
    published = _edges(EVENTS["published"])
    assert ("starter_fetches", "true", "complete_event") in published


# ------------------------------------------------------- the whole cycle (walker)


@pytest.mark.parametrize("variant", sorted(SCRAPES))
def test_full_cycle_start_wait_resume_files(service, tmp_path, variant):
    """Scrape a site starts the run and parks; Scraper events takes the service's
    run_completed and answers resume_waits; that wakes the waiting run, which stores
    result.jsonl and result.csv and returns them with the summary and a preview."""
    service.run_patch = {"platform": WAITING_PLATFORM}
    scrape_responses: list = []
    events_responses: list = []
    events_walks = []

    def wakes(step, pending):
        assert step["step_id"] == "wait_for_run"
        assert pending["source"] == EXT
        assert pending["correlation_key"].startswith("run_01FAKE")
        walk, items = _scraper_events(
            service,
            tmp_path,
            EVENTS[variant],
            "run_completed",
            pending["correlation_key"],
            events_responses,
        )
        events_walks.append(walk)
        # The control process delivers only an event of this key and a listed type;
        # an event of another run never wakes this one.
        other = {**items[0], "correlation_key": "run_01OTHERRUN000000000000000"}
        return [other, *items]

    walk = _scrape(service, tmp_path, SCRAPES[variant], wakes, scrape_responses)
    outcome, state = walk.run({**SCRAPE_INPUTS, "run_name": "Supplier A"})
    assert outcome == "stop_success" and walk.trace[-1] == "done", walk.failures
    run_id = state["scraper_run_id"]
    assert walk.waits == [
        {
            "step_id": "wait_for_run",
            "source": EXT,
            "event_types": ["run_completed", "run_failed"],
            "correlation_key": run_id,
            "timeout_ms": WAIT["timeout_ms"],
        }
    ]
    # Scraper events woke it and stored nothing itself.
    events = events_walks[0]
    assert "starter_fetches" in events.trace and "get_result" not in events.trace
    assert events.trace[-2:] == ["complete_event", "handled"]
    claim = next(r for sid, _op, r in events_responses if sid == "claim_event")
    assert claim["resume_waits"] == [
        {
            "correlation_key": run_id,
            "event_type": "run_completed",
            "payload": {
                "run_id": run_id,
                "state": "completed",
                "outcome": "complete",
                "stop_reason": "finished",
                "record_count": state["run"]["record_count"],
            },
        }
    ]
    assert state["run_event"]["event_type"] == "run_completed"
    assert state["run_event"]["payload"]["run_id"] == run_id
    # Scrape a site stored the files and returns them.
    assert walk.trace[walk.trace.index("wait_for_run") :] == [
        "wait_for_run",
        "check_run",
        "has_records",
        "get_result",
        "has_csv",
        "get_result_csv",
        "read_result",
        "done",
    ]
    assert walk.artifacts == {"result_file": "artifact:res_out", "result_csv": "artifact:res_csv"}
    assert set(walk.outputs) >= {"scraper_run_id", "run_summary", "result", "result_preview"}
    assert walk.outputs["result"]["record_count"] == service.result_bytes.count(b"\n")
    assert service.downloads == ["jsonl", "csv"]
    assert (tmp_path / "result-artifact.jsonl").read_bytes() == service.result_bytes
    assert not service.schema_violations


def test_an_older_run_still_gets_its_files_in_scraper_events(service, tmp_path):
    """A run started by a copy before 0.1.23 (step start_run) or by Re-collect: nobody
    waits, so Scraper events stores the files as before; its resume is harmless."""
    responses: list = []
    walk, items = _scraper_events(
        service, tmp_path, EVENTS["copy"], "run_completed", EVENT_INPUTS["run_id"], responses
    )
    assert "get_result" in walk.trace and "mark_processed" in walk.trace
    assert service.downloads == ["jsonl", "csv"]
    assert [i["event_type"] for i in items] == ["run_completed"]


@pytest.mark.parametrize("variant", sorted(SCRAPES))
def test_a_failed_run_fails_scrape_a_site_with_its_summary(service, tmp_path, variant):
    service.run_patch = {
        "platform": WAITING_PLATFORM,
        "state": "failed",
        "outcome": None,
        "stop_reason": None,
        "result": None,
    }
    responses: list = []

    def wakes(step, pending):
        _walk, items = _scraper_events(
            service, tmp_path, EVENTS[variant], "run_failed", pending["correlation_key"], []
        )
        assert [i["event_type"] for i in items] == ["run_failed"]
        assert items[0]["payload"]["state"] == "failed"
        return items

    walk = _scrape(service, tmp_path, SCRAPES[variant], wakes, responses)
    outcome, _state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "failed" and walk.trace[-1] == "check_run"
    assert "run_not_completed" in walk.failures[-1]
    assert "did not complete. Run is failed" in walk.failures[-1]
    assert "still going on" not in walk.failures[-1]
    assert not service.downloads


def test_timeout_reads_the_run_and_still_gets_a_completed_result(service, tmp_path):
    """The webhook never came: after the timeout the run is read; it completed, so the
    files are stored anyway."""
    service.run_patch = {"platform": WAITING_PLATFORM}
    walk = _scrape(
        service, tmp_path, SCRAPES["published"], lambda _s, _p: [{"timed_out": True}], []
    )
    outcome, _state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_success" and walk.trace[-1] == "done"
    assert "workflow.wait.event_timed_out" in walk.failures[0]
    assert set(walk.artifacts) == {"result_file", "result_csv"}


def test_timeout_on_a_run_still_going_on_fails_plainly(service, tmp_path):
    service.run_patch = {
        "platform": WAITING_PLATFORM,
        "state": "running",
        "outcome": None,
        "stop_reason": None,
        "result": None,
    }
    walk = _scrape(service, tmp_path, SCRAPES["copy"], lambda _s, _p: [{"timed_out": True}], [])
    outcome, _state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "failed" and walk.trace[-2:] == ["wait_for_run", "check_run"]
    assert "It is still going on" in walk.failures[-1]
    assert not service.downloads


def test_a_run_without_records_ends_without_files(service, tmp_path):
    service.run_patch = {
        "platform": WAITING_PLATFORM,
        "result": None,
        "counters": {**example("get_run.response.json")["counters"], "records_extracted": 0},
    }

    def wakes(_step, pending):
        return [{"event_type": "run_completed", "correlation_key": pending["correlation_key"]}]

    walk = _scrape(service, tmp_path, SCRAPES["copy"], wakes, [])
    outcome, state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_success" and walk.trace[-1] == "no_records"
    assert "produced no records" in state["run_summary"] and not walk.artifacts


def test_a_test_run_does_not_wait(service, tmp_path):
    walk = _scrape(service, tmp_path, SCRAPES["published"], None, [], suppress_start=True)
    outcome, _state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_success" and walk.trace[-1] == "test_run_only"
    assert not walk.waits


# ------------------------------------------------------------ the handlers


def test_claim_event_resumes_only_finished_or_failed_runs(invoke, service):
    run_id = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"
    scope = {**ROOT_SCOPE, "step_id": "claim_event"}
    completed = invoke("claim_event", {"event_id": "evt_01COMPLETED0000000000000001"}, scope=scope)
    assert completed["resume_waits"][0]["correlation_key"] == run_id
    assert set(completed["resume_waits"][0]["payload"]) == {
        "run_id",
        "state",
        "outcome",
        "stop_reason",
        "record_count",
    }
    for event_type in ("mapping_required", "problem"):
        event_id = f"evt_01{event_type.upper().replace('_', '')[:10]:0<26}"
        service.event_bodies[event_id] = {
            "event_type": event_type,
            "outcome": None,
            "stop_reason": None,
            **(
                {"problem_code": "site_unavailable"}
                if event_type == "problem"
                else {"resource_id": "mrq_01JCXB1G0Z9Y8X7W6V5T4S3R2Q"}
            ),
        }
        response = invoke("claim_event", {"event_id": event_id}, scope=scope)
        assert response["ok"] and "resume_waits" not in response
    # Another job's claim: not ours, so no wake from this job.
    other = {**scope, "job_id": "9a9a9a9a-0000-4000-8000-000000000002"}
    response = invoke("claim_event", {"event_id": "evt_01COMPLETED0000000000000001"}, scope=other)
    assert response["result"]["claimed"] is False and "resume_waits" not in response


def test_the_wake_does_not_depend_on_reading_the_run(invoke, service):
    service.override("GET", r"^/v1/runs/run_", 503, {"error": {"code": "x", "message": "down"}})
    scope = {**ROOT_SCOPE, "step_id": "claim_event"}
    response = invoke("claim_event", {"event_id": "evt_01COMPLETED0000000000000002"}, scope=scope)
    assert response["ok"]
    item = response["resume_waits"][0]
    assert item["event_type"] == "run_completed"
    assert item["payload"]["state"] == "completed" and item["payload"]["record_count"] is None


def test_other_actions_never_answer_resume_waits(invoke):
    response = invoke("get_run_summary", {"run_id": "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"})
    assert response["ok"] and "resume_waits" not in response
    assert response["result"]["result_by_starter"] is False  # the example started at start_run


def test_require_completed(invoke, service):
    run_id = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"
    assert invoke("get_run_summary", {"run_id": run_id, "require_completed": True})["ok"]
    service.run_patch = {"state": "running", "outcome": None, "stop_reason": None}
    response = invoke("get_run_summary", {"run_id": run_id, "require_completed": True})
    assert response["error_code"] == "run_not_completed"
    assert response["error"].startswith(
        f"The scraper run {run_id} did not complete. Run is running"
    )
    assert "run_not_completed" in OPERATIONS["get_run_summary"]["error_codes"]
    # Without the flag the same run is just read.
    assert invoke("get_run_summary", {"run_id": run_id})["ok"]


def test_run_detail_names_the_waiting_job(invoke, service):
    from handlers.run_page import result_text

    run = example("get_run.response.json")
    run["platform"] = dict(WAITING_PLATFORM)
    run["events"] = []
    text = result_text(run)
    assert "'Scrape a site' job" in text and WAITING_PLATFORM["job_id"] in text
    run["result"] = None
    run["state"] = "running"
    assert result_text(run).startswith("not yet: the 'Scrape a site' job")
    service.run_patch = {"platform": WAITING_PLATFORM, "events": []}
    answer = invoke("result_download", {"run_id": "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"})["result"]
    assert "'Scrape a site' job" in answer["in_flow_steward"]


# ----------------------------------------------------------- A54: mapping gaps


GAP = {
    "field": "mappings[0].fields[2]",
    "code": "price_not_extracted",
    "message": "2 of 3 matched samples show a price, but the mapping read none: read the "
    "product's price block.",
}


def _test_answer(*, passed: bool, failures: list | None = None, samples_patch=None) -> dict:
    answer = example("post_mappings_test.response.json")
    answer["passed"] = passed
    answer["templates"][0]["passed"] = passed
    if failures is not None:
        answer["templates"][0]["failures"] = failures
        answer["warnings"] = [*answer.get("warnings", []), *failures]
    for row in answer.get("samples", []):
        row.update(samples_patch or {})
    return answer


def _test_inputs():
    return {
        "site": "lumen-shop.example",
        "mappings": [example("mapping.product.json")],
        "sample_ids": ["smp_01JCXA00000000000000000001"],
    }


def test_a_mapping_gap_fails_the_test_and_goes_back_to_the_agent(invoke, service):
    service.override(
        "POST", r"^/v1/mappings/test$", 200, _test_answer(passed=False, failures=[GAP])
    )
    result = invoke("test_mapping", _test_inputs())["result"]
    assert result["passed"] is False
    gap = next(f for f in result["failures"] if f["kind"] == "mapping_gap")
    assert gap["code"] == "price_not_extracted" and gap["message"] == GAP["message"]
    assert "price block" in gap["what_to_change"]
    assert result["templates"][0]["gaps"] == [GAP["message"]]
    assert f"Not passed: product: {GAP['message']}" in result["summary"]


def test_a_gap_is_never_passed_even_if_the_answer_says_so(invoke, service):
    service.override("POST", r"^/v1/mappings/test$", 200, _test_answer(passed=True, failures=[GAP]))
    result = invoke("test_mapping", _test_inputs())["result"]
    assert result["passed"] is False and result["failures"]


def test_price_status_of_the_samples_in_the_summary(invoke, service):
    answer = _test_answer(
        passed=True,
        samples_patch={
            "price_on_page": "on_request",
            "price_status": "on_request",
            "currency_from_default": True,
        },
    )
    service.override("POST", r"^/v1/mappings/test$", 200, answer)
    result = invoke("test_mapping", _test_inputs())["result"]
    assert result["passed"] is True
    assert "Prices on the samples: price on request 2" in result["summary"]
    assert "currency taken from the default on 2 samples" in result["summary"]
    assert result["sample_results"][0]["price_on_page"] == "on_request"


def test_build_mapping_never_returns_a_mapping_with_gaps(service, tmp_path):
    """impl_build_mapping: the agent gets the gap on every refinement and the action
    returns passed: false, so Scrape a site activates nothing and starts nothing."""
    for _ in range(5):
        service.override(
            "POST", r"^/v1/mappings/test$", 200, _test_answer(passed=False, failures=[GAP])
        )
    service.mappings.clear()
    seen: list = []

    def agent(_step, inputs):
        seen.append(copy.deepcopy(inputs.get("failures")))
        return {"mappings_json": json.dumps([example("mapping.product.json")]), "notes": "x"}

    def action(_step, inputs):
        walk = Walk(IMPL, connector=_recording(service, tmp_path, []), agent=agent)
        _outcome, state = walk.run(inputs, raw_inputs=True)
        return {
            k: state.get(k)
            for k in ("mappings", "passed", "field_results", "failures", "attempts", "summary")
        }

    walk = Walk(
        SCRAPES["published"],
        connector=_recording(service, tmp_path, []),
        invoke=action,
        child=IMPL,
    )
    outcome, _state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_fail" and walk.trace[-1] == "no_mapping"
    assert any(f.get("kind") == "mapping_gap" for failures in seen[1:] for f in (failures or [])), (
        seen
    )
    assert not service.calls("PUT", "/v1/sites/") and not service.calls("POST", "/v1/runs")


def test_a_repair_gets_the_gaps_in_words(invoke, service):
    request = example("get_mapping_request.response.json")
    request["failures"] = {
        **request.get("failures", {}),
        "gaps": {"variants_not_used": 3, "price_not_extracted": 1},
    }
    service.override("GET", r"^/v1/mapping-requests/", 200, request)
    result = invoke(
        "get_mapping_request", {"mapping_request_id": "mrq_01JCXB1G0Z9Y8X7W6V5T4S3R2Q"}
    )["result"]
    reasons = result["failures"]["repair_reason"]
    assert reasons[0].startswith("3 stored pages: the page carries variant data")
    assert reasons[1].startswith("1 stored page: the page shows a price")


def test_samples_file_explains_the_sections_and_the_price():
    from handlers.samples_file import render_samples

    sample = {
        "sample_id": "smp_1",
        "url": "https://shop.example/p/1",
        "title": "P",
        "distilled_html": "<!-- wss:section product-summary at div.summary --><p class=price></p>",
        "price_on_page": "on_request",
    }
    text, _report = render_samples("shop.example", [sample])
    assert "wss:section <name> at <path>" in text and "variant_sources" in text
    assert "- Price on the page: on_request" in text


def test_the_skill_states_the_a54_rules():
    skill = yaml.safe_load((ROOT / "artifacts/skills/build_extraction_mapping.yaml").read_text())
    text = " ".join(skill["instructions"])
    assert (
        "For a product's description, list the product's own description first "
        "(#tab-description, .woocommerce-Tabs-panel--description, .product-description, "
        "[itemprop=description] inside the product, JSON-LD Product.description); use JSON-LD "
        "WebPage/WebSite description, meta name=description and og:description only as the "
        "last source, never the first." in text
    )
    for words in (
        "wss:section",
        "variant_sources",
        "price_on_page",
        "on_request and missing pages are not mapping failures",
        "price_not_extracted",
        "variants_not_used",
        "never activated while one remains",
        "repair_reason",
        "default_currency on a currency transform is only the last resort",
    ):
        assert words in text, words
    agent = yaml.safe_load((ROOT / "artifacts/agents/scraper_mapping_author.yaml").read_text())
    assert "only as the last source, never the first" in agent["prompts"]["system"]


def test_missed_pages_say_what_the_mapping_misses(service):
    from handlers.pages import mapping_gaps_text

    assert (
        mapping_gaps_text({"mapping_gaps": ["variants_not_used", "price_not_extracted"]})
        == "The page has variants the mapping does not read and shows a price the mapping "
        "did not read."
    )
    sentence = (
        "the page carries variant data (two or more variants) but the mapping declares no variants"
    )
    assert mapping_gaps_text({"mapping_gaps": ["variants_not_used"], "last_error": sentence}) == (
        sentence
    )
    assert mapping_gaps_text({"reason": "blocked"}) == ""
    grid = yaml.safe_load((ROOT / "ui/components/run_missed.yaml").read_text())
    assert {"key": "gaps", "label": "What went wrong", "type": "text"} in grid["columns"]


# ------------------------------------------------------------------ guide, version


@pytest.mark.parametrize(
    "words",
    [
        "## Use Scrape a site from your own workflow",
        "**Recipe: a supplier's products, weekly**",
        "**Recipe: competitors' prices, weekly**",
        "**Several sites from one workflow.**",
        "artifact_mapping:",
        "### Prices in the result",
        "`on_request`",
        "`price_max`",
        "`currency_from_default: true`",
        "Scrape a site waits at most **3 days**",
        "**Scrape a site keeps waiting**",
    ],
)
def test_guide(words):
    assert words in (ROOT / "docs/setup-guide.md").read_text(encoding="utf-8")
