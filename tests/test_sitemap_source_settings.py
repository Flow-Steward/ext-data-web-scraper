"""Optional sitemap source scope travels from both workflow forms to validation."""

import copy
import json
from pathlib import Path

import pytest

from fake_service import example, schema_errors
from test_0_1_23 import SCRAPES
from test_templates import IMPL, SCRAPE_INPUTS, _action, _connector
from workflow_sim import Walk

from handlers.profile import Profile
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


@pytest.mark.parametrize("value", [None, "", "  ", [], {}, "\n\t"])
def test_empty_sitemap_settings_are_omitted_from_outbound_layers(value):
    layer = {key: value for key in FIELDS}
    # Existing fields keep their unset semantics; only the new optional keys go.
    layer["max_pages"] = None
    assert run_inputs_layer(layer, []) == {"max_pages": None}
    assert site_settings_layer(layer) == {"max_pages": None}
    profile = Profile("connection", "profile", "https://generic.test", "token", None, "", "",
                      defaults=layer)
    assert profile.contract_profile()["defaults"] == {"max_pages": None}


@pytest.mark.parametrize("key", ["sitemap_source_include", "sitemap_source_exclude"])
@pytest.mark.parametrize("value", ["[]", [" ", "\t"]])
def test_empty_source_arrays_are_omitted_after_canvas_coercion(key, value):
    assert run_inputs_layer({key: value}, []) == {}
    assert site_settings_layer({key: value}) == {}


def test_explicit_sitemap_profile_defaults_are_preserved():
    profile = Profile("connection", "profile", "https://generic.test", "token", None, "", "",
                      defaults=EXPECTED)
    assert profile.contract_profile()["defaults"] == EXPECTED


@pytest.fixture
def older_sitemap_service(service, monkeypatch):
    """A strict service contract from before sitemap scope settings existed."""
    from referencing import Resource
    import fake_service

    schema = json.loads((Path(__file__).parents[1] /
                         "service_contract/schemas/job_config.schema.json").read_text())
    for key in FIELDS:
        schema["properties"].pop(key)
        schema["$defs"]["job_settings"]["properties"].pop(key)
    limits = schema["$defs"]["service_limits"]
    limits["properties"].pop("max_sitemap_entries")
    limits["required"].remove("max_sitemap_entries")
    registry = fake_service.REGISTRY.with_resource(schema["$id"], Resource.from_contents(schema))
    monkeypatch.setattr(fake_service, "REGISTRY", registry)
    health = example("get_health.response.json")
    health["limits"].pop("max_sitemap_entries")
    service.override("GET", r"^/health$", 200, health)
    return service


@pytest.mark.parametrize("variant", SCRAPES)
def test_blank_sitemap_workflow_settings_work_with_older_service(
    variant, older_sitemap_service, tmp_path
):
    service = older_sitemap_service
    walk = Walk(copy.deepcopy(SCRAPES[variant]), connector=_connector(service, tmp_path),
                invoke=_action(service, tmp_path, []), child=IMPL)
    outcome, _state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_success", walk.failures
    body = service.calls("POST", "/v1/config/validate")[0]["body"]
    for layer in [body["run_inputs"], body["site_settings"], body["profile"]["defaults"]]:
        assert not set(FIELDS).intersection(layer)
    assert not service.schema_violations


@pytest.mark.parametrize("key", FIELDS)
def test_explicit_sitemap_setting_is_not_silently_dropped_for_older_service(
    key, invoke, older_sitemap_service
):
    response = invoke("validate_config", {"site_url": "https://generic.test/", key: FIELDS[key]})
    assert not response["ok"]
    assert response["error_code"] == "config_invalid"
    assert key in response["error"]
    assert "refused" in response["error"]
    body = older_sitemap_service.calls("POST", "/v1/config/validate")[0]["body"]
    assert body["run_inputs"][key] == EXPECTED[key]
    assert not older_sitemap_service.calls("POST", "/v1/runs")


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
