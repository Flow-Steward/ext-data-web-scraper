"""0.1.18: the CSV beside JSON Lines (contract 7.5) and re-collecting a run's missed
pages (A45, contract 4.10), service ef08989."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from conftest import ROOT_SCOPE, csv_output_descriptor, make_payload, output_descriptor
from fake_service import example, schema_errors
from test_templates import EVENT_INPUTS, EVENTS, _connector
from workflow_sim import Walk

ROOT = Path(__file__).resolve().parents[1]
RUN = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"
DEFAULT_REASONS = [
    "fetch_failed",
    "blocked",
    "incomplete",
    "no_template",
    "template_failed",
    "stopped",
]
#: A page call: the host sends no job and no step.
PAGE_SCOPE = {**ROOT_SCOPE, "job_id": None, "step_id": None, "step_execution_id": None}


def _events_walk(service, tmp_path):
    def tabular(_step, _inputs):
        return {"dataset_handle": "dataset:x", "row_count": 3, "preview_rows": [{"t": 1}]}

    scope = {**ROOT_SCOPE, "step_id": "claim_event"}
    return Walk(EVENTS, connector=_connector(service, tmp_path, scope), tabular=tabular)


# ----------------------------------------------------------------------- CSV


def test_descriptors_with_and_without_the_csv_are_contract_valid(service):
    new = service._descriptor(RUN)
    new["fetched_at"] = "2026-11-14T11:00:00Z"
    assert "csv" in new and not schema_errors(
        "result_descriptor.schema.json#/$defs/service_result", new
    )
    service.csv_enabled = False
    old = service._descriptor(RUN)
    assert "csv" not in old and "fetched_at" not in old
    assert not schema_errors("result_descriptor.schema.json#/$defs/service_result", old)


@pytest.mark.parametrize("csv", [True, False], ids=["new service", "older service"])
def test_get_result_works_with_and_without_the_csv(invoke, service, tmp_path, csv):
    service.csv_enabled = csv
    if csv:
        service.result_descriptor_patch = {"fetched_at": "2026-11-14T11:00:00Z"}
    response = invoke(
        "get_result",
        {"run_id": RUN},
        artifacts={"inputs": [], "outputs": [output_descriptor(tmp_path)]},
    )
    assert response["ok"], response
    assert response["result"]["has_csv"] is csv
    assert response["result"]["file_name"] == f"{RUN}.jsonl"
    assert service.downloads == ["jsonl"]
    # The workflow descriptor (top level of the schema) is unchanged, plus has_csv.
    schema = json.loads(
        (ROOT / "service_contract/schemas/result_descriptor.schema.json").read_text()
    )
    assert list(response["result"]) == [*schema["properties"], "has_csv"]


def test_get_result_csv_stores_the_checked_csv(invoke, service, tmp_path):
    response = invoke(
        "get_result_csv",
        {"run_id": RUN},
        artifacts={"inputs": [], "outputs": [csv_output_descriptor(tmp_path)]},
    )
    assert response["ok"], response
    result = response["result"]
    assert result["artifact_handle"] == "artifact:res_csv"
    assert result["file_name"] == f"{RUN}.csv"
    assert result["sha256"] == hashlib.sha256(service.csv_bytes).hexdigest()
    stored = (tmp_path / "result-artifact.csv").read_bytes()
    assert stored == service.csv_bytes and stored.startswith(b"\xef\xbb\xbf")
    assert service.downloads == ["csv"]
    download = service.calls("GET", f"/v1/runs/{RUN}/result/download")[0]
    assert download["query"] == {"format": "csv"}
    assert download["headers"]["accept"] == "text/csv"


def test_get_result_csv_refuses_a_wrong_checksum(invoke, service, tmp_path):
    descriptor = service._descriptor(RUN)
    descriptor["csv"]["sha256"] = "0" * 64
    service.override("GET", rf"^/v1/runs/{RUN}/result$", 200, descriptor)
    response = invoke(
        "get_result_csv",
        {"run_id": RUN},
        artifacts={"inputs": [], "outputs": [csv_output_descriptor(tmp_path)]},
    )
    assert response["error_code"] == "result_checksum_mismatch"
    assert "CSV file" in response["error"]
    assert not (tmp_path / "result-artifact.csv").exists()


def test_get_result_csv_on_an_older_service(invoke, service, tmp_path):
    service.csv_enabled = False
    response = invoke(
        "get_result_csv",
        {"run_id": RUN},
        artifacts={"inputs": [], "outputs": [csv_output_descriptor(tmp_path)]},
    )
    assert response["error_code"] == "result_csv_missing"
    assert "Update the service image" in response["error"]
    assert not service.downloads


def test_get_result_csv_follows_only_the_service_api(invoke, service, tmp_path):
    descriptor = service._descriptor(RUN)
    descriptor["csv"]["download_path"] = f"/v1/runs/{RUN}/result/download?format=csv"
    bad = json.loads(json.dumps(descriptor))
    bad["csv"]["download_path"] = "https://elsewhere.example/x.csv"
    service.override("GET", rf"^/v1/runs/{RUN}/result$", 200, bad)
    response = invoke(
        "get_result_csv",
        {"run_id": RUN},
        artifacts={"inputs": [], "outputs": [csv_output_descriptor(tmp_path)]},
    )
    assert response["error_code"] == "invalid_service_response"


def test_scraper_events_returns_both_files(service, tmp_path):
    walk = _events_walk(service, tmp_path)
    outcome, state = walk.run(dict(EVENT_INPUTS))
    assert outcome == "stop_success" and walk.trace[-1] == "handled"
    assert walk.trace[walk.trace.index("get_result") + 1 :][:3] == [
        "has_csv",
        "get_result_csv",
        "read_result",
    ]
    assert walk.artifacts == {"result_file": "artifact:res_out", "result_csv": "artifact:res_csv"}
    assert state["result_csv"]["file_name"] == f"{RUN}.csv"


def test_scraper_events_with_an_older_service_skips_the_csv(service, tmp_path):
    service.csv_enabled = False
    walk = _events_walk(service, tmp_path)
    outcome, _state = walk.run(dict(EVENT_INPUTS))
    assert outcome == "stop_success" and "get_result_csv" not in walk.trace
    assert walk.artifacts == {"result_file": "artifact:res_out"}


def test_a_failed_csv_keeps_the_claim(service, tmp_path):
    descriptor = service._descriptor(RUN)
    descriptor["csv"]["sha256"] = "0" * 64
    # Get result reads the descriptor first, then Get result CSV.
    service.override("GET", rf"^/v1/runs/{RUN}/result$", 200, service._descriptor(RUN))
    service.override("GET", rf"^/v1/runs/{RUN}/result$", 200, descriptor)
    walk = _events_walk(service, tmp_path)
    outcome, _state = walk.run(dict(EVENT_INPUTS))
    assert outcome == "stop_fail" and walk.trace[-1] == "processing_failed"
    assert not service.calls("POST", "/v1/events/evt_01JCXF5T6V7W8X9Y0Z1A2B3C4D/stage")


def test_csv_declarations():
    steps = {s["step_id"]: s for s in EVENTS["steps"]}
    csv_step = steps["get_result_csv"]
    assert csv_step["override"]["returned_files"] == [
        {"result": "artifact_handle", "key": "result_csv"}
    ]
    assert csv_step["error_policy"] == {"on_error": "route_to_step"}
    assert steps["has_csv"]["override"]["if"]["condition"]["selector"] == "state.result.has_csv"
    policies = yaml.safe_load((ROOT / "contracts/artifact_policies.yaml").read_text())["policies"]
    csv_policy = next(p for p in policies if p["operation_id"] == "get_result_csv")
    assert csv_policy["outputs"][0]["binding_key"] == "result_csv_handle"
    assert csv_policy["outputs"][0]["content_type"] == "text/csv"
    # Get result keeps one prepared output: an older service's run still stores its file.
    get_result = next(p for p in policies if p["operation_id"] == "get_result")
    assert len(get_result["outputs"]) == 1


def test_run_detail_names_both_files_and_the_csv_address(invoke, service):
    run = example("get_run.response.json")
    run["result"]["csv"] = example("get_run_result.response.json")["csv"]
    assert not schema_errors("run_summary.schema.json", run)
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    line = invoke("show_run", {"run_id": RUN})["result"]["result"]
    assert line.startswith(f"{RUN}.jsonl (2.7 MB) and {RUN}.csv (1.9 MB): 1318 records")
    assert "panel Result (result_file, result_csv and the first rows)" in line
    download = invoke("result_download", {"run_id": RUN})["result"]
    assert f"/v1/runs/{RUN}/result/download?format=csv" in download["from_the_service"]


# ------------------------------------------------------------------- re-collect


def _recollects(service):
    return service.calls("POST", f"/v1/runs/{RUN}/recollect")


def test_recollect_as_a_workflow_step(invoke, service):
    scope = {**ROOT_SCOPE, "step_id": "recollect_missed"}
    response = invoke("recollect_run", {"run_id": RUN}, scope=scope)
    assert response["ok"], response
    call = _recollects(service)[0]
    assert call["headers"]["idempotency-key"] == f"fs:{ROOT_SCOPE['job_id']}:recollect_missed"
    body = call["body"]
    assert "reasons" not in body  # the service's default
    assert body["platform"]["job_id"] == ROOT_SCOPE["job_id"]
    assert body["platform"]["step_id"] == "recollect_missed"
    assert body["profile"]["webhook"]["url"]
    assert not service.schema_violations
    result = response["result"]
    assert result["parent_run_id"] == RUN and result["run_id"].startswith("run_01RECO")
    assert (
        result["url_count"] == 15
        and result["by_reason"] == "fetch failed 10, blocked 2, incomplete 3"
    )
    assert result["created"] is True and result["reasons"] == DEFAULT_REASONS
    assert result["message"].startswith(
        f"Run {result['run_id']} collects again 15 missed pages of run {RUN}"
    )


def test_recollect_from_the_page_is_safe_to_press_twice(invoke, service):
    inputs = {"run_id": RUN, "reasons": ["fetch_failed", "blocked"]}
    first = invoke("recollect_run", inputs, scope=PAGE_SCOPE)["result"]
    second = invoke("recollect_run", inputs, scope=PAGE_SCOPE)["result"]
    assert first["run_id"] == second["run_id"]
    assert first["created"] is True and second["created"] is False
    assert second["message"].startswith("This re-collect was already started a moment ago")
    calls = _recollects(service)
    key = calls[0]["headers"]["idempotency-key"]
    assert key.startswith("fs:page:") and key == calls[1]["headers"]["idempotency-key"]
    body = calls[0]["body"]
    assert body["reasons"] == ["fetch_failed", "blocked"]
    assert body["platform"]["job_id"].startswith("page-")
    assert body["platform"]["step_id"] == "run_detail_recollect"
    assert not service.schema_violations
    assert first["url_count"] == 12 and first["by_reason"] == "fetch failed 10, blocked 2"


def test_recollect_other_reasons_is_another_run(invoke, service):
    invoke("recollect_run", {"run_id": RUN, "reasons": ["blocked"]}, scope=PAGE_SCOPE)
    invoke("recollect_run", {"run_id": RUN, "reasons": ["fetch_failed"]}, scope=PAGE_SCOPE)
    keys = {c["headers"]["idempotency-key"] for c in _recollects(service)}
    assert len(keys) == 2


@pytest.mark.parametrize(
    ("reasons", "words"),
    [(["lost"], "'lost' is not an unextracted reason"), ({"x": 1}, "must be a list")],
)
def test_recollect_bad_reasons_send_nothing(invoke, service, reasons, words):
    response = invoke("recollect_run", {"run_id": RUN, "reasons": reasons})
    assert response["error_code"] == "invalid_payload" and words in response["error"]
    assert not _recollects(service)


def test_recollect_reasons_as_text(invoke, service):
    invoke("recollect_run", {"run_id": RUN, "reasons": "blocked, not_found"})
    assert _recollects(service)[0]["body"]["reasons"] == ["blocked", "not_found"]


def test_run_not_finished_in_plain_words(invoke, service):
    service.override(
        "POST",
        rf"^/v1/runs/{RUN}/recollect$",
        409,
        {
            "error": {
                "code": "run_not_finished",
                "message": "run is running",
                "request_id": "req_9",
                "details": {"state": "running"},
            }
        },
    )
    response = invoke("recollect_run", {"run_id": RUN})
    assert response["error_code"] == "run_not_finished"
    assert response["error"].startswith("The run has not finished yet")
    assert "Run state: running." in response["error"]
    assert response["definitely_no_external_effect"] is True


def test_nothing_to_recollect_names_the_missed_pages(invoke, service):
    service.override(
        "POST", rf"^/v1/runs/{RUN}/recollect$", 409, example("error.nothing_to_recollect.json")
    )
    response = invoke("recollect_run", {"run_id": RUN, "reasons": ["fetch_failed", "blocked"]})
    assert response["error_code"] == "nothing_to_recollect"
    assert response["error"].startswith("The run has no missed pages with these reasons")
    assert "Its missed pages by reason: not found 3." in response["error"]
    assert response["result"]["details"]["unextracted_by_reason"] == {"not_found": 3}


def test_recollect_needs_a_webhook(invoke, service):
    from conftest import profile_config

    config = profile_config(service.url, webhook_url="")
    response = invoke("recollect_run", {"run_id": RUN}, config=config)
    assert response["ok"] is False and not _recollects(service)


def test_run_detail_links_parent_and_child(invoke, service):
    parent = example("get_run.response.json")
    parent["recollected_by"] = [
        {
            "run_id": "run_01CHILD0000000000000000001",
            "state": "running",
            "created_at": "2026-11-14T11:00:00Z",
        }
    ]
    assert not schema_errors("run_summary.schema.json", parent)
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, parent)
    state = invoke("show_run", {"run_id": RUN})["result"]["state"]
    assert "Missed pages collected again by run_01CHILD0000000000000000001 (running)." in state

    child = example("get_run.response.json")
    child["run_id"] = "run_01CHILD0000000000000000001"
    child["platform"]["job_id"] = "page-0123456789abcdef01234567"
    child["recollect_of"] = {
        "run_id": RUN,
        "reasons": ["fetch_failed", "blocked"],
        "url_count": 12,
        "by_reason": {"fetch_failed": 10, "blocked": 2},
    }
    assert not schema_errors("run_summary.schema.json", child)
    service.override("GET", r"^/v1/runs/run_01CHILD", 200, child)
    result = invoke("show_run", {"run_id": child["run_id"]})["result"]
    assert "Started from the Run detail page ('Collect missed pages again')" in result["state"]
    assert (
        f"Collects again 12 missed pages of run {RUN} (fetch failed 10, blocked 2)."
        in result["state"]
    )
    assert "/jobs/page-" not in json.dumps(result)
    service.override("GET", r"^/v1/runs/run_01CHILD", 200, child)
    message = invoke("get_run_summary", {"run_id": child["run_id"]})["result"]["message"]
    assert f"Collects again 12 missed pages of run {RUN}" in message


def test_a_child_run_completed_event_goes_through_scraper_events(service, tmp_path):
    event = example("event.run_completed.json")
    event["parent_run_id"] = "run_01PARENT000000000000000001"
    assert not schema_errors("event.schema.json", event)
    assert EVENTS["settings"]["callable"]["strict_inputs"] is False
    walk = _events_walk(service, tmp_path)
    # strict_inputs false: the platform keeps the extra body field; nothing reads it.
    outcome, state = walk.run(
        {**EVENT_INPUTS, "parent_run_id": event["parent_run_id"]}, raw_inputs=True
    )
    assert outcome == "stop_success" and walk.trace[-1] == "handled"
    assert set(walk.artifacts) == {"result_file", "result_csv"}
    assert state["inputs"]["parent_run_id"] == event["parent_run_id"]


def test_recollect_declarations():
    actions = {
        a["action_id"]: a
        for a in yaml.safe_load((ROOT / "ui/actions/actions.yaml").read_text())["actions"]
    }
    assert actions["recollect_run"]["workflow_visible"] is True
    assert {"run_not_finished", "nothing_to_recollect"} <= set(
        actions["recollect_run"]["error_codes"]
    )
    grid = yaml.safe_load((ROOT / "ui/components/runs_table.yaml").read_text())
    row = next(a for a in grid["row_actions"] if a["action_id"] == "recollect_run")
    assert row["label"] == "Re-collect missed pages"
    assert row["visible_if"] == {"path": "can_recollect", "eq": True}
    manifest = yaml.safe_load((ROOT / "extension.yaml").read_text())
    # Like stop_run and resend_event: not a declared external effect (see DEV-LOG 0.1.18).
    assert {e["operation_id"] for e in manifest["external_effects"]} == {"start_run"}


# --------------------------------------------------------------- guide, version


def test_guide_sections():
    guide = (ROOT / "docs/setup-guide.md").read_text(encoding="utf-8")
    for words in (
        "### The CSV file",
        "The separator is `;`",
        "byte order mark",
        "a text that starts with `=`, `+`, `-` or `@` gets a leading `'`",
        "read `result.jsonl`: it keeps every value as",
        "## Collect the missed pages again",
        "**Re-collect missed pages**",
        "**Get result CSV**",
        "`result_csv`",
    ):
        assert words in guide, words


def test_csv_action_payload():
    payload = make_payload("get_result_csv", {"run_id": RUN}, service_url="http://127.0.0.1:1")
    assert payload["action"]["action_id"] == "get_result_csv"
