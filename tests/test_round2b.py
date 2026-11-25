"""ROUND2 phase B (service 46c7b49..7f3560b): crawl scope, AI-chosen fields, spec tables,
why a run stopped, completeness and comparison."""

from __future__ import annotations

import copy
import json

from conftest import ROOT_SCOPE, make_payload, samples_descriptor
from fake_service import example, schema_errors
from test_templates import EVENT_INPUTS, EVENTS, IMPL, SCRAPE, SCRAPE_INPUTS, _connector
from workflow_sim import StepFailed, Walk

from handlers.mappings import structural_problems
from handlers.router import handle_request

RUN = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"


def _run(**changes) -> dict:
    run = example("get_run.response.json")
    run.update(changes)
    assert not schema_errors("run_summary.schema.json", run)
    return run


EXPLANATION = {
    "sentence": "The run stopped because the site stopped answering: 12 server errors in a row "
    "on www.lumen-shop.example after pauses of up to 5 minutes. 40 of 52 target pages were "
    "collected.",
    "advice": "Run it again later, with fewer pages at once.",
}
COMPLETENESS = {
    "target_pages_found": 52,
    "pages_collected": 40,
    "records": 46,
    "not_collected": 12,
    "not_collected_by_reason": {"fetch_failed": 12},
    "sentence": "52 target pages found, 40 collected (46 records with variants), 12 not collected (12 fetch failed).",
}
COMPARISON = {
    "run_id": "run_01JCX00000000000000000000A",
    "finished_at": "2026-11-12T10:00:00Z",
    "target_pages_found": 60,
    "pages_collected": 60,
    "records": 70,
    "not_collected": 0,
    "change": {"records": -24},
    "warning": "24 fewer records than the run of 2026-11-12: found pages were not collected "
    "(12 fetch failed), the site did not shrink.",
}
TIMELINE = [
    {"at": "2026-11-14T09:00:00Z", "kind": "started", "message": "started"},
    {
        "at": "2026-11-14T09:01:00Z",
        "kind": "discovery_decided",
        "message": "discovery: sitemap and crawl",
    },
    {"at": "2026-11-14T09:40:00Z", "kind": "stop", "message": "stopped: site unavailable"},
]


# ------------------------------------------------------------- the run form


def test_scope_and_ai_fields_follow_the_basics_and_travel_typed(invoke, service):
    names = [row["name"] for row in SCRAPE["settings"]["callable"]["expected_inputs"]]
    debug = names.index("debug")
    assert names[debug + 1 : debug + 5] == [
        "crawl_include",
        "stay_in_section",
        "ai_choose_fields",
        "fields_hint",
    ]
    rows = {row["name"]: row for row in SCRAPE["settings"]["callable"]["expected_inputs"]}
    assert rows["crawl_include"]["label"] == "Where to walk (regex)"
    assert rows["stay_in_section"]["label"] == "Stay inside the start page's section"
    response = invoke(
        "validate_config",
        {
            "site_url": "https://www.lumen-shop.example/leuchten/",
            "crawl_include": "/leuchten/\n/lampen/",
            "stay_in_section": "true",
            "ai_choose_fields": "yes",
            "fields_hint": " wine: vintage, type ",
        },
    )
    assert response["ok"], response
    layer = service.calls("POST", "/v1/config/validate")[0]["body"]["run_inputs"]
    assert layer["crawl_include"] == ["/leuchten/", "/lampen/"]
    assert layer["stay_in_section"] is True and layer["ai_choose_fields"] is True
    assert layer["fields_hint"] == "wine: vintage, type"
    assert response["result"]["ai_choose_fields"] is True
    assert response["result"]["fields_hint"] == "wine: vintage, type"
    assert not service.schema_violations


def test_ai_chosen_fields_reach_the_agent(service, tmp_path):
    seen: dict = {}

    def agent(_step, inputs):
        seen.update(inputs)
        return {"mappings_json": json.dumps([example("mapping.product.json")]), "notes": "x"}

    def child(_step, inputs):
        walk = Walk(IMPL, connector=_connector(service, tmp_path), agent=agent)
        outcome, state = walk.run(inputs, raw_inputs=True)
        if outcome == "failed":
            raise StepFailed("; ".join(walk.failures))
        return walk.outputs

    service.mappings.clear()
    walk = Walk(SCRAPE, connector=_connector(service, tmp_path), invoke=child, child=IMPL)
    outcome, _state = walk.run(
        {**SCRAPE_INPUTS, "ai_choose_fields": True, "fields_hint": "lamps: wattage"}
    )
    assert outcome == "stop_success", walk.failures
    assert seen["ai_choose_fields"] is True and seen["fields_hint"] == "lamps: wattage"


def test_the_samples_file_tells_the_agent_to_choose_fields(service, tmp_path):
    descriptor = samples_descriptor(tmp_path)
    payload = make_payload(
        "write_samples_file",
        {
            "site": "x.example",
            "samples": example("post_samples.response.json")["samples"],
            "ai_choose_fields": True,
            "fields_hint": "wine: vintage",
        },
        service_url=service.url,
        artifacts={"inputs": [], "outputs": [descriptor]},
    )
    assert handle_request(payload)["ok"]
    text = open(descriptor["_path"], encoding="utf-8").read()
    assert (
        "Let the AI choose the fields: yes." in text
        and "Hint from the operator: wine: vintage" in text
    )


# --------------------------------------------------- spec tables and images


def test_spec_table_sources_are_checked_and_values_are_objects(invoke, service):
    mapping = copy.deepcopy(example("mapping.product.json"))
    mapping["fields"].append(
        {
            "name": "specifications",
            "type": "spec_table",
            "sources": [
                {
                    "kind": "spec_table",
                    "row_selector": "table.specs tr",
                    "key_selector": "th",
                    "value_selector": "td",
                }
            ],
        }
    )
    assert not schema_errors("mapping.schema.json", mapping)
    assert structural_problems([mapping]) == []
    broken = copy.deepcopy(mapping)
    broken["fields"][-1]["sources"] = [{"kind": "spec_table", "row_selector": "tr"}]
    assert any("spec_table source needs" in p for p in structural_problems([broken]))
    record = example("record.json")
    record["data"]["specifications"] = {"Vintage": "2019", "Bottle size": "0.75 l"}
    assert not schema_errors("record.schema.json", record)
    answer = example("post_mappings_test.response.json")
    answer["templates"][0]["fields"][0]["example_values"] = [{"Vintage": "2019"}]
    service.override("POST", r"^/v1/mappings/test$", 200, answer)
    result = invoke(
        "test_mapping",
        {
            "site": "lumen-shop.example",
            "mappings_json": json.dumps([mapping]),
            "sample_ids": ["smp_01JCX9Q1A2B3C4D5E6F7G8H9J0"],
        },
    )["result"]
    assert result["field_results"][0]["examples"] == ['{"Vintage": "2019"}']


# ------------------------------------------------- why it stopped, completeness


def test_run_detail_says_why_it_stopped_how_complete_and_against_last_time(invoke, service):
    run = _run(
        state="completed",
        outcome="partial",
        stop_reason="site_unavailable",
        explanation=EXPLANATION,
        completeness=COMPLETENESS,
        comparison=COMPARISON,
        timeline=TIMELINE,
    )
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    result = invoke("show_run", {"run_id": RUN})["result"]
    assert result["outcome"].startswith("partial (stop reason: site_unavailable). The run stopped")
    assert "What to do: Run it again later, with fewer pages at once." in result["outcome"]
    assert result["records"].startswith(COMPLETENESS["sentence"] + " " + COMPARISON["warning"])
    assert (
        "Timeline: 09:00 started; 09:01 discovery: sitemap and crawl; 09:40 stopped"
        in result["progress"]
    )
    plain = [k for k, v in result.items() if not isinstance(v, (dict, list))]
    assert len(plain) <= 12
    assert result["details"]["comparison"]["change"] == {"records": -24}


def test_progress_text_while_running(invoke, service):
    completeness = {
        **COMPLETENESS,
        "progress": {
            "collected": 40,
            "expected": 52,
            "estimate": True,
            "text": "about 40 of 52 collected",
        },
    }
    run = _run(state="running", outcome=None, stop_reason=None, completeness=completeness)
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    assert invoke("show_run", {"run_id": RUN})["result"]["records"].startswith(
        "about 40 of 52 collected"
    )


def test_get_run_summary_carries_the_texts(invoke, service):
    run = _run(
        state="completed",
        outcome="partial",
        stop_reason="site_unavailable",
        explanation=EXPLANATION,
        completeness=COMPLETENESS,
        comparison=COMPARISON,
        timeline=TIMELINE,
    )
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    result = invoke("get_run_summary", {"run_id": RUN})["result"]
    assert result["explanation"].startswith("The run stopped because the site stopped answering")
    assert result["comparison_warning"] == COMPARISON["warning"]
    assert result["completeness"] == COMPLETENESS["sentence"] + " " + COMPARISON["warning"]
    assert result["timeline"].endswith("09:40 stopped: site unavailable")
    assert result["message"].startswith(
        "Run is completed, outcome partial, stop reason site_unavailable. The run stopped"
    )


def test_a_site_unavailable_problem_takes_the_problem_route(service, tmp_path):
    service.event_bodies[EVENT_INPUTS["event_id"]] = {
        "event_type": "problem",
        "outcome": None,
        "stop_reason": None,
        "problem_code": "site_unavailable",
    }
    service.override(
        "GET",
        rf"^/v1/runs/{RUN}$",
        200,
        _run(
            state="completed",
            outcome="partial",
            stop_reason="site_unavailable",
            explanation=EXPLANATION,
        ),
    )
    walk = Walk(
        EVENTS, connector=_connector(service, tmp_path, {**ROOT_SCOPE, "step_id": "claim_event"})
    )
    outcome, state = walk.run({**EVENT_INPUTS, "event_type": "problem"})
    assert outcome == "stop_success" and "problem_summary" in walk.trace, walk.failures
    assert state["problem"]["explanation"].startswith("The run stopped because the site")
