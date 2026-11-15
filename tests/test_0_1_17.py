"""0.1.17: service storage and clean-up (actions, Service page, Service maintenance template);
A40: the Scraper events job shows the result file and its first rows, Run detail the descriptor."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from conftest import ROOT_SCOPE
from fake_service import example, schema_errors
from test_templates import EVENT_INPUTS, EVENTS, MAINT, SCRAPE, _connector
from workflow_sim import Walk

ROOT = Path(__file__).resolve().parents[1]
RUN = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"
ACTIONS = {
    a["action_id"]: a
    for a in yaml.safe_load((ROOT / "ui/actions/actions.yaml").read_text())["actions"]
}
OPERATIONS = {
    op["operation_id"]: op
    for op in yaml.safe_load((ROOT / "contracts/operation_manifest.yaml").read_text())["operations"]
}
#: extension-action-result-summary.ts (0.9.16): keys the summary leaves out, and its 12 values.
RUNTIME_FIELD = re.compile(
    r"(?:^|_)(?:command|argv|args|cwd|path|paths|bundle|stdout|stderr|trace|traceback|duration"
    r"|elapsed|install|account|correlation|request)(?:_|$)",
    re.IGNORECASE,
)
SENSITIVE_FIELD = re.compile(
    r"(?:authorization|cookie|credential|password|private[_-]?key|secret|token)", re.IGNORECASE
)
CLEANUP_ALL = {
    "working_files_older_than_days": 7,
    "result_files_older_than_days": 14,
    "run_records_older_than_days": 30,
    "samples_older_than_hours": 24,
    "compact_database": True,
}


def _summary(result: dict) -> list[tuple[str, object]]:
    """What the page shows: the first 12 plain values, keys in the order the host sends (sorted)."""
    rows = [
        (key, value)
        for key, value in sorted(result.items())
        if not RUNTIME_FIELD.search(key) and not isinstance(value, dict)
    ]
    return rows


def _posts(service):
    return service.calls("POST", "/v1/admin/cleanup")


# ------------------------------------------------------------------- storage


def test_storage_in_plain_words(invoke, service):
    result = invoke("service_storage")["result"]
    shown = _summary(result)
    assert len(shown) <= 12, [k for k, _ in shown]
    assert not any(SENSITIVE_FIELD.search(key) for key, _ in shown)
    assert all(isinstance(value, str) and value for _key, value in shown)
    assert result["message"] == (
        "The service keeps 20.2 MB; its disk has 38.4 GB free of 58.4 GB. The biggest part is "
        "samples (11.0 MB)."
    )
    assert result["disk"].startswith("38.4 GB free of 58.4 GB (34 % used in all; 20.2 MB")
    assert result["working_files"].startswith("1.0 MB in 4 runs")
    assert "1 run not finished: never touched" in result["working_files"]
    assert "Oldest 2026-11-12 08:31 UTC." in result["working_files"]
    assert "1 not fetched by Flow Steward yet (40.0 KB)" in result["result_files"]
    assert result["samples"].startswith("11.0 MB in 38 samples")
    assert result["screenshots"].startswith("409.6 KB in 3 screenshots")
    assert "write-ahead log 3.9 MB" in result["database"]
    assert "about 1.1 MB is free inside the file" in result["database"]
    assert (
        result["runs"]
        == "12 runs: 1 running, 9 completed, 2 failed. Oldest finished 2026-11-12 09:02 UTC."
    )
    assert result["retention"] == (
        "The service cleans up by itself every 60 seconds: working files 7 days, result files "
        "14 days and run records 30 days after a run ends; samples and screenshots after 24 hours."
    )
    assert result["last_automatic_cleanup"] == (
        "2026-11-14 09:59 UTC, nothing removed. Last time it removed something: "
        "2026-11-14 08:12 UTC, 1.3 MB freed (4 samples, 1 screenshot)."
    )
    assert result["last_manual_cleanup"] == "Never."
    assert result["never_removed"].startswith("Browser identities 2.0 KB")
    assert result["details"] == example("get_admin_storage.response.json")
    assert service.calls("GET", "/v1/admin/storage")[0]["headers"]["authorization"]


def test_storage_says_when_the_disk_is_low(invoke, service):
    answer = example("get_admin_storage.response.json")
    answer["volume"]["free_bytes"] = 1024**3
    service.override("GET", r"^/v1/admin/storage$", 200, answer)
    message = invoke("service_storage")["result"]["message"]
    assert message.startswith("Low on disk space. ") and message.endswith("Clean up now.")


def test_storage_names_the_last_manual_cleanup(invoke, service):
    answer = example("get_admin_storage.response.json")
    applied = example("post_admin_cleanup.response.json")
    answer["last_cleanup"]["manual"] = {
        "at": applied["time"],
        "trigger": "manual",
        "removed": applied["removed"],
        "bytes_freed": applied["bytes_freed"],
    }
    assert not schema_errors("storage.schema.json", answer)
    service.override("GET", r"^/v1/admin/storage$", 200, answer)
    line = invoke("service_storage")["result"]["last_manual_cleanup"]
    assert line == (
        "2026-11-14 10:02 UTC, 16.2 MB freed (working files of 3 runs, 4 result files, "
        "38 samples, 3 screenshots)."
    )


@pytest.mark.parametrize("action", ["service_storage", "clean_up_storage"])
def test_an_older_service_is_told_to_update(invoke, service, action):
    method = "GET" if action == "service_storage" else "POST"
    path = "storage" if action == "service_storage" else "cleanup"
    service.override(
        method,
        rf"^/v1/admin/{path}$",
        404,
        {"error": {"code": "not_found", "message": "No route", "request_id": "req_1"}},
    )
    response = invoke(action, CLEANUP_ALL if action != "service_storage" else None)
    assert response["error_code"] == "service_route_missing"
    assert "older than the storage routes" in response["error"]
    assert response["definitely_no_external_effect"] is True


def test_unauthorized_goes_the_existing_path(invoke, service):
    service.override(
        "GET",
        r"^/v1/admin/storage$",
        401,
        {"error": {"code": "unauthorized", "message": "no", "request_id": "r"}},
    )
    assert invoke("service_storage")["error_code"] == "service_unauthorized"


# ------------------------------------------------------------------- clean-up


def test_cleanup_is_a_dry_run_by_default(invoke, service):
    response = invoke("clean_up_storage", CLEANUP_ALL)
    assert response["ok"], response
    body = _posts(service)[0]["body"]
    assert body == {
        "dry_run": True,
        "run_working_files": {"older_than_days": 7},
        "result_files": {"older_than_days": 14, "include_unfetched": False},
        "run_records": {"older_than_days": 30, "include_unfetched": False},
        "samples": {"older_than_hours": 24},
        "protection_screenshots": {"older_than_hours": 24},
        "compact_database": True,
    }
    assert not service.schema_violations
    result = response["result"]
    assert result["dry_run"] is True
    assert result["message"].startswith(
        "Dry run: nothing was removed. This clean-up would free 12.3 MB, and compacting would "
        "give back up to 5.0 MB more."
    )
    assert (
        result["working_files"]
        == "3 runs, 768.0 KB would be removed (finished at least 7 days ago)."
    )
    assert (
        result["samples"]
        == "38 samples, 11.0 MB would be removed (last written at least 24 hours ago)."
    )
    assert result["run_records"] == "Nothing to remove."
    assert result["database"].startswith(
        "The write-ahead log (3.9 MB) would be written back and emptied"
    )
    assert "compacting the file is skipped: 1 run(s) not finished" in result["database"]
    assert result["kept"].startswith(
        "Kept: 1 run not finished (nothing of it is touched); 1 finished run"
    )
    assert result["freed"] == "12.3 MB would be freed (plus what compacting gives back)."
    assert result["bytes_freed"] == 12891750
    assert result["details"]["request"] == body
    assert len(_summary(result)) <= 12


def test_cleanup_applied_from_a_workflow(invoke, service):
    result = invoke("clean_up_storage", {**CLEANUP_ALL, "dry_run": False})["result"]
    assert _posts(service)[0]["body"]["dry_run"] is False
    assert result["dry_run"] is False
    assert result["message"] == (
        "Clean-up done at 2026-11-14 10:02 UTC: 16.2 MB freed. The database file was not "
        "compacted; see 'database'."
    )
    assert result["working_files"] == "3 runs, 768.0 KB removed (finished at least 7 days ago)."
    assert result["database"].endswith("Database 7.6 MB → 3.6 MB.")
    assert result["freed"] == "16.2 MB freed."


def test_parts_left_empty_are_not_sent(invoke, service):
    result = invoke("clean_up_storage", {"samples_older_than_hours": "0"})["result"]
    body = _posts(service)[0]["body"]
    assert body == {
        "dry_run": True,
        "samples": {"older_than_hours": 0},
        "protection_screenshots": {"older_than_hours": 0},
    }
    assert result["working_files"] == "Not part of this clean-up."
    assert result["database"] == "Not compacted ('Compact the database' is off)."
    assert result["freed"] == "11.4 MB would be freed."


def test_unfetched_results_only_when_asked(invoke, service):
    invoke(
        "clean_up_storage",
        {"result_files_older_than_days": 0, "include_unfetched_results": True},
    )
    body = _posts(service)[0]["body"]
    assert body["result_files"] == {"older_than_days": 0, "include_unfetched": True}


@pytest.mark.parametrize(
    ("inputs", "words"),
    [
        ({}, "Nothing to clean up was chosen"),
        ({"compact_database": False}, "Nothing to clean up was chosen"),
        ({"working_files_older_than_days": -1}, "between 0 and 3650"),
        ({"samples_older_than_hours": 87601}, "between 0 and 87600"),
        ({"run_records_older_than_days": "ten"}, "must be a whole number"),
        ({"run_records_older_than_days": 1.5}, "must be a whole number"),
        ({"compact_database": "maybe"}, "must be true or false"),
    ],
)
def test_bad_cleanup_inputs_send_nothing(invoke, service, inputs, words):
    response = invoke("clean_up_storage", inputs)
    assert response["error_code"] == "invalid_payload" and words in response["error"]
    assert response["definitely_no_external_effect"] is True
    assert not _posts(service)


def test_service_refusal_goes_the_existing_path(invoke, service):
    service.override(
        "POST",
        r"^/v1/admin/cleanup$",
        422,
        {
            "error": {
                "code": "validation_failed",
                "message": "bad",
                "request_id": "req_2",
                "fields": [
                    {
                        "field": "samples.older_than_hours",
                        "code": "invalid_value",
                        "message": "too big",
                    }
                ],
            }
        },
    )
    response = invoke("clean_up_storage", CLEANUP_ALL)
    assert response["error_code"] == "config_invalid"
    assert response["error"].startswith("The scraper service refused the clean-up settings.")
    assert "samples.older_than_hours has a value the service does not accept" in response["error"]


def test_clean_up_now_needs_the_tick(invoke, service):
    response = invoke("clean_up_storage_now", CLEANUP_ALL)
    assert response["error_code"] == "invalid_payload" and "dry run" in response["error"]
    assert not _posts(service)
    result = invoke("clean_up_storage_now", {**CLEANUP_ALL, "confirm_removal": True})["result"]
    assert _posts(service)[0]["body"]["dry_run"] is False and result["dry_run"] is False


# ------------------------------------------------------------- manifests, page


def test_actions_and_operations():
    for action_id in ("service_storage", "clean_up_storage"):
        assert ACTIONS[action_id]["workflow_visible"] is True
    assert OPERATIONS["service_storage"]["operation_kind"] == "source"
    assert OPERATIONS["clean_up_storage"]["operation_kind"] == "action"
    now = ACTIONS["clean_up_storage_now"]
    assert now["workflow_visible"] is False
    assert now["result_fields"] == ACTIONS["clean_up_storage"]["result_fields"]
    dry = next(i for i in OPERATIONS["clean_up_storage"]["inputs"] if i["name"] == "dry_run")
    assert dry["required"] is False and "default" not in dry and "Empty: on" in dry["description"]
    for action in ("service_storage", "clean_up_storage"):
        plain = [
            row["name"]
            for row in OPERATIONS[action]["outputs"]
            if row["value_type"] != "object" and not RUNTIME_FIELD.search(row["name"])
        ]
        assert len(plain) <= 12, action


# ------------------------------------------------------- Service maintenance


def test_service_maintenance_cleans_up_with_the_service_s_own_ages(service, tmp_path):
    walk = Walk(MAINT, connector=_connector(service, tmp_path))
    outcome, state = walk.run({})
    assert outcome == "stop_success", walk.failures
    assert walk.trace == ["storage_before", "clean_up", "storage_after", "done"]
    body = _posts(service)[0]["body"]
    assert body == {
        "dry_run": False,
        "run_working_files": {"older_than_days": 7},
        "result_files": {"older_than_days": 14, "include_unfetched": False},
        "run_records": {"older_than_days": 30, "include_unfetched": False},
        "samples": {"older_than_hours": 24},
        "protection_screenshots": {"older_than_hours": 24},
        "compact_database": True,
    }
    assert set(walk.outputs) == {
        "storage_before",
        "cleanup",
        "freed",
        "bytes_freed",
        "storage_after",
    }
    assert walk.outputs["freed"] == "16.2 MB freed."
    assert walk.outputs["bytes_freed"] == 17032942
    assert not service.schema_violations


def test_service_maintenance_dry_run_input(service, tmp_path):
    walk = Walk(MAINT, connector=_connector(service, tmp_path))
    outcome, _state = walk.run({"dry_run": True})
    assert outcome == "stop_success"
    assert _posts(service)[0]["body"]["dry_run"] is True
    assert walk.outputs["cleanup"].startswith("Dry run: nothing was removed.")


def test_service_maintenance_is_schedulable_and_explained():
    callable_ = MAINT["settings"]["callable"]
    assert "schedule" in callable_["trigger_channels"]
    assert [row["name"] for row in callable_["expected_inputs"]] == ["dry_run"]
    assert all("default" not in row for row in callable_["expected_inputs"])
    notes = " ".join(n["text"] for n in MAINT["settings"]["canvas_annotations"])
    assert "Schedules" in notes and "Weekly" in notes


# ----------------------------------------------------------------------- A40


def _events_walk(service, tmp_path):
    reads: list = []

    def tabular(step, inputs):
        reads.append(inputs["artifact_handle"])
        return {
            "dataset_handle": "dataset:x",
            "row_count": 3,
            "preview_rows": [{"title": "A"}, {"title": "B"}],
        }

    scope = {**ROOT_SCOPE, "step_id": "claim_event"}
    return Walk(EVENTS, connector=_connector(service, tmp_path, scope), tabular=tabular)


def test_scraper_events_returns_the_file_and_the_first_rows(service, tmp_path):
    walk = _events_walk(service, tmp_path)
    outcome, state = walk.run(dict(EVENT_INPUTS))
    assert outcome == "stop_success" and walk.trace[-1] == "handled"
    assert walk.artifacts == {"result_file": "artifact:res_out", "result_csv": "artifact:res_csv"}
    assert walk.outputs["result_preview"] == [{"title": "A"}, {"title": "B"}]
    assert walk.outputs["result"]["file_name"] == f"{RUN}.jsonl"
    assert walk.outputs["result"]["record_count"] == state["result"]["record_count"]
    assert walk.outputs["run_summary"].startswith("Run is completed")


def test_scraper_events_declarations_match_the_platform_rules():
    """workflow_returned_files.derive_returned_files (0.9.16 = 0.9.17): a connector step's
    `override.returned_files` entry names one of its mapped outputs; the legacy
    `expected_artifacts` row of the same name is then satisfied, not a second file."""
    callable_ = EVENTS["settings"]["callable"]
    steps = {s["step_id"]: s for s in EVENTS["steps"]}
    get_result = steps["get_result"]
    assert get_result["override"]["returned_files"] == [
        {"result": "artifact_handle", "key": "result_file"}
    ]
    assert "artifact_handle" in get_result["output_mapping_jsonb"]
    assert [row["name"] for row in callable_["expected_artifacts"]] == ["result_file", "result_csv"]
    names = [row["name"] for row in callable_["expected_outputs"]]
    assert names == ["run_summary", "result", "result_preview"]
    assert not {"sys", "inputs", "outputs", "workflow_data", "files", "mode"} & set(names)
    assert all(row["required"] is False for row in callable_["expected_outputs"])
    assert callable_["output_mode"] == "variables_and_artifacts"
    # Each output is a top-level state key one step writes (collect_runtime_outputs).
    written = {
        target.removeprefix("state.").split(".")[0]
        for step in EVENTS["steps"]
        for target in (step.get("output_mapping_jsonb") or {}).values()
    }
    assert set(names) <= written
    assert steps["read_result"]["output_mapping_jsonb"] == {"preview_rows": "state.result_preview"}


def test_a_run_without_records_returns_no_file(service, tmp_path):
    run = example("get_run.response.json")
    run["result"] = None
    run["counters"]["records_extracted"] = 0
    run["stop_reason"] = "stopped"
    service.run_patch = run  # every read: Claim event reads the run too (0.1.23)
    walk = _events_walk(service, tmp_path)
    outcome, _state = walk.run(dict(EVENT_INPUTS))
    assert outcome == "stop_success" and "get_result" not in walk.trace
    assert walk.artifacts == {} and "result_preview" not in walk.outputs
    assert "no records" in walk.outputs["run_summary"]


@pytest.mark.parametrize("workflow", [EVENTS, MAINT], ids=["events", "maint"])
def test_new_stop_messages_fit_what_the_platform_shows(workflow):
    for step in workflow["steps"]:
        for kind in ("stop_fail", "stop_success"):
            message = (step.get("override") or {}).get(kind, {}).get("message")
            if message:
                assert len(message) <= 512 and "{{" not in message, step["step_id"]


def test_scrape_a_site_says_where_the_results_appear():
    # 0.1.23: Scrape a site waits for its run and holds the files itself.
    message = next(s for s in SCRAPE["steps"] if s["step_id"] == "done")["override"][
        "stop_success"
    ]["message"]
    for words in ("Result panel", "result_file", "result_csv", "run_summary", "Invoke"):
        assert words in message


def test_run_detail_shows_the_result_descriptor(invoke, service):
    result = invoke("show_run", {"run_id": RUN})["result"]
    line = result["result"]
    assert line.startswith(
        f"{RUN}.jsonl (2.7 MB): 1318 records, kept by the service until 2026-11-26."
    )
    assert "'Scraper events' job /jobs/job_e2a8d4410b, panel Result (result_file and" in line
    assert len(_summary(result)) <= 12


@pytest.mark.parametrize(
    ("state", "words"),
    [
        ("running", "not yet: when the run ends"),
        ("failed", "none: the run failed"),
        ("completed", "none: The run produced no records"),
    ],
)
def test_run_detail_result_line_without_a_file(invoke, service, state, words):
    run = example("get_run.response.json")
    run["state"] = state
    run["result"] = None
    if state == "completed":
        run["counters"]["records_extracted"] = 0
    else:
        run["outcome"] = None
        run["stop_reason"] = None
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    assert invoke("show_run", {"run_id": RUN})["result"]["result"].startswith(words)


# ------------------------------------------------------------- guide, version


def test_guide_sections():
    guide = (ROOT / "docs/setup-guide.md").read_text(encoding="utf-8")
    for words in (
        "## Where are my results",
        "## Disk use and clean-up",
        "--log-driver json-file --log-opt max-size=20m --log-opt max-file=5",
        "**Clean up - dry run**",
        "**Clean up now**",
        "**The Service page**",
        "service_maintenance.bundle.yaml",
        "Repeat **Weekly**",
        "`result_preview`",
        "`result_file`",
    ):
        assert words in guide, words


def test_bundles_carry_the_release_version():
    manifest = yaml.safe_load((ROOT / "extension.yaml").read_text())
    descriptor = yaml.safe_load((ROOT / "extension-descriptor.yaml").read_text())
    assert manifest["version"] == descriptor["release_version"]
    for path in sorted((ROOT / "templates").glob("*.bundle.json")):
        bundle = json.loads(path.read_text())
        assert bundle["metadata"]["source_version"] == manifest["version"], path.name
        assert bundle["workflows"][0]["version"] == manifest["version"], path.name


def test_cleanup_answer_examples_are_contract_valid():
    for name in ("post_admin_cleanup.response.json", "post_admin_cleanup.response.dry_run.json"):
        assert not schema_errors("cleanup.schema.json", example(name)), name
    assert not schema_errors("storage.schema.json", example("get_admin_storage.response.json"))
