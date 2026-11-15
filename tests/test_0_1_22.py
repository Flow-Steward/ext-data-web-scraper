"""0.1.22: Scrape a site, Scraper events and Service maintenance ship as read-only
extension workflows (descriptor artifacts.workflows) with the connection slot
`scraper` (ADR 0068) and hooks (ADR 0038); the project-copy templates stay."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest
import yaml
from conftest import ROOT_SCOPE
from jsonschema import Draft202012Validator
from test_templates import EVENT_INPUTS, IMPL, SCRAPE_INPUTS, _action, _connector, _tabular
from workflow_sim import Walk

ROOT = Path(__file__).resolve().parents[1]
DESCRIPTOR = yaml.safe_load((ROOT / "extension-descriptor.yaml").read_text())
IDS = ("scrape_a_site", "scraper_events", "service_maintenance")
WORKFLOWS = {
    wid: yaml.safe_load((ROOT / "artifacts" / "workflows" / f"{wid}.yaml").read_text())
    for wid in IDS
}
SCRAPE, EVENTS, MAINT = (WORKFLOWS[wid] for wid in IDS)


def _hooks(workflow: dict) -> dict[str, dict]:
    return {
        s["step_id"]: s["override"]["invoke_workflow"]["hook"]
        for s in workflow["steps"]
        if "hook" in ((s.get("override") or {}).get("invoke_workflow") or {})
    }


def _edges(workflow: dict) -> list[tuple[str, str, str]]:
    return [
        (e["source"]["id"], e["outcome"]["name"], e["target"]["id"])
        for e in workflow["settings"]["execution_graph"]["edges"]
    ]


# ------------------------------------------------------------------ shipping


def test_the_three_workflows_are_published():
    rows = {r["workflow_id"]: r for r in DESCRIPTOR["artifacts"]["workflows"]}
    for wid in IDS:
        assert rows[wid]["publish"] is True
        assert rows[wid]["ref"] == f"artifacts/workflows/{wid}.yaml"
        assert WORKFLOWS[wid]["workflow_id"] == wid
    assert DESCRIPTOR["runtime"]["compatibility"]["platform_min"] == "1.8.0"


def test_they_are_generated_from_the_templates():
    path = ROOT / ".github" / "scripts" / "extension_workflows.py"
    spec = importlib.util.spec_from_file_location("extension_workflows", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--check"]) == 0


@pytest.mark.parametrize("wid", IDS)
def test_slot_export_and_bindings(wid):
    workflow = WORKFLOWS[wid]
    settings = workflow["settings"]
    assert settings["connection_slots"] == [
        {
            "slot": "scraper",
            "extension_id": "flowsteward.web-scraper",
            "connection_type_ids": ["scraper_profile"],
            "label": "Scraper profile",
        }
    ]
    assert settings["portability"] == {"allow_full_export": True}
    assert "execution_graph" in settings and "execution_graph" not in workflow
    text = json.dumps(workflow)
    assert "connection_id" not in text and "connection_ref" not in text
    assert "install_id" not in text
    for step in workflow["steps"]:
        if step["step_kind"] == "connector":
            assert step["connection_binding_jsonb"] == {"slot": "scraper"}, step["step_id"]
    # The canvas points to the hooks, not to "place for your own steps".
    notes = " ".join(n["text"] for n in settings["canvas_annotations"])
    assert "Place for your own steps" not in notes and "read-only" in notes


def test_build_mapping_is_called_as_the_extension_s_action():
    invoke = next(s for s in SCRAPE["steps"] if s["step_id"] == "build_mapping")["override"][
        "invoke_workflow"
    ]
    assert invoke["action"] == {
        "extension_id": "flowsteward.web-scraper",
        "action_id": "build_mapping",
    }
    assert "workflow_id" not in invoke
    assert invoke["connections"] == {"profile": {"slot": "scraper"}}


# --------------------------------------------------------------------- hooks


@pytest.mark.parametrize(
    ("wid", "hooks"),
    [
        ("scrape_a_site", {"review_mapping", "before_start"}),
        ("scraper_events", {"on_error"}),
        ("service_maintenance", set()),
    ],
)
def test_hook_declarations(wid, hooks):
    declared = _hooks(WORKFLOWS[wid])
    assert set(declared) == hooks
    for step_id, hook in declared.items():
        assert hook["hook_id"] == step_id and hook["contract_version"] == 1
        Draft202012Validator.check_schema(hook["input_schema"])
        props = set(hook["input_schema"]["properties"])
        assert set(hook["input_mapping"]) <= props
        assert set(hook["input_schema"]["required"]) <= set(hook["input_mapping"])
        assert set(hook["mutable_fields"]) <= props
        for spec in (hook.get("additional_outputs") or {}).values():
            Draft202012Validator(spec["schema"]).validate(spec["default"])
        assert hook["stage_contract"]["summary"]
        step = next(s for s in WORKFLOWS[wid]["steps"] if s["step_id"] == step_id)
        # A hook Invoke carries no child mappings (0.9.18 invoke_config_issues).
        assert set(step["override"]["invoke_workflow"]) == {"hook"}
    # Not in this release (owner, 2026-11-14): the result goes to a person's own workflow.
    assert not {"process_result", "on_completed"} & set(declared)


def test_hooks_sit_where_the_contract_says():
    edges = _edges(SCRAPE)
    assert ("review_mapping", "success", "retest_mapping") in edges
    assert {s for s, _o, t in edges if t == "review_mapping"} == {"stored_passed", "built_passed"}
    assert ("activate_mapping", "success", "before_start") in edges
    assert ("before_start", "success", "skip_run") in edges
    assert ("skip_run", "false", "start_crawl") in edges and (
        "skip_run",
        "true",
        "run_skipped",
    ) in edges
    steps = {s["step_id"]: s for s in SCRAPE["steps"]}
    reviewed = "state.hooks.review_mapping.output.mappings"
    assert steps["retest_mapping"]["input_mapping_jsonb"]["mappings"] == reviewed
    assert steps["activate_mapping"]["input_mapping_jsonb"]["mappings"] == reviewed
    assert (
        steps["start_crawl"]["input_mapping_jsonb"]["config"]
        == "state.hooks.before_start.output.config"
    )
    edges = _edges(EVENTS)
    assert {s for s, _o, t in edges if t == "on_error"} == {"problem_summary"}
    assert ("on_error", "success", "complete_event") in edges


# --------------------------------------------------------------------- walks


def _scrape(service, tmp_path, **kwargs):
    return Walk(
        SCRAPE,
        connector=_connector(service, tmp_path),
        invoke=_action(service, tmp_path, []),
        child=IMPL,
        **kwargs,
    )


def test_empty_hooks_change_nothing(service, tmp_path):
    walk = _scrape(service, tmp_path)
    outcome, state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_success" and walk.trace[-1] == "done", walk.failures
    assert walk.hooks_run == ["review_mapping", "before_start"]
    sent = service.calls("POST", "/v1/runs")[0]["body"]["config"]
    assert sent == state["config"]
    assert state["hooks"]["review_mapping"]["output"]["mappings"] == state["mappings"]


def test_review_mapping_changes_what_is_tested_and_activated(service, tmp_path):
    from fake_service import example

    reviewed = [example("mapping.spare_part.json")]

    def review(output):
        return {**output, "mappings": copy.deepcopy(reviewed)}

    walk = _scrape(service, tmp_path, hook_steps={"review_mapping": review})
    outcome, _state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_success", walk.failures
    tested = service.calls("POST", "/v1/mappings/test")[-1]["body"]["mappings"]
    assert [m["template_id"] for m in tested] == ["spare_part"]


def test_before_start_can_change_the_config_or_skip(service, tmp_path):
    def smaller(output):
        output["config"] = {**output["config"], "max_records": 3}
        return output

    walk = _scrape(service, tmp_path, hook_steps={"before_start": smaller})
    outcome, _state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_success"
    assert service.calls("POST", "/v1/runs")[0]["body"]["config"]["max_records"] == 3

    def skip(output):
        return {**output, "skip_run": True}

    service.requests.clear()
    walk = _scrape(service, tmp_path, hook_steps={"before_start": skip})
    outcome, _state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_success" and walk.trace[-1] == "run_skipped"
    assert not service.calls("POST", "/v1/runs")


def _events(service, tmp_path, **kwargs):
    scope = {**ROOT_SCOPE, "step_id": "claim_event"}
    return Walk(
        EVENTS, connector=_connector(service, tmp_path, scope), tabular=_tabular([]), **kwargs
    )


def test_on_error_runs_only_for_problems(service, tmp_path):
    walk = _events(service, tmp_path)
    outcome, _state = walk.run(dict(EVENT_INPUTS))
    assert outcome == "stop_success" and "on_error" not in walk.hooks_run
    seen = []

    def notify(output):
        seen.append(copy.deepcopy(output))
        return output

    event_id = "evt_01PROBLEM00000000000000001"
    service.event_bodies[event_id] = {
        "event_type": "run_failed",
        "outcome": None,
        "stop_reason": None,
    }
    walk = _events(service, tmp_path, hook_steps={"on_error": notify})
    outcome, _state = walk.run({**EVENT_INPUTS, "event_id": event_id, "event_type": "run_failed"})
    assert outcome == "stop_success" and walk.trace[-2:] == ["complete_event", "handled"]
    assert walk.hooks_run == ["on_error"]
    assert seen[0]["event_type"] == "run_failed" and seen[0]["run_id"] == EVENT_INPUTS["run_id"]


def test_service_maintenance_runs_with_the_slot(service, tmp_path):
    walk = Walk(MAINT, connector=_connector(service, tmp_path))
    outcome, _state = walk.run({"dry_run": True})
    assert outcome == "stop_success"


# ------------------------------------------------------------- copies, guide


def test_the_project_copy_templates_stay():
    for name in ("scrape_site", "scraper_events", "service_maintenance"):
        workflow = json.loads((ROOT / "templates" / f"{name}.bundle.json").read_text())[
            "workflows"
        ][0]
        assert "connection_slots" not in workflow["settings"]
        assert not _hooks({"steps": workflow["steps"]})


@pytest.mark.parametrize(
    "words",
    [
        "## Read-only workflows and hooks",
        "**Review the mapping**",
        "**Before the crawl starts**",
        "**On a problem or a failed run**",
        "Your copies keep working",
        "Connections card",
        "New webhook",
    ],
)
def test_guide(words):
    assert words in (ROOT / "docs/setup-guide.md").read_text(encoding="utf-8")
