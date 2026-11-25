"""The two importable template bundles: shape, contents, data flow."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

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
from workflow_sim import RESERVED_STATE_ROOTS, StepFailed, Walk, resolve, split_default

ROOT = Path(__file__).resolve().parents[1]
BUNDLES = {
    name: json.loads((ROOT / "templates" / name).read_text())
    for name in (
        "scrape_site.bundle.json",
        "scraper_events.bundle.json",
        "service_maintenance.bundle.json",
    )
}
SCRAPE = BUNDLES["scrape_site.bundle.json"]["workflows"][0]
EVENTS = BUNDLES["scraper_events.bundle.json"]["workflows"][0]
MAINT = BUNDLES["service_maintenance.bundle.json"]["workflows"][0]
OPERATIONS = {
    op["operation_id"]: op
    for op in yaml.safe_load((ROOT / "contracts/operation_manifest.yaml").read_text())["operations"]
}
IMPL = yaml.safe_load((ROOT / "artifacts/workflows/impl_build_mapping.yaml").read_text())
SITE_SETTINGS = [
    "sitemap_url",
    "max_sitemap_entries",
    "sitemap_source_include",
    "sitemap_source_exclude",
    "url_exclude",
    "target_page_selector",
    "max_pages",
    "single_page",
    "fetch_mode",
    "browser",
    "use_proxy_tiers",
    "challenge_click",
    "use_solver",
    "delay_min_seconds",
    "delay_max_seconds",
    "max_concurrency",
    "max_requests_per_minute",
    "extraction_mode",
    "structured_data_types",
    "wait_after_load_ms",
    "wait_for_selector_ms",
    "headers",
    "respect_robots",
    "rebuild_mapping",
    "debug",
]  # task document 5.3; `proxy_tiers` is the contract's `use_proxy_tiers`


@pytest.mark.parametrize("name", sorted(BUNDLES))
def test_bundle_top_level(name):
    bundle = BUNDLES[name]
    assert list(bundle) == [
        "schema_version",
        "kind",
        "metadata",
        "workflows",
        "dependencies",
        "import_hints",
    ]
    assert (
        bundle["schema_version"] == "platform.workflow_bundle.v2"
        and bundle["kind"] == "workflow_bundle"
    )
    assert bundle["import_hints"]["default_enabled"] is False
    workflow = bundle["workflows"][0]
    assert workflow["enabled"] is False
    assert {"steps", "groups", "for_each", "execution_graph"} <= set(workflow)
    deps = bundle["dependencies"]
    assert [d["extension_id"] for d in deps["extensions"]] == ["flowsteward.web-scraper"]
    assert [c["connection_ref"] for c in deps["project_connections"]] == ["scraper_profile"]
    assert deps["agents"] == [] and deps["child_workflows"] == []


@pytest.mark.parametrize("name", sorted(BUNDLES))
def test_bundle_carries_no_secret_and_no_connection_or_install_id(name):
    text = json.dumps(BUNDLES[name])
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text)
    assert "pi_" not in text
    assert not re.search(r"(secret|token|password|api_key)\"\s*:\s*\"[^\"]+", text, re.I)
    values = set()

    def walk(node, key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif key in {"connection_id", "connection_ref", "install_id", "project_connection_id"}:
            values.add(node)

    walk(BUNDLES[name])
    assert values <= {"scraper_profile", "flowsteward.web-scraper"}


@pytest.mark.parametrize("workflow", [SCRAPE, EVENTS, MAINT], ids=["scrape", "events", "maint"])
def test_graph_is_complete_and_terminates(workflow):
    steps = {s["step_id"]: s for s in workflow["steps"]}
    edges = workflow["execution_graph"]["edges"]
    outcomes: dict[str, set] = {}
    for edge in edges:
        assert edge["source"]["id"] in steps and edge["target"]["id"] in steps
        outcomes.setdefault(edge["source"]["id"], set()).add(
            (edge["outcome"]["name"], edge["outcome"].get("case_id", ""))
        )
        assert edge["rework"] is False
    for step_id, step in steps.items():
        kind = step["step_kind"]
        got = outcomes.get(step_id, set())
        if kind == "if":
            assert got == {("true", ""), ("false", "")}, step_id
        elif kind == "switch":
            cases = {("case", c["case_id"]) for c in step["override"]["switch"]["cases"]}
            assert got == cases | {("default", "")}, step_id
        elif kind in {"stop_success", "stop_fail"}:
            assert not got, step_id
        elif (step.get("error_policy") or {}).get("on_error") == "route_to_step":
            # workflow_definition_validation.check_required_ports: success and error.
            assert step["error_policy"] == {"on_error": "route_to_step"}, step_id
            assert got == {("success", ""), ("error", "")}, step_id
        else:
            assert got == {("success", "")}, step_id
    reachable, todo = set(), [workflow["execution_graph"]["entry"]["id"]]
    while todo:
        node = todo.pop()
        if node not in reachable:
            reachable.add(node)
            todo.extend(e["target"]["id"] for e in edges if e["source"]["id"] == node)
    assert reachable == set(steps)


@pytest.mark.parametrize("workflow", [SCRAPE, EVENTS, MAINT], ids=["scrape", "events", "maint"])
def test_every_step_matches_its_operation(workflow):
    for step in workflow["steps"]:
        if step["step_kind"] == "connector":
            assert step["connector_id"] == "extension.flowsteward.web-scraper"
            assert step["connection_binding_jsonb"] == {"connection_id": "scraper_profile"}
            assert "connector_install_id" not in step
            operation = OPERATIONS[step["override"]["connector"]["operation_id"]]
            names = {row["name"] for row in operation["inputs"]}
            required = {
                row["name"]
                for row in operation["inputs"]
                if row["required"] and row["value_type"] != "connection_ref"
            }
            assert set(step["input_mapping_jsonb"]) <= names, step["step_id"]
            assert required <= set(step["input_mapping_jsonb"]), step["step_id"]
            assert set(step["output_mapping_jsonb"]) <= {
                row["name"] for row in operation["outputs"]
            }, step["step_id"]
        if step["step_kind"] == "invoke_workflow":
            spec = step["override"]["invoke_workflow"]
            assert spec["action"] == {
                "extension_id": "flowsteward.web-scraper",
                "install_id": "flowsteward.web-scraper",
                "action_id": "build_mapping",
            }
            # The importer rewrites this prefix to the mapped install
            # (workflow_portability_rewrite._rewrite_action_invoke), and save-time
            # validation wants projection_bridge_id(install, local id).
            assert spec["workflow_id"] == "extension.flowsteward.web-scraper.impl_build_mapping"
            assert spec["connections"] == {"profile": {"connection_id": "scraper_profile"}}
            operation = OPERATIONS["build_mapping"]
            assert set(spec["input_mapping"]) <= {row["name"] for row in operation["inputs"]}
            assert {"site", "sample_ids", "samples"} <= set(spec["input_mapping"])
            assert set(spec["output_mapping"]) <= {row["name"] for row in operation["outputs"]}


RUN_FORM = [
    # the common fields first (owner, 0.1.9) ...
    "site_url",
    "extraction_instructions",
    "run_name",  # 0.1.23: only the run's title (settings.run_title)
    "max_records",
    "target_page_selector",
    "url_include",
    "url_exclude",
    "discovery_mode",
    "debug",
    # ... then the rest: crawl scope and AI-chosen fields first (ROUND2 §1, §5)
    "crawl_include",
    "stay_in_section",
    "ai_choose_fields",
    "fields_hint",
    "fields",
    "required_fields",
    "urls",
    "urls_file",
    "sitemap_url",
    "max_sitemap_entries",
    "sitemap_source_include",
    "sitemap_source_exclude",
    "max_pages",
    "single_page",
    "fetch_mode",
    "browser",
    "browser_os",
    "canvas_noise",
    "use_proxy_tiers",
    "challenge_click",
    "use_solver",
    "politeness",  # 0.1.24 (service rc-next12, A64): the preset the four below override
    "delay_min_seconds",
    "delay_max_seconds",
    "max_concurrency",
    "max_requests_per_minute",
    "page_timeout_seconds",  # 0.1.24 (A63)
    "extraction_mode",
    "structured_data_types",
    "wait_after_load_ms",
    "wait_for_selector_ms",
    "headers",
    "respect_robots",
    "rebuild_mapping",
    # 0.1.20 (service README 4.11): links the crawl skips
    "link_rules_off",
    "trap_pattern_budget",
    # 0.1.24 (A65, A66): skip or queue a start while the site is busy
    "when_site_busy",
]


def test_run_form_of_scrape_a_site():
    fields = SCRAPE["settings"]["callable"]["expected_inputs"]
    assert [f["name"] for f in fields] == RUN_FORM
    assert [f["name"] for f in fields if f["required"]] == ["site_url", "extraction_instructions"]
    for field in fields:
        # Core 0.9.16 treats an interface default as a fixed value and refuses a
        # caller's value (workflow.input.override_forbidden): empty means the default.
        assert "default" not in field, field["name"]
        assert field["type"] in {
            "string",
            "paragraph",
            "number",
            "boolean",
            "select",
            "object",
            "array",
            "file",
        }
        assert field["description"] and field["label"][0].isupper()
        if not field["required"] and field["name"] not in {"urls", "urls_file"}:
            assert "Empty:" in field["description"], field["name"]
        if field["type"] == "select":
            assert all(isinstance(option, str) for option in field["options"])
    assert "schedule" in SCRAPE["settings"]["callable"]["trigger_channels"]


def test_every_setting_is_a_run_input_and_site_settings_is_gone():
    """0.1.9: nothing is edited inside the workflow; every setting is on the run form."""
    from handlers.settings import RUN_INPUT_KEYS, SITE_SETTING_KEYS

    assert list(SITE_SETTING_KEYS) == SITE_SETTINGS  # still read from old project copies
    assert set(SITE_SETTINGS) <= set(RUN_FORM)  # browser_os, canvas_noise: run form only
    assert [name for name in RUN_FORM if name not in {"urls", "urls_file", "run_name"}] == list(
        RUN_INPUT_KEYS
    )
    assert all(step["step_id"] != "site_settings" for step in SCRAPE["steps"])
    assert "site_settings" not in json.dumps(SCRAPE["execution_graph"])
    assert SCRAPE["execution_graph"]["entry"]["id"] == "validate_settings"
    validate = next(s for s in SCRAPE["steps"] if s["step_id"] == "validate_settings")
    assert "site_settings" not in validate["input_mapping_jsonb"]
    # run_name only titles the job: it is not a setting of the run.
    assert list(validate["input_mapping_jsonb"]) == [n for n in RUN_FORM if n != "run_name"]
    for name, selector in validate["input_mapping_jsonb"].items():
        expected = f"state.inputs.{name}"
        if name not in {"site_url", "extraction_instructions"}:
            expected += "|default:null"
        assert selector == expected
    texts = " ".join(n["text"] for n in SCRAPE["settings"]["canvas_annotations"])
    assert "Site settings' step" not in texts.replace("still has a 'Site settings' step", "")
    assert "Every setting is on the run form" in texts


def test_events_inputs_and_webhook_channel():
    callable_settings = EVENTS["settings"]["callable"]
    assert [f["name"] for f in callable_settings["expected_inputs"]] == [
        "event_id",
        "event_type",
        "run_id",
        "resource_id",
    ]
    assert all(
        f["required"] and f["type"] == "string" and "default" not in f
        for f in callable_settings["expected_inputs"]
    )
    assert "webhook" in callable_settings["trigger_channels"]
    tabular = next(s for s in EVENTS["steps"] if s["step_kind"] == "tabular_feed")
    assert tabular["override"]["tabular_feed"]["operation"] == "read"
    assert tabular["override"]["tabular_feed"]["input"] == {
        "artifact_handle": "state.result.artifact_handle"
    }


@pytest.mark.parametrize("workflow", [SCRAPE, EVENTS], ids=["scrape", "events"])
def test_canvas_notes_follow_the_platform_limits(workflow):
    notes = workflow["settings"]["canvas_annotations"]
    ids = [n["id"] for n in notes]
    assert len(ids) == len(set(ids)) and len(notes) <= 500
    for n in notes:
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,100}", n["id"])
        assert (
            len(n["text"]) <= 20000
            and "Place for your own steps" in n["text"]
            or "Keep" in n["text"]
            or "Trigger" in n["text"]
            or "run form" in n["text"]
        )
        assert 160 <= n["width"] <= 4000 and 100 <= n["height"] <= 4000
        assert -1_000_000 <= n["x"] <= 1_000_000 and -1_000_000 <= n["y"] <= 1_000_000
    texts = " ".join(n["text"] for n in notes)
    if workflow is SCRAPE:
        assert "before the start" in texts and "review the mapping" in texts
    else:
        assert "process the result" in texts and "after completion" in texts and "on error" in texts


# --------------------------------------------------------------------- walks


def _connector(service, tmp_path, scope=None):
    from handlers.router import handle_request

    def run(step, inputs):
        operation = step["override"]["connector"]["operation_id"]
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
        if not response["ok"]:
            raise StepFailed(f"{step['step_id']}: {response.get('error_code')}")
        declared = {row["name"] for row in OPERATIONS[operation]["outputs"]}
        assert declared <= set(response["result"]), (operation, declared - set(response["result"]))
        return response["result"]

    return run


def _action(service, tmp_path, agent_mappings):
    """Run the composite action by walking impl_build_mapping with a scripted agent."""

    def run(step, inputs):
        walk = Walk(
            IMPL,
            connector=_connector(service, tmp_path),
            agent=lambda _step, _inputs: {
                "mappings_json": json.dumps(agent_mappings),
                "notes": "scripted",
            },
        )
        outcome, state = walk.run(inputs, raw_inputs=True)
        if outcome == "failed":
            raise StepFailed("the build_mapping child failed: " + "; ".join(walk.failures))
        return {
            name: state.get(name)
            for name in ("mappings", "passed", "field_results", "failures", "attempts", "summary")
        }

    return run


SCRAPE_INPUTS = {
    "site_url": "https://www.lumen-shop.example/",
    "extraction_instructions": "Lamps and spare parts: name, price, article number.",
    "fields": ["title", "description", "price", "currency", "part_number", "image_urls"],
    "required_fields": ["title", "price", "currency"],
    "discovery_mode": "auto",
    "max_records": 10,
}


def test_scrape_a_site_with_a_stored_mapping(service, tmp_path):
    walk = Walk(
        SCRAPE,
        connector=_connector(service, tmp_path),
        invoke=_action(service, tmp_path, []),
        child=IMPL,
    )
    outcome, state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_success", walk.trace
    assert "build_mapping" not in walk.trace and "test_stored_mapping" in walk.trace
    assert state["scraper_run_id"].startswith("run_")
    assert "run_id" not in state  # reserved root (state_path_guards.py:37)
    assert state["config"]["discovery_mode"] == "sitemap_and_crawl"  # the analysis chose
    assert not walk.reads_missing, walk.reads_missing
    assert not service.schema_violations


def test_scrape_a_site_builds_a_mapping_for_a_new_site(service, tmp_path):
    service.mappings.clear()
    mappings = [example("mapping.product.json"), example("mapping.spare_part.json")]
    walk = Walk(
        SCRAPE,
        connector=_connector(service, tmp_path),
        invoke=_action(service, tmp_path, mappings),
        child=IMPL,
    )
    outcome, state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_success", walk.trace
    assert "build_mapping" in walk.trace and "retest_mapping" in walk.trace
    assert {(site, t) for (site, t) in service.mappings} == {
        ("lumen-shop.example", "product"),
        ("lumen-shop.example", "spare_part"),
    }
    assert not service.schema_violations


def test_scrape_a_site_stops_when_no_mapping_passes(service, tmp_path):
    service.mappings.clear()
    failing = example("post_mappings_test.response.json")
    failing["passed"] = False
    for _ in range(5):
        service.override("POST", r"^/v1/mappings/test$", 200, failing)
    walk = Walk(
        SCRAPE,
        connector=_connector(service, tmp_path),
        invoke=_action(service, tmp_path, [example("mapping.product.json")]),
        child=IMPL,
    )
    outcome, state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_fail" and walk.trace[-1] == "no_mapping"
    assert state["mapping_attempts"] == 3
    assert not service.calls("POST", "/v1/runs")


def test_scrape_a_site_url_list_skips_the_strategy(service, tmp_path):
    inputs = {
        **SCRAPE_INPUTS,
        "discovery_mode": "url_list",
        "urls": ["https://www.lumen-shop.example/ersatzteile/a"],
    }
    walk = Walk(
        SCRAPE,
        connector=_connector(service, tmp_path),
        invoke=_action(service, tmp_path, []),
        child=IMPL,
    )
    outcome, _state = walk.run(inputs)
    assert outcome == "stop_success"
    assert "analyze_site" not in walk.trace


def test_scrape_a_site_changed_review_is_retested(service, tmp_path):
    failing = example("post_mappings_test.response.json")
    failing["passed"] = False
    # The stored mapping passes the first test; the "review" made it fail.
    service.override(
        "POST", r"^/v1/mappings/test$", 200, example("post_mappings_test.response.json")
    )
    service.override("POST", r"^/v1/mappings/test$", 200, failing)
    walk = Walk(
        SCRAPE,
        connector=_connector(service, tmp_path),
        invoke=_action(service, tmp_path, []),
        child=IMPL,
    )
    outcome, _state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_fail" and walk.trace[-1] == "review_failed"
    assert not service.calls("PUT", "/v1/sites/")


EVENT_INPUTS = {
    "event_id": "evt_01JCXF5T6V7W8X9Y0Z1A2B3C4D",
    "event_type": "run_completed",
    "run_id": "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1",
    "resource_id": "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1",
}


def _tabular(read):
    def run(step, inputs):
        read.append(inputs["artifact_handle"])
        return {"dataset_handle": "dataset:x", "row_count": 2}

    return run


def test_events_run_completed_processes_once_and_resumes(service, tmp_path):
    reads: list = []
    scope = {**ROOT_SCOPE, "step_id": "claim_event"}
    walk = Walk(EVENTS, connector=_connector(service, tmp_path, scope), tabular=_tabular(reads))
    outcome, state = walk.run(dict(EVENT_INPUTS))
    assert outcome == "stop_success" and walk.trace[-1] == "handled"
    assert reads == ["artifact:res_out"]
    assert state["result"]["record_count"] == service.result_bytes.count(b"\n")
    assert (tmp_path / "result-artifact.jsonl").read_bytes() == service.result_bytes
    assert not walk.reads_missing, walk.reads_missing
    # The same delivery again (another run): already handled, nothing runs.
    other = {**scope, "job_id": "9a9a9a9a-0000-4000-8000-000000000002"}
    walk = Walk(EVENTS, connector=_connector(service, tmp_path, other), tabular=_tabular(reads))
    outcome, _state = walk.run(dict(EVENT_INPUTS))
    assert walk.trace[-1] == "not_ours" and reads == ["artifact:res_out"]


def test_events_resend_resumes_after_processed(service, tmp_path):
    reads: list = []
    scope = {**ROOT_SCOPE, "step_id": "claim_event"}
    event_id = EVENT_INPUTS["event_id"]
    service.events[event_id] = {
        "state": "pending",
        "claim_id": None,
        "claimant": None,
        "stages": ["processed"],
        "generation": 2,
        "live": True,
        "resend_mode": "resume",
    }
    walk = Walk(EVENTS, connector=_connector(service, tmp_path, scope), tabular=_tabular(reads))
    outcome, _state = walk.run(dict(EVENT_INPUTS))
    assert outcome == "stop_success"
    assert "get_result" not in walk.trace and reads == []
    # m6: back through Mark processed (a recorded stage is a no-op), so the
    # "after completion" steps between it and Mark completed run again.
    assert walk.trace[walk.trace.index("already_processed") + 1] == "mark_processed"
    assert "mark_completed" in walk.trace and "complete_event" in walk.trace
    service.events[event_id] = {
        "state": "pending",
        "claim_id": None,
        "claimant": None,
        "stages": ["processed", "completed"],
        "generation": 3,
        "live": True,
    }
    walk = Walk(EVENTS, connector=_connector(service, tmp_path, scope), tabular=_tabular(reads))
    walk.run(dict(EVENT_INPUTS))
    assert "run_summary" not in walk.trace and walk.trace[-2:] == ["complete_event", "handled"]


@pytest.mark.parametrize("passes", [True, False])
def test_events_mapping_required(service, tmp_path, passes):
    inputs = {
        **EVENT_INPUTS,
        "event_type": "mapping_required",
        "resource_id": "mrq_01JCXB1G0Z9Y8X7W6V5T4S3R2Q",
    }
    service.event_bodies[inputs["event_id"]] = {
        "event_type": "mapping_required",
        "resource_id": inputs["resource_id"],
        "template_id": "spare_part",
        "outcome": None,
        "stop_reason": None,
    }
    if not passes:
        failing = example("post_mappings_test.response.json")
        failing["passed"] = False
        for _ in range(5):
            service.override("POST", r"^/v1/mappings/test$", 200, failing)
    repaired = example("mapping.spare_part.json")
    repaired["label"] = "Spare part sheet (repaired)"
    walk = Walk(
        EVENTS,
        connector=_connector(service, tmp_path, {**ROOT_SCOPE, "step_id": "claim_event"}),
        invoke=_action(service, tmp_path, [repaired]),
        child=IMPL,
    )
    outcome, state = walk.run(inputs)
    assert outcome == "stop_success"
    if passes:
        assert "activate_repair" in walk.trace
        body = service.calls("PUT", "/v1/sites/lumen-shop.example/mappings")[0]["body"]
        assert (
            body["expected_version"] == 1
            and body["origin"]["mapping_request_id"] == inputs["resource_id"]
        )
        # The fake's spare_part is at version 2: dropped, not an error; the request
        # is then closed (m5) and the event completed.
        assert state["repair"]["conflict"] is True
        assert walk.trace[-4:] == [
            "repair_conflict",
            "report_conflict",
            "complete_event",
            "handled",
        ]
    else:
        assert "report_failure" in walk.trace
        assert service.calls("POST", "/v1/mapping-requests/")
    assert not walk.reads_missing, walk.reads_missing


@pytest.mark.parametrize("event_type", ["problem", "run_failed"])
def test_events_problem_paths(service, tmp_path, event_type):
    fields = {"event_type": event_type, "outcome": None, "stop_reason": None}
    if event_type == "problem":
        fields["problem_code"] = "block_rate_high"
    service.event_bodies[EVENT_INPUTS["event_id"]] = fields
    walk = Walk(
        EVENTS, connector=_connector(service, tmp_path, {**ROOT_SCOPE, "step_id": "claim_event"})
    )
    outcome, state = walk.run({**EVENT_INPUTS, "event_type": event_type})
    assert outcome == "stop_success"
    assert "problem_summary" in walk.trace and "complete_event" in walk.trace
    assert state["problem"]["terminal"] is True


# ------------------------------------------- platform rules the walker enforces

WORKFLOWS = {"scrape": SCRAPE, "events": EVENTS, "impl": IMPL, "maint": MAINT}


def _required_only(workflow):
    rows = workflow["settings"]["callable"]["expected_inputs"]
    return {row["name"]: "x" for row in rows if row["required"]}


def _input_reads(workflow):
    """(step, input name, selector) of every connector input that reads state.inputs.*."""
    for step in workflow["steps"]:
        if step["step_kind"] != "connector":
            continue
        for name, selector in (step.get("input_mapping_jsonb") or {}).items():
            if isinstance(selector, str) and selector.startswith("state.inputs."):
                yield step["step_id"], name, selector


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_every_input_mapping_resolves_with_only_the_required_inputs(name):
    """B4 / M1: a run (or child run) that supplies only its required inputs.

    Run start and the Invoke leave an optional input out of state.inputs
    entirely; a connector input reading it without `|default:` is unresolved and
    the step fails before the connector is called.
    """
    workflow = WORKFLOWS[name]
    state = {"inputs": _required_only(workflow)}
    optional = {
        row["name"]
        for row in workflow["settings"]["callable"]["expected_inputs"]
        if not row["required"]
    }
    seen_optional = set()
    for step_id, input_name, selector in _input_reads(workflow):
        found, _value = resolve(state, selector)
        assert found, f"{name}.{step_id}.{input_name} <- {selector}: unresolved without a default"
        source = split_default(selector)[0][len("state.inputs.") :].split(".")[0]
        if source in optional:
            seen_optional.add(source)
            assert split_default(selector)[2], selector
    if name == "scrape":
        # run_name only titles the job (settings.run_title); no step reads it.
        assert seen_optional == optional - {"run_name"}
    if name == "impl":
        assert {"required_fill_threshold", "required_fields", "extraction_mode"} <= seen_optional
        assert "max_refinements" in seen_optional


def test_default_filters_give_the_values_the_handlers_expect():
    step = next(s for s in IMPL["steps"] if s["step_id"] == "test_candidate")
    defaults = {
        name: split_default(selector)[1]
        for name, selector in step["input_mapping_jsonb"].items()
        if split_default(selector)[2]
    }
    assert defaults == {
        "required_fill_threshold": 0.9,
        "required_fields": [],
        "extraction_mode": "auto",
        "max_refinements": 2,
        "target_page_selector": "",
        "variant_sample_ids": [],
    }
    validate = next(s for s in SCRAPE["steps"] if s["step_id"] == "validate_settings")
    for name, selector in validate["input_mapping_jsonb"].items():
        if split_default(selector)[2]:
            # null is "not set in this layer" for the handlers (common.is_unset).
            assert split_default(selector)[1] is None, name


def _write_targets(workflow):
    for step in workflow["steps"]:
        override = step.get("override") or {}
        yield from (step.get("output_mapping_jsonb") or {}).values()
        yield from (override.get("invoke_workflow", {}).get("output_mapping") or {}).values()
        yield from (override.get("set_variable", {}).get("assignments") or {})
        if "tabular_feed" in override:
            yield override["tabular_feed"]["result_target"]


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_no_step_writes_a_reserved_state_root(name):
    """B3: state_path_guards.RESERVED_STATE_ROOTS refuses these at save time and at run time."""
    workflow = WORKFLOWS[name]
    targets = list(_write_targets(workflow))
    assert targets
    for target in targets:
        assert target.startswith("state."), target
        root = target[len("state.") :].split(".")[0]
        assert root not in RESERVED_STATE_ROOTS, f"{name}: {target}"
    for row in workflow["settings"]["callable"]["expected_outputs"]:
        assert row["name"] not in RESERVED_STATE_ROOTS, row["name"]


def test_scrape_a_site_with_only_the_required_inputs(service, tmp_path):
    """The trial run of the setup guide minus max_records: two fields, nothing else."""
    service.mappings.clear()
    mappings = [example("mapping.product.json"), example("mapping.spare_part.json")]
    walk = Walk(
        SCRAPE,
        connector=_connector(service, tmp_path),
        invoke=_action(service, tmp_path, mappings),
        child=IMPL,
    )
    outcome, state = walk.run(
        {
            "site_url": SCRAPE_INPUTS["site_url"],
            "extraction_instructions": SCRAPE_INPUTS["extraction_instructions"],
        }
    )
    assert outcome == "stop_success", (walk.trace, walk.failures)
    assert "build_mapping" in walk.trace and state["scraper_run_id"].startswith("run_")
    assert not walk.reads_missing and not walk.failures
    body = service.calls("POST", "/v1/config/validate")[0]["body"]
    assert "fields" not in body["run_inputs"] or body["run_inputs"]["fields"] in (None, [])
    assert not service.schema_violations


def test_a_missing_optional_input_without_default_fails_the_step(service, tmp_path):
    """The walker is as strict as the platform: this is what B4 looked like."""
    broken = copy.deepcopy(SCRAPE)
    step = next(s for s in broken["steps"] if s["step_id"] == "validate_settings")
    step["input_mapping_jsonb"]["urls"] = "state.inputs.urls"
    walk = Walk(broken, connector=_connector(service, tmp_path))
    outcome, _state = walk.run(
        {"site_url": "https://www.lumen-shop.example/", "extraction_instructions": "x"}
    )
    assert outcome == "failed" and walk.trace == ["validate_settings"]
    assert "unresolved_required_input" in walk.failures[0]
    assert not service.calls("POST", "/v1/config/validate")


@pytest.mark.parametrize("failing", ["result", "download"])
def test_events_result_failure_ends_failed_and_keeps_the_claim(service, tmp_path, failing):
    """M2: Get result fails -> 'Processing failed'; nothing is staged or completed."""
    from fake_service import error_body

    if failing == "result":
        service.override(
            "GET",
            r"/result$",
            410,
            error_body("result_expired", "The result file has expired."),
        )
    else:
        service.result_descriptor_patch = {"sha256": "0" * 64}
    reads: list = []
    walk = Walk(
        EVENTS,
        connector=_connector(service, tmp_path, {**ROOT_SCOPE, "step_id": "claim_event"}),
        tabular=_tabular(reads),
    )
    outcome, _state = walk.run(dict(EVENT_INPUTS))
    assert outcome == "stop_fail" and walk.trace[-2:] == ["get_result", "processing_failed"]
    assert not service.calls("POST", f"/v1/events/{EVENT_INPUTS['event_id']}/stage")
    assert not service.calls("POST", f"/v1/events/{EVENT_INPUTS['event_id']}/complete")
    assert reads == []


@pytest.mark.parametrize("failing", ["child", "activation"])
def test_events_repair_error_fails_the_request_and_completes_the_event(service, tmp_path, failing):
    """M2: Build mapping or Activate fails -> fail_mapping_request, complete, job failed."""
    from fake_service import error_body

    inputs = {
        **EVENT_INPUTS,
        "event_type": "mapping_required",
        "resource_id": "mrq_01JCXB1G0Z9Y8X7W6V5T4S3R2Q",
    }
    service.event_bodies[inputs["event_id"]] = {
        "event_type": "mapping_required",
        "resource_id": inputs["resource_id"],
        "template_id": "spare_part",
        "outcome": None,
        "stop_reason": None,
    }
    if failing == "child":
        service.override(
            "POST",
            r"^/v1/mappings/test$",
            404,
            error_body("sample_not_found", "Sample expired."),
        )
    else:
        service.override(
            "PUT", r"/mappings$", 500, error_body("internal_error", "The service failed.")
        )
    walk = Walk(
        EVENTS,
        connector=_connector(service, tmp_path, {**ROOT_SCOPE, "step_id": "claim_event"}),
        invoke=_action(service, tmp_path, [example("mapping.spare_part.json")]),
        child=IMPL,
    )
    outcome, _state = walk.run(inputs)
    assert outcome == "stop_fail", (walk.trace, walk.failures)
    assert walk.trace[-3:] == ["report_repair_error", "complete_after_error", "repair_failed"]
    failed = service.calls("POST", f"/v1/mapping-requests/{inputs['resource_id']}/fail")
    assert len(failed) == 1 and "error" in failed[0]["body"]["failure_reason"]
    assert service.calls("POST", f"/v1/events/{inputs['event_id']}/complete")


def test_no_loop_in_the_templates_is_unbounded():
    """Every cycle crosses an explicitly bounded rework edge."""
    for workflow in (SCRAPE, EVENTS, MAINT, IMPL):
        execution_graph = workflow.get("execution_graph") or workflow["settings"]["execution_graph"]
        edges = execution_graph["edges"]
        graph: dict[str, set] = {}
        for edge in edges:
            if edge.get("rework"):
                assert 1 <= edge["rework_limit"] <= 5
                continue
            graph.setdefault(edge["source"]["id"], set()).add(edge["target"]["id"])
        state: dict[str, int] = {}

        def visit(node, graph=graph, state=state):
            state[node] = 1
            for nxt in graph.get(node, ()):
                assert state.get(nxt) != 1, f"cycle through {nxt}"
                if nxt not in state:
                    visit(nxt)
            state[node] = 2

        for node in {s["step_id"] for s in workflow["steps"]}:
            if node not in state:
                visit(node)


@pytest.mark.parametrize("name", sorted(BUNDLES))
def test_yaml_copy_for_the_import_dialog_equals_the_bundle(name):
    """M3: 'Upload YAML' accepts .yaml/.yml only; the importer parses with yaml.safe_load
    (workflow_portability_import.py:136), so the shipped YAML must carry the same data."""
    target = ROOT / "templates" / name.replace(".json", ".yaml")
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == BUNDLES[name]
    # The JSON is valid YAML too (it can be pasted into the text box).
    assert yaml.safe_load((ROOT / "templates" / name).read_text()) == BUNDLES[name]


@pytest.mark.parametrize("workflow", [SCRAPE, EVENTS, MAINT], ids=["scrape", "events", "maint"])
def test_canvas_notes_never_cover_a_step(workflow):
    """A8: the bundle carries no step positions, so the builder lays the steps out
    itself (assets/frontend/src/pages/workflows/builder/layout.ts: columns from x = 0,
    rows from y = 0, then shifted right by CANVAS_MARGIN_X + START_WIDTH + COLUMN_GAP).
    Every step therefore sits at x >= 0; the notes stand in a column left of it."""
    assert "canvas_layout" not in workflow["settings"]
    notes = workflow["settings"]["canvas_annotations"]
    for note in notes:
        assert note["x"] + note["width"] <= -80, note["id"]
    ordered = sorted(notes, key=lambda n: n["y"])
    for upper, lower in zip(ordered, ordered[1:], strict=False):
        assert upper["y"] + upper["height"] <= lower["y"], (upper["id"], lower["id"])


def test_scraper_events_accepts_event_fields_it_does_not_declare():
    """The webhook body is the whole event (occurred_at, delivery_generation, outcome ...,
    and fields a later contract adds). With strict inputs the platform refuses every
    undeclared body key (workflow_entrypoint_input_service.py:113-160); the platform
    check tests/tools/check_webhook_inputs.py runs that code over the contract examples."""
    assert EVENTS["settings"]["callable"]["strict_inputs"] is False
    assert SCRAPE["settings"]["callable"]["strict_inputs"] is True
