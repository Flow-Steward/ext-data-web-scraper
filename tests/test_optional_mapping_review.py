"""Optional coverage gets bounded review without losing a tested required pass."""

import copy
import json

import pytest
from fake_service import example
from test_build_mapping import INPUTS, _Agent, _walk
from workflow_sim import StepFailed


def _candidate(label):
    mappings = [example("mapping.product.json")]
    mappings[0]["label"] = label
    return mappings


def _optional_warning():
    response = example("post_mappings_test.response.json")
    response["templates"] = [
        row for row in response["templates"] if row["template_id"] == "product"
    ]
    response["samples"] = [
        row for row in response["samples"] if row["template_id"] == "product"
    ]
    response["templates"][0].update(
        variant_count=7,
        samples_with_variants=1,
        variant_sources_used={"embedded_json": 1},
        variant_fields=[
            {
                "name": "description",
                "variant_count": 7,
                "filled_count": 6,
                "fill_rate": 6 / 7,
                "example_values": ["An edition description"],
            }
        ],
    )
    return response


def _required_failure():
    response = example("post_mappings_test.response.json")
    response["passed"] = False
    response["templates"][0]["passed"] = False
    response["templates"][0]["fields"][0].update(
        passed=False, filled_count=0, fill_rate=0.0
    )
    return response


def _responses(service, responses):
    for response in responses:
        service.override("POST", r"^/v1/mappings/test$", 200, copy.deepcopy(response))


def test_optional_coverage_warning_reaches_a_review_then_clean_pass(service):
    original, reviewed = _candidate("initial"), _candidate("reviewed")
    _responses(service, [_optional_warning()])
    agent = _Agent([original, reviewed])
    walk, outcome, state = _walk(service, dict(INPUTS), agent, failing_tests=0)
    assert state["attempts"] == 2
    assert outcome == "stop_success" and state["passed"] is True
    assert state["mappings"] == reviewed
    assert walk.trace.count("write_samples") == 2
    assert agent.calls[1]["previous_mappings"] == original
    feedback = agent.calls[1]["failures"]
    assert any(
        row["kind"] == "variant_fill"
        and row["field"] == "description"
        and "6/7" in row["message"]
        for row in feedback
    )
    assert state["failures"] == []
    assert state["warnings"] == []


@pytest.mark.parametrize("refinements", [0, 1, 2])
def test_legitimate_optional_absence_stays_nonfatal_and_bounded(service, refinements):
    original = _candidate("legitimate missing optional value")
    _responses(service, [_optional_warning()] * 4)
    agent = _Agent([original])
    walk, outcome, state = _walk(
        service, {**INPUTS, "max_refinements": refinements}, agent, failing_tests=0
    )
    assert state["attempts"] == refinements + 1
    assert outcome == "stop_success" and state["passed"] is True
    assert len(agent.calls) == refinements + 1
    assert state["mappings"] == original
    assert state["failures"] == []
    assert "Warning:" in state["summary"] and "6/7" in state["summary"]
    description = next(
        field
        for field in state["mappings"][0]["fields"]
        if field["name"] == "description"
    )
    assert description.get("required", False) is False
    assert (
        walk.outputs["passed"] is True and walk.outputs["summary"] == state["summary"]
    )


def test_clean_required_pass_does_not_spend_optional_review_budget(service):
    agent = _Agent([_candidate("clean")])
    walk, outcome, state = _walk(service, dict(INPUTS), agent, failing_tests=0)
    assert outcome == "stop_success" and state["passed"] is True
    assert state["attempts"] == 1
    assert walk.trace.count("write_mapping") == 1
    assert state["warnings"] == []


@pytest.mark.parametrize("invalid", [False, True])
def test_optional_review_regression_returns_last_tested_passing_candidate(
    service, invalid
):
    original, latest, regressed = (
        _candidate("original pass"),
        _candidate("latest pass"),
        _candidate("regressed"),
    )
    _responses(service, [_optional_warning(), _optional_warning(), _required_failure()])
    agent = _Agent([original, latest, [] if invalid else regressed])
    walk, outcome, state = _walk(service, dict(INPUTS), agent, failing_tests=0)
    assert state["attempts"] == 3
    assert outcome == "stop_success" and state["passed"] is True
    assert state["mappings"] == latest
    assert state["failures"] == []
    assert all(row["passed"] for row in state["field_results"] if row["required"])
    assert "Warning:" in state["summary"] and "6/7" in state["summary"]
    assert walk.outputs["mappings"] == latest


def test_optional_review_provider_failure_retains_tested_passing_candidate(service):
    original = _candidate("tested before provider failure")
    _responses(service, [_optional_warning()])
    calls = []

    def agent(step, inputs):
        calls.append(inputs)
        if len(calls) == 1:
            return {"mappings_json": json.dumps(original), "notes": "first candidate"}
        raise StepFailed("model provider unavailable during optional review")

    walk, outcome, state = _walk(service, dict(INPUTS), agent, failing_tests=0)
    assert len(calls) == 2
    assert outcome == "stop_success" and state["passed"] is True
    assert state["mappings"] == original
    assert state["failures"] == []
    assert "Warning:" in state["summary"]
    assert walk.outputs["mappings"] == original


def test_initial_provider_failure_cannot_claim_a_passing_mapping(service):
    def agent(step, inputs):
        raise StepFailed("model provider unavailable before any candidate")

    _walk_result, outcome, state = _walk(service, dict(INPUTS), agent, failing_tests=0)
    assert outcome in {"failed", "stop_fail"}
    assert not state.get("passed")
