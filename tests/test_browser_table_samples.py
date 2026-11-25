"""Browser table acquisition is explicit, bounded and verified offline before acceptance."""

import copy
import json

import pytest

from fake_service import example, schema_errors
from test_build_mapping import INPUTS, _Agent
from test_templates import IMPL, SCRAPE, EVENTS, EVENT_INPUTS, SCRAPE_INPUTS, _action, _connector
from workflow_sim import Walk

from handlers.mappings import _agent_samples, structural_problems
from handlers.samples_file import render_samples

SOURCE = {
    "kind": "browser_table",
    "selector": "#compatibility",
    "row_selector": "tbody tr",
    "cell_selector": "td",
    "loading_selector": ".loading",
    "empty_selector": ".empty",
    "next_selector": ".next",
    "current_page_selector": ".current",
    "max_pages": 3,
}
SID = INPUTS["sample_ids"][0]
URL = INPUTS["samples"][0]["url"]


def candidate(source=None):
    mapping = example("mapping.product.json")
    mapping["fields"].append(
        {
            "name": "compatibility",
            "type": "html",
            "required": False,
            "sources": [copy.deepcopy(source or SOURCE)],
        }
    )
    return [mapping]


def report(reason="uncaptured", complete=False, sid=SID, url=URL):
    response = example("post_mappings_test.response.json")
    response["templates"] = response["templates"][:1]
    response["samples"] = response["samples"][:1]
    response["passed"] = True  # Required ordinary fields pass; optional table must still verify.
    response["templates"][0]["empty_fields"] = []
    response["samples"][0].update(sample_id=sid, url=url)
    response["samples"][0]["table_coverage"] = {
        "compatibility": {
            "complete": complete,
            "reason": "finished" if complete else reason,
            "pages": 1 if complete else 0,
            "bytes": 20 if complete else 0,
            "row_count": 0,
        }
    }
    return response


def config():
    return example("post_config_validate.response.json")["config"]


def test_table_source_contract_and_structural_validation():
    assert not schema_errors("mapping.schema.json", candidate()[0])
    assert structural_problems(candidate()) == []


def test_fetch_samples_passes_sources_at_top_level_only(invoke, service):
    response = invoke("fetch_samples", {"config": config(), "table_sources": [SOURCE]})
    assert response["ok"], response
    body = service.calls("POST", "/v1/samples")[0]["body"]
    assert body["table_sources"] == [SOURCE]
    assert "table_sources" not in body["config"]


@pytest.mark.parametrize(
    "reason",
    [
        "uncaptured",
        "fingerprint_mismatch",
        "evidence_unavailable",
        "policy_denied",
        "page_limit",
        "time_limit",
        "browser_required",
    ],
)
def test_table_testing_stays_offline_and_rejects_unknown_optional_table(invoke, service, reason):
    service.override("POST", r"^/v1/mappings/test$", 200, report(reason))
    response = invoke(
        "test_mapping", {"site": INPUTS["site"], "mappings": candidate(), "sample_ids": [SID]}
    )
    assert response["ok"], response
    result = response["result"]
    assert not result["passed"]
    assert result["table_resample_needed"] is (
        reason in {"uncaptured", "fingerprint_mismatch", "evidence_unavailable"}
    )
    assert (
        result["table_sources"] == [SOURCE]
        if result["table_resample_needed"]
        else not result["table_sources"]
    )
    assert not service.calls("POST", "/v1/samples")
    assert any(row["kind"] == "table_unverified" for row in result["failures"])
    assert reason in result["summary"]


def test_complete_explicit_empty_table_is_acceptable(invoke, service):
    service.override("POST", r"^/v1/mappings/test$", 200, report(complete=True))
    result = invoke(
        "test_mapping", {"site": INPUTS["site"], "mappings": candidate(), "sample_ids": [SID]}
    )["result"]
    assert result["passed"]
    assert not result["table_resample_needed"]


def test_table_evidence_projection_is_bounded_and_omits_private_receipts():
    sample = {
        **INPUTS["samples"][0],
        "table_evidence": {
            "retained": False,
            "reason": "storage_limit",
            "key": "table_evidence_private",
            "run_id": "run_private",
            "path": "/private/archive",
        },
        "table_captures": [
            {
                "source_key": "a" * 64,
                "complete": True,
                "reason": "finished",
                "pages": 2,
                "bytes": 1000000,
                "headers": ["Model", "From", "To"],
                "rows": [["Mock model", "2011-09", ""]] * 10000,
            }
        ],
    }
    projected = _agent_samples([sample])
    text, _ = render_samples(INPUTS["site"], projected)
    assert "2011-09" in text and "Table" in text
    assert "storage_limit" in text and "shorten" in text.lower()
    assert "table_evidence_private" not in text and "/private/archive" not in text
    assert "run_private" not in text
    assert len(json.dumps(projected)) < 100000


def test_ordinary_forms_and_agent_tools_stay_unchanged():
    import yaml
    from pathlib import Path

    root = Path(__file__).parents[1]
    agent = yaml.safe_load((root / "artifacts/agents/scraper_mapping_author.yaml").read_text())
    assert agent["tools"] == []
    for workflow in [SCRAPE, EVENTS]:
        fields = workflow["settings"]["callable"]["expected_inputs"]
        assert "table_sources" not in {row["name"] for row in fields}


def fresh_samples():
    response = example("post_samples.response.json")
    response["samples"] = response["samples"][:1]
    response["samples"][0].update(sample_id=SID[:-1] + "1", url=URL)
    return response


def child_walk(service, tmp_path, responses, candidates=None, max_refinements=0):
    for response in responses:
        service.override("POST", r"^/v1/mappings/test$", 200, response)
    service.override("POST", r"^/v1/samples$", 200, fresh_samples())
    agent = _Agent(candidates or [candidate()])
    walk = Walk(IMPL, connector=_connector(service, tmp_path), agent=agent)
    outcome, state = walk.run({**INPUTS, "config": config(), "max_refinements": max_refinements})
    return walk, outcome, state, agent


def test_child_explicitly_reacquires_once_and_retests_without_spending_attempt(service, tmp_path):
    fresh = fresh_samples()["samples"][0]
    walk, outcome, state, agent = child_walk(
        service, tmp_path, [report(), report(complete=True, sid=fresh["sample_id"])]
    )
    assert outcome == "stop_success", walk.failures
    assert state["passed"] is True
    assert state["attempts"] == 1 and len(agent.calls) == 1
    requests = service.calls("POST", "/v1/samples")
    assert len(requests) == 1
    assert requests[0]["body"]["urls"] == [URL]
    assert requests[0]["body"]["table_sources"] == [SOURCE]
    assert service.calls("POST", "/v1/mappings/test")[1]["body"]["sample_ids"] == [
        fresh["sample_id"]
    ]
    assert walk.outputs["sample_ids"] == [fresh["sample_id"]]


@pytest.mark.parametrize(
    "reason", ["policy_denied", "page_limit", "time_limit", "storage_limit", "browser_required"]
)
def test_child_never_reacquires_policy_or_budget_failure(service, tmp_path, reason):
    _walk, outcome, state, _agent = child_walk(service, tmp_path, [report(reason)])
    assert outcome == "stop_success"
    assert state["passed"] is False
    assert not service.calls("POST", "/v1/samples")


def test_changed_source_after_budget_is_not_fetched_and_fallback_restores_evidence(
    service, tmp_path
):
    fresh = fresh_samples()["samples"][0]
    passing = report(complete=True, sid=fresh["sample_id"])
    passing["templates"][0]["empty_fields"] = ["description"]
    changed = candidate(SOURCE | {"selector": "#different"})
    walk, outcome, state, agent = child_walk(
        service,
        tmp_path,
        [report(), passing, report(sid=fresh["sample_id"])],
        [candidate(), changed],
        1,
    )
    assert outcome == "stop_success" and state["passed"]
    assert state["mappings"] == candidate()
    assert "unverified" in state["summary"]
    assert len(agent.calls) == 2 and len(service.calls("POST", "/v1/samples")) == 1
    assert walk.outputs["sample_ids"] == [fresh["sample_id"]]
    assert walk.outputs["samples"][0]["sample_id"] == fresh["sample_id"]


def test_parent_and_repair_use_private_sampling_config_and_returned_sample_ids():
    build = next(row for row in SCRAPE["steps"] if row["step_id"] == "build_mapping")
    normal = build["override"]["invoke_workflow"]
    assert normal["input_mapping"]["config"] == "state.config"
    assert normal["output_mapping"]["sample_ids"] == "state.sample_ids"
    repair = next(row for row in EVENTS["steps"] if row["step_id"] == "repair_mapping")
    repair = repair["override"]["invoke_workflow"]
    assert repair["input_mapping"]["config"] == "state.request.sampling_config"
    assert repair["output_mapping"]["sample_ids"] == "state.repair.sample_ids"
    retest = next(row for row in EVENTS["steps"] if row["step_id"] == "retest_repair")
    assert retest["input_mapping_jsonb"]["sample_ids"] == "state.repair.sample_ids"


def test_missing_offline_sample_coverage_cannot_approve_browser_table(invoke, service):
    response = report(complete=True)
    response["samples"] = []
    service.override("POST", r"^/v1/mappings/test$", 200, response)
    result = invoke(
        "test_mapping", {"site": INPUTS["site"], "mappings": candidate(), "sample_ids": [SID]}
    )["result"]
    assert result["passed"] is False
    assert not result["table_resample_needed"]


def test_samples_file_also_redacts_unprojected_private_receipts():
    sample = {
        **INPUTS["samples"][0],
        "table_evidence": {
            "retained": False,
            "reason": "storage_limit",
            "key": "private_capture_key",
            "run_id": "private_run",
            "path": "/private/store",
        },
    }
    text, _ = render_samples(INPUTS["site"], [sample])
    assert "storage_limit" in text
    assert "private_capture_key" not in text and "/private/store" not in text


def test_reacquisition_keeps_entire_tested_sample_set_and_all_candidate_sources():
    from handlers.table_samples import table_feedback

    mappings = candidate()
    other_source = SOURCE | {"selector": "#other"}
    mappings[0]["fields"].append({"name": "other_table", "type": "html", "sources": [other_source]})
    response = report()
    complete = {"complete": True, "reason": "finished", "pages": 1, "bytes": 20, "row_count": 0}
    response["samples"][0]["table_coverage"]["other_table"] = complete
    second = copy.deepcopy(response["samples"][0])
    second.update(sample_id=SID[:-1] + "2", url=URL + "/other")
    second["table_coverage"]["compatibility"] = complete
    response["samples"].append(second)
    plan = table_feedback(mappings, response, [SID, second["sample_id"]])
    assert plan["table_resample_needed"]
    assert plan["table_sample_urls"] == [URL, second["url"]]
    assert plan["table_sources"] == [SOURCE, other_source]


def test_explicit_http_mode_does_not_start_reacquisition(service, tmp_path):
    service.override("POST", r"^/v1/mappings/test$", 200, report())
    walk = Walk(IMPL, connector=_connector(service, tmp_path), agent=_Agent([candidate()]))
    outcome, state = walk.run(
        {**INPUTS, "config": config() | {"fetch_mode": "http"}, "max_refinements": 0}
    )
    assert outcome == "stop_success" and not state["passed"]
    assert not service.calls("POST", "/v1/samples")
    assert "HTTP" in state["summary"]


def test_table_obligation_survives_css_downgrade_then_field_removal(invoke, service):
    service.override("POST", r"^/v1/mappings/test$", 200, report())
    first = invoke(
        "test_mapping", {"site": INPUTS["site"], "mappings": candidate(), "sample_ids": [SID]}
    )["result"]
    obligation = first["table_obligations"]
    css = candidate()
    css[0]["fields"][-1]["sources"] = [
        {"kind": "css", "selector": "#compatibility", "extract": "html"}
    ]
    for mappings in [css, [example("mapping.product.json")]]:
        result = invoke(
            "test_mapping",
            {
                "site": INPUTS["site"],
                "mappings": mappings,
                "sample_ids": [SID],
                "table_obligations": obligation,
            },
        )["result"]
        assert not result["passed"]
        assert any(row["kind"] == "table_downgrade" for row in result["failures"])
        obligation = result["table_obligations"]
    assert len(service.calls("POST", "/v1/mappings/test")) == 1


@pytest.mark.parametrize("flow", ["normal", "repair"])
def test_regular_and_repair_flow_retest_acquired_ids_before_activation(service, tmp_path, flow):
    initial = example("post_samples.response.json")
    initial["samples"] = initial["samples"][:1]
    initial["samples"][0].update(sample_id=SID, url=URL)
    proposed = candidate()
    first, complete = (
        report(),
        report(complete=True, sid=fresh_samples()["samples"][0]["sample_id"]),
    )
    if flow == "normal":
        service.override("POST", r"^/v1/samples$", 200, initial)
        workflow = SCRAPE
        inputs = {
            **SCRAPE_INPUTS,
            "rebuild_mapping": True,
            "fetch_mode": "browser",
            "target_page_selector": "div.product-detail",
        }
    else:
        proposed[0]["template_id"] = "spare_part"
        request = example("get_mapping_request.response.json")
        request["samples"] = initial["samples"]
        request["instructions"]["target_page_selector"] = "div.product-detail"
        service.override("GET", r"^/v1/mapping-requests/.*$", 200, request)
        service.run_patch = {"config": config() | {"fetch_mode": "browser"}}
        workflow = EVENTS
        inputs = {
            **EVENT_INPUTS,
            "event_type": "mapping_required",
            "resource_id": request["mapping_request_id"],
        }
        service.event_bodies[inputs["event_id"]] = {
            "event_type": "mapping_required",
            "resource_id": inputs["resource_id"],
        }
        for response in [first, complete]:
            response["templates"][0]["template_id"] = "spare_part"
            response["samples"][0]["template_id"] = "spare_part"
    service.override("POST", r"^/v1/samples$", 200, fresh_samples())
    for response in [first, complete, complete]:
        service.override("POST", r"^/v1/mappings/test$", 200, copy.deepcopy(response))
    walk = Walk(
        workflow,
        connector=_connector(service, tmp_path),
        invoke=_action(service, tmp_path, proposed),
        child=IMPL,
    )
    outcome, state = walk.run(inputs)
    assert outcome == "stop_success", (
        walk.trace,
        walk.failures,
        state.get("mapping_summary"),
        state.get("config", {}).get("fetch_mode"),
        [call["body"]["sample_ids"] for call in service.calls("POST", "/v1/mappings/test")],
    )
    tests = service.calls("POST", "/v1/mappings/test")
    assert len(tests) == 3
    assert tests[-1]["body"]["sample_ids"] == [fresh_samples()["samples"][0]["sample_id"]]
    assert (
        len(
            [
                call
                for call in service.calls("POST", "/v1/samples")
                if call["body"].get("table_sources")
            ]
        )
        == 1
    )
    assert service.calls("PUT", "/v1/sites/")
    if flow == "repair":
        assert service.calls("GET", "/v1/runs/" + request["run_id"])
        acquisition = service.calls("POST", "/v1/samples")[0]["body"]
        assert acquisition["config"] == service.run_patch["config"]


def test_unknown_optional_table_is_never_automatically_activated(service, tmp_path):
    initial = example("post_samples.response.json")
    initial["samples"] = initial["samples"][:1]
    initial["samples"][0].update(sample_id=SID, url=URL)
    service.override("POST", r"^/v1/samples$", 200, initial)
    for _ in range(3):
        service.override("POST", r"^/v1/mappings/test$", 200, report("worker_unsupported"))
    walk = Walk(
        SCRAPE,
        connector=_connector(service, tmp_path),
        invoke=_action(service, tmp_path, candidate()),
        child=IMPL,
    )
    outcome, _state = walk.run({**SCRAPE_INPUTS, "rebuild_mapping": True})
    assert outcome == "stop_fail", walk.trace
    assert not service.calls("PUT", "/v1/sites/")
    assert not service.calls("POST", "/v1/runs")
    assert len(service.calls("POST", "/v1/samples")) == 1  # Initial ordinary samples only.


def test_duplicate_served_id_cannot_expand_acquisition_url_budget():
    from handlers.table_samples import table_feedback

    response = report()
    duplicate = copy.deepcopy(response["samples"][0])
    duplicate["url"] = URL + "/unexpected"
    response["samples"].append(duplicate)
    plan = table_feedback(candidate(), response, [SID])
    assert not plan["table_resample_needed"]
    assert plan["table_sample_urls"] == []


@pytest.mark.parametrize("value", [None, []])
def test_unset_table_sources_are_omitted_for_older_services(invoke, service, value):
    response = invoke("fetch_samples", {"config": config(), "table_sources": value})
    assert response["ok"]
    assert "table_sources" not in service.calls("POST", "/v1/samples")[0]["body"]


def test_more_than_five_sources_are_unverified_without_partial_acquisition():
    from handlers.table_samples import table_feedback

    mappings = candidate()
    response = report()
    for index in range(5):
        name = f"table_{index}"
        mappings[0]["fields"].append(
            {"name": name, "type": "html", "sources": [SOURCE | {"selector": f"#table-{index}"}]}
        )
        response["samples"][0]["table_coverage"][name] = response["samples"][0]["table_coverage"][
            "compatibility"
        ]
    plan = table_feedback(mappings, response, [SID])
    assert not plan["table_resample_needed"] and plan["table_sources"] == []
    assert len(plan["table_failures"]) == 6


def test_invalid_explicit_table_sources_are_refused_not_silently_dropped(invoke, service):
    response = invoke("fetch_samples", {"config": config(), "table_sources": "not an array"})
    assert not response["ok"] and response["error_code"] == "invalid_payload"
    assert "table_sources" in response["error"]
    assert not service.calls("POST", "/v1/samples")


def test_source_deduplication_preserves_fingerprint_distinct_numeric_json():
    from handlers.table_samples import table_feedback

    first = SOURCE | {"max_seconds": 1}
    second = SOURCE | {"max_seconds": 1.0}
    mappings = candidate(first)
    mappings[0]["fields"].append({"name": "other_table", "type": "html", "sources": [second]})
    response = report()
    response["samples"][0]["table_coverage"]["other_table"] = response["samples"][0][
        "table_coverage"
    ]["compatibility"]
    plan = table_feedback(mappings, response, [SID])
    assert plan["table_resample_needed"] and len(plan["table_sources"]) == 2
    assert type(plan["table_sources"][0]["max_seconds"]) is int
    assert type(plan["table_sources"][1]["max_seconds"]) is float


def test_zero_page_claim_does_not_count_as_complete_table_evidence(invoke, service):
    response = report(complete=True)
    response["samples"][0]["table_coverage"]["compatibility"]["pages"] = 0
    service.override("POST", r"^/v1/mappings/test$", 200, response)
    result = invoke(
        "test_mapping", {"site": INPUTS["site"], "mappings": candidate(), "sample_ids": [SID]}
    )["result"]
    assert result["passed"] is False
    assert not result["table_resample_needed"]


def test_variant_table_collision_feedback_never_plans_capture(invoke, service):
    from fake_service import error_body

    mappings = candidate()
    mappings[0]["variants"] = {
        "sources": [
            {
                "kind": "embedded_json",
                "selector": "script#state",
                "path": "variants",
                "format": "generic",
            }
        ],
        "fields": [{"name": "compatibility", "path": "raw.compatibility", "type": "html"}],
    }
    message = "A browser table field cannot also be overridden by a variant; use one verified source or separate table and variant fields."
    service.override(
        "POST",
        r"^/v1/mappings/test$",
        422,
        error_body(
            "validation_failed",
            message,
            fields=[
                {
                    "field": "mappings[0].fields[6].sources",
                    "code": "ambiguous_table_variant",
                    "message": message,
                }
            ],
        ),
    )
    result = invoke(
        "test_mapping", {"site": INPUTS["site"], "mappings": mappings, "sample_ids": [SID]}
    )["result"]
    assert result["passed"] is False and not result["table_resample_needed"]
    assert any("one verified source" in row["message"] for row in result["failures"])
    assert not service.calls("POST", "/v1/samples")


def test_captured_table_html_never_bypasses_preview_budget_through_extracted():
    sample = {
        **INPUTS["samples"][0],
        "extracted": {"compatibility": "<table>" + "private full table" * 300000 + "</table>"},
        "table_captures": [
            {
                "source_key": "a" * 64,
                "complete": True,
                "reason": "finished",
                "pages": 1,
                "bytes": 4000000,
                "row_count": 9000,
                "column_count": 3,
                "preview_truncated": True,
                "headers": ["Model", "From", "To"],
                "rows": [["Mock model", "2011-09", ""]],
            }
        ],
    }
    view = _agent_samples([sample])[0]
    assert "private full table" not in json.dumps(view)
    assert view["table_captures"][0]["row_count"] == 9000
    assert view["table_captures"][0]["rows_shortened"] is True
    assert view["table_captures"][0]["column_count"] == 3
    ordinary = _agent_samples([{**INPUTS["samples"][0], "extracted": {"title": "Known title"}}])[0]
    assert ordinary["extracted"] == {"title": "Known title"}


def test_initial_previous_table_obligation_is_not_lost_with_many_new_templates(invoke):
    mappings = []
    for index in range(50):
        row = example("mapping.product.json")
        row["template_id"] = f"template_{index}"
        mappings.append(row)
    result = invoke(
        "test_mapping",
        {
            "site": INPUTS["site"],
            "mappings": candidate(),
            "mappings_json": json.dumps(mappings),
            "sample_ids": [SID],
        },
    )["result"]
    assert {"template_id": "product", "field": "compatibility"} in result["table_obligations"]
    assert not result["passed"]


def test_final_offline_retest_keeps_only_honest_carried_table_warnings(invoke, service):
    service.override("POST", r"^/v1/mappings/test$", 200, report(complete=True))
    warning = {
        "kind": "table_unavailable",
        "message": "A later table source remained unverified; the earlier verified mapping was retained.",
    }
    result = invoke(
        "test_mapping",
        {
            "site": INPUTS["site"],
            "mappings": candidate(),
            "sample_ids": [SID],
            "carried_table_warnings": [
                warning,
                {"kind": "optional_field", "message": "stale ordinary warning"},
            ],
        },
    )["result"]
    assert result["passed"] is True
    assert warning["message"] in result["summary"]
    assert "stale ordinary warning" not in result["summary"]
    assert not service.calls("POST", "/v1/samples")


@pytest.mark.parametrize(
    "reason",
    ["policy_denied", "page_limit", "time_limit", "worker_unsupported", "browser_required"],
)
def test_actual_capture_failures_do_not_spend_paid_author_refinement_budget(
    service, tmp_path, reason
):
    _walk, outcome, state, agent = child_walk(
        service, tmp_path, [report(reason)] * 4, max_refinements=3
    )
    assert outcome == "stop_success" and not state["passed"]
    assert len(agent.calls) == 1 and state["attempts"] == 1
    assert not service.calls("POST", "/v1/samples")
