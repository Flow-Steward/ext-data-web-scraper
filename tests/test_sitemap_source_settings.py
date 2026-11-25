"""Optional sitemap source scope travels from both workflow forms to validation."""

import copy

import pytest

from fake_service import schema_errors
from test_0_1_23 import SCRAPES
from test_templates import IMPL, SCRAPE_INPUTS, _action, _connector
from workflow_sim import Walk

from handlers.settings import coerce_setting, run_inputs_layer, site_settings_layer

FIELDS = {
    "max_sitemap_entries": "250000",
    "sitemap_source_include": '["/catalog/index", "/category-[0-9]{1,3}\\\\.xml"]',
    "sitemap_source_exclude": ["/archived/", "/other/"],
}
EXPECTED = {
    "max_sitemap_entries": 250000,
    "sitemap_source_include": ["/catalog/index", r"/category-[0-9]{1,3}\.xml"],
    "sitemap_source_exclude": ["/archived/", "/other/"],
}


def test_sitemap_settings_parse_run_and_legacy_layers():
    assert run_inputs_layer(FIELDS, []) == EXPECTED
    assert site_settings_layer(FIELDS) == EXPECTED
    assert coerce_setting("sitemap_source_include", "/catalog/\n/category/") == [
        "/catalog/", "/category/"
    ]


@pytest.mark.parametrize("value", [None, "", [], {}])
def test_empty_sitemap_settings_keep_profile_defaults(value):
    assert run_inputs_layer({key: value for key in FIELDS}, []) == {
        key: value for key in FIELDS
    }


@pytest.mark.parametrize("value", [True, "1.5", "invalid"])
def test_noninteger_entry_budget_stays_invalid_for_service(value):
    assert coerce_setting("max_sitemap_entries", value) == value
    assert schema_errors("job_config.schema.json#/$defs/job_settings", {
        "max_sitemap_entries": coerce_setting("max_sitemap_entries", value)
    })


@pytest.mark.parametrize("value", [0, 1000001])
def test_contract_bounds_entry_budget(value):
    assert schema_errors("job_config.schema.json#/$defs/job_settings", {
        "max_sitemap_entries": value
    })


def test_validation_passes_optional_sitemap_settings_to_service(invoke, service):
    response = invoke("validate_config", {"site_url": "https://generic.test/", **FIELDS})
    assert response["ok"], response
    body = service.calls("POST", "/v1/config/validate")[0]["body"]
    assert {key: body["run_inputs"][key] for key in FIELDS} == EXPECTED
    assert {key: response["result"]["config"][key] for key in FIELDS} == EXPECTED
    assert not service.schema_violations


@pytest.mark.parametrize("variant", SCRAPES)
def test_workflow_sitemap_inputs_reach_run_config(variant, service, tmp_path):
    workflow = copy.deepcopy(SCRAPES[variant])
    form = {field["name"]: field for field in workflow["settings"]["callable"]["expected_inputs"]}
    for key in FIELDS:
        assert key in form
        assert form[key]["required"] is False
        assert "default" not in form[key]
    walk = Walk(workflow, connector=_connector(service, tmp_path),
                invoke=_action(service, tmp_path, []), child=IMPL)
    outcome, state = walk.run({**SCRAPE_INPUTS, **FIELDS})
    assert outcome == "stop_success", walk.failures
    assert {key: state["config"][key] for key in FIELDS} == EXPECTED
    started = service.calls("POST", "/v1/runs")[0]["body"]["config"]
    assert {key: started[key] for key in FIELDS} == EXPECTED
    body = service.calls("POST", "/v1/config/validate")[0]["body"]
    assert {key: body["run_inputs"][key] for key in FIELDS} == EXPECTED
    assert not service.schema_violations


def test_declared_source_scope_run_response_matches_contract():
    from fake_service import example

    run = example("get_run.response.json")
    run["sitemap_discovery"] = {
        "complete": True,
        "capped": False,
        "cap_reason": None,
        "entries_seen": 20,
        "source_files": 2,
        "failed_sources": [],
        "pending_sources": [],
        "refused_sources": [],
        "scope": "configured_sources",
        "excluded_source_count": 1,
        "excluded_sources": ["https://generic.test/other.xml"],
    }
    assert not schema_errors("run_summary.schema.json", run)
