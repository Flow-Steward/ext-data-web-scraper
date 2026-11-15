"""The composite action build_mapping: its implementation workflow, agent and skill."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
import json
import tempfile

from conftest import make_payload, samples_descriptor
from fake_service import example
from workflow_sim import Walk

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / "artifacts/workflows/impl_build_mapping.yaml").read_text())
AGENT = yaml.safe_load((ROOT / "artifacts/agents/scraper_mapping_author.yaml").read_text())
SKILL = yaml.safe_load((ROOT / "artifacts/skills/build_extraction_mapping.yaml").read_text())
DESCRIPTOR = yaml.safe_load((ROOT / "extension-descriptor.yaml").read_text())
OPERATION = next(
    op
    for op in yaml.safe_load((ROOT / "contracts/operation_manifest.yaml").read_text())["operations"]
    if op["operation_id"] == "build_mapping"
)


def test_descriptor_publishes_the_implementation_agent_and_skill():
    artifacts = DESCRIPTOR["artifacts"]
    assert [w["workflow_id"] for w in artifacts["workflows"]] == [
        "impl_build_mapping",
        "scrape_a_site",
        "scraper_events",
        "service_maintenance",
    ]
    assert [a["agent_id"] for a in artifacts["agents"]] == ["scraper_mapping_author"]
    assert [s["skill_id"] for s in artifacts["skills"]] == ["build_extraction_mapping"]
    for kind, key in (("workflows", "workflow_id"), ("agents", "agent_id"), ("skills", "skill_id")):
        for entry in artifacts[kind]:
            loaded = yaml.safe_load((ROOT / entry["ref"]).read_text())
            assert loaded[key] == entry[key]


def test_interface_equals_the_operation():
    callable_settings = WORKFLOW["settings"]["callable"]
    assert callable_settings["trigger_channels"] == ["parent_workflow"]
    assert [row["name"] for row in callable_settings["expected_inputs"]] == [
        row["name"] for row in OPERATION["inputs"]
    ]
    assert [row["name"] for row in callable_settings["expected_outputs"]] == [
        row["name"] for row in OPERATION["outputs"]
    ]
    assert "connection_type_ids" not in OPERATION


def test_marker_slot_and_body_rules():
    marker = WORKFLOW["settings"]["action_implementation"]
    assert marker["action_id"] == "build_mapping"
    assert marker["connections"] == [
        {
            "slot": "profile",
            "extension_id": "flowsteward.web-scraper",
            "connection_type_ids": ["scraper_profile"],
        }
    ]
    kinds = {step["step_kind"] for step in WORKFLOW["steps"]}
    assert kinds <= {"set_variable", "agent", "connector", "if", "stop_success"}
    assert "hooks" not in WORKFLOW and "hook" not in str(WORKFLOW["steps"])
    for step in WORKFLOW["steps"]:
        if step["step_kind"] == "connector":
            assert step["connector_id"] == "extension.flowsteward.web-scraper"
            assert step["connection_binding_jsonb"] == {"slot": "profile"}
            assert "connector_install_id" not in step
            assert "connection_id" not in str(step)
        if step["step_kind"] == "agent":
            assert step["agent_id"] == AGENT["agent_id"] and step["skill_id"] == SKILL["skill_id"]


def test_graph_is_complete():
    steps = {s["step_id"]: s for s in WORKFLOW["steps"]}
    edges = WORKFLOW["settings"]["execution_graph"]["edges"]
    out: dict[str, set[str]] = {}
    for edge in edges:
        out.setdefault(edge["source"]["id"], set()).add(edge["outcome"]["name"])
        if edge.get("rework"):
            assert edge["outcome"]["name"] in {"true", "false"}
            assert 1 <= edge["rework_limit"] <= 50
    for step_id, step in steps.items():
        expected = {"if": {"true", "false"}, "stop_success": set()}.get(
            step["step_kind"], {"success"}
        )
        assert out.get(step_id, set()) == expected, step_id
    reachable, todo = set(), [WORKFLOW["settings"]["execution_graph"]["entry"]["id"]]
    while todo:
        node = todo.pop()
        if node in reachable:
            continue
        reachable.add(node)
        todo.extend(e["target"]["id"] for e in edges if e["source"]["id"] == node)
    assert reachable == set(steps)


def test_agent_inputs_are_skill_inputs_and_the_prompt_guards_page_content():
    skill_inputs = {row["name"] for row in SKILL["expected_inputs"]}
    agent_step = next(s for s in WORKFLOW["steps"] if s["step_kind"] == "agent")
    assert set(agent_step["input_mapping_jsonb"]) <= skill_inputs
    assert set(agent_step["output_mapping_jsonb"]) <= {
        row["name"] for row in SKILL["expected_outputs"]
    }
    text = (
        AGENT["prompts"]["system"] + " ".join(SKILL["instructions"]) + SKILL["prompt_template"]
    ).lower()
    for phrase in ("untrusted", "never", "json-ld", "no code", "css"):
        assert phrase in text, phrase
    # The skill's example is itself a valid mapping.
    from fake_service import schema_errors

    for mapping in SKILL["examples"][0]["output"]["mappings"]:
        assert not schema_errors("mapping.schema.json", mapping)


class _Agent:
    """Scripted mapping agent: a broken candidate first, then a good one."""

    def __init__(self, candidates):
        self.candidates = list(candidates)
        self.calls: list[dict] = []

    def __call__(self, step, inputs):
        self.calls.append(inputs)
        candidate = self.candidates.pop(0) if len(self.candidates) > 1 else self.candidates[0]
        return {"mappings_json": json.dumps(candidate), "notes": "scripted"}


def _walk(service, invoke_inputs, agent, failing_tests: int):
    from handlers.router import handle_request

    failing = example("post_mappings_test.response.json")
    failing["passed"] = False
    failing["templates"][0]["passed"] = False
    failing["templates"][0]["fields"][2].update(
        {"passed": False, "filled_count": 0, "fill_rate": 0.0}
    )
    for _ in range(failing_tests):
        service.override("POST", r"^/v1/mappings/test$", 200, failing)

    def connector(step, inputs):
        operation = step["override"]["connector"]["operation_id"]
        artifacts = None
        if operation == "write_samples_file":
            folder = Path(tempfile.mkdtemp(prefix="ws_samples_"))
            artifacts = {"inputs": [], "outputs": [samples_descriptor(folder)]}
        payload = make_payload(operation, inputs, service_url=service.url, artifacts=artifacts)
        response = handle_request(payload)
        assert response["ok"], response
        return response["result"]

    walk = Walk(WORKFLOW, connector=connector, agent=agent)
    return walk, *walk.run(invoke_inputs)


INPUTS = {
    "site": "lumen-shop.example",
    "sample_ids": ["smp_01JCX9Q1A2B3C4D5E6F7G8H9J0"],
    "samples": [
        {
            "sample_id": "smp_01JCX9Q1A2B3C4D5E6F7G8H9J0",
            "url": "https://www.lumen-shop.example/produkt/x",
        }
    ],
    "extraction_instructions": "Lamps: name, price, currency.",
    "fields": ["title", "price", "currency"],
    "required_fields": ["title", "price"],
    "required_fill_threshold": 0.9,
    "extraction_mode": "auto",
}


def test_refines_with_failures_then_passes(service):
    broken = [example("mapping.product.json")]
    good = [example("mapping.product.json")]
    agent = _Agent([broken, good])
    walk, outcome, state = _walk(service, dict(INPUTS), agent, failing_tests=1)
    assert outcome == "stop_success" and walk.trace[-1] == "done_passed"
    assert state["passed"] is True and state["attempts"] == 2
    assert walk.trace.count("write_mapping") == 2
    second = agent.calls[1]
    assert second["failures"] and second["previous_mappings"] == broken
    assert agent.calls[0]["attempt"] == 0 and second["attempt"] == 1


def test_gives_up_after_two_refinements(service):
    agent = _Agent([[example("mapping.product.json")]])
    walk, outcome, state = _walk(service, dict(INPUTS), agent, failing_tests=10)
    assert outcome == "stop_success" and walk.trace[-1] == "done_not_passed"
    assert state["passed"] is False and state["attempts"] == 3
    assert walk.trace.count("write_mapping") == 3
    assert state["failures"]


@pytest.mark.parametrize(("refinements", "generations"), [(0, 1), (1, 2), (4, 5)])
def test_refinements_are_configurable(service, refinements, generations):
    agent = _Agent([[example("mapping.product.json")]])
    walk, _outcome, state = _walk(
        service, {**INPUTS, "max_refinements": refinements}, agent, failing_tests=10
    )
    assert walk.trace.count("write_mapping") == generations
    assert state["attempts"] == generations


def test_repair_starts_from_the_previous_mapping(service):
    previous = [example("mapping.spare_part.json")]
    agent = _Agent([[example("mapping.spare_part.json")]])
    _walk(
        service,
        {**INPUTS, "previous_mappings": previous, "previous_failures": {"fields": []}},
        agent,
        failing_tests=0,
    )
    assert agent.calls[0]["previous_mappings"] == previous
    assert agent.calls[0]["service_failures"] == {"fields": []}
    assert agent.calls[0]["failures"] == []


def test_runs_with_only_the_required_inputs(service):
    """M1: an Invoke leaves empty optional inputs out of the child's state.inputs.

    `test_candidate` then reads them through `|default:` (connector_mapping.py
    110-144); without it the connector step fails with unresolved_required_input.
    """
    agent = _Agent([[example("mapping.product.json")]])
    required = {name: INPUTS[name] for name in ("site", "sample_ids", "samples")}
    walk, outcome, state = _walk(service, required, agent, failing_tests=10)
    assert outcome == "stop_success" and walk.trace[-1] == "done_not_passed"
    assert not walk.reads_missing and not walk.failures
    assert state["attempts"] == 3  # the default max_refinements: 2
    bodies = [call["body"] for call in service.calls("POST", "/v1/mappings/test")]
    assert bodies and all(body["required_fill_threshold"] == 0.9 for body in bodies)
