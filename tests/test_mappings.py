"""Samples, mapping tests, versions, export/import, mapping requests."""

from __future__ import annotations

import copy
import json

import pytest

from fake_service import example

SITE = "lumen-shop.example"


def _mapping(name: str = "mapping.product.json") -> dict:
    return example(name)


def _config(invoke) -> dict:
    return invoke(
        "validate_config", {"site_url": "https://www.lumen-shop.example/", "site_settings": {}}
    )["result"]["config"]


def test_fetch_samples_returns_agent_ready_pages(invoke, service):
    response = invoke("fetch_samples", {"config": _config(invoke)})
    assert response["ok"] is True, response
    result = response["result"]
    assert result["sample_count"] == len(result["sample_ids"]) == len(result["samples"])
    sample = result["samples"][0]
    assert {"sample_id", "url", "distilled_html", "jsonld", "meta", "template_hint"} <= set(sample)
    assert "Product" in result["jsonld_types"]
    assert "sample_count" in result["warning_text"]
    assert "webhook" not in service.calls("POST", "/v1/samples")[0]["body"]["profile"]
    assert not service.schema_violations


def test_no_samples_is_explained(invoke, service):
    empty = example("post_samples.response.json")
    empty["samples"] = []
    service.override("POST", r"^/v1/samples$", 200, empty)
    response = invoke("fetch_samples", {"config": _config(invoke)})
    assert response["error_code"] == "no_samples"
    assert "Analyze site" in response["error"]


def test_passing_mapping_with_field_table(invoke, service):
    response = invoke(
        "test_mapping",
        {"site": SITE, "mappings": [_mapping()], "sample_ids": ["smp_01JCX9Q1A2B3C4D5E6F7G8H9J0"]},
    )
    assert response["ok"] is True, response
    result = response["result"]
    assert result["passed"] is True
    assert result["failures"] == []
    assert result["attempt"] == 1 and result["can_refine"] is True
    assert any(row["name"] == "price" and row["passed"] for row in result["field_results"])
    assert result["summary"].startswith("Passed.")
    assert not service.schema_violations


def test_failing_mapping_lists_failures_and_counts_attempts(invoke, service):
    failing = example("post_mappings_test.response.json")
    failing["passed"] = False
    template = failing["templates"][0]
    template["passed"] = False
    price = next(f for f in template["fields"] if f["name"] == "price")
    price.update({"passed": False, "filled_count": 0, "fill_rate": 0.0, "example_values": []})
    service.override("POST", r"^/v1/mappings/test$", 200, failing)
    response = invoke(
        "test_mapping",
        {
            "site": SITE,
            "mappings": [_mapping()],
            "sample_ids": ["smp_1"],
            "attempt": 2,
            "max_refinements": 2,
        },
    )
    result = response["result"]
    assert result["passed"] is False
    assert result["attempt"] == 3 and result["can_refine"] is False
    assert any(f["kind"] == "fill" and f["field"] == "price" for f in result["failures"])


def test_structural_problems_never_reach_the_service(invoke, service):
    broken = _mapping()
    broken["fields"][0]["sources"] = []
    del broken["match"]
    response = invoke("test_mapping", {"site": SITE, "mappings": [broken], "sample_ids": ["smp_1"]})
    result = response["result"]
    assert response["ok"] is True and result["passed"] is False
    messages = " ".join(f["message"] for f in result["failures"])
    assert (
        "match needs url_patterns or a selector" in messages
        and "between 1 and 5 sources" in messages
    )
    assert not service.calls("POST", "/v1/mappings/test")


def test_schema_refusal_becomes_failures_for_a_refinement(invoke, service):
    candidate = _mapping()
    candidate["fields"][0]["sources"][0]["kind"] = "xpath"
    response = invoke(
        "test_mapping", {"site": SITE, "mappings": candidate, "sample_ids": ["smp_1"]}
    )
    # The local check already catches the unknown kind; with the service:
    assert response["result"]["passed"] is False
    candidate = _mapping()
    candidate["fields"][0]["type"] = "money"
    response = invoke(
        "test_mapping", {"site": SITE, "mappings": [candidate], "sample_ids": ["smp_1"]}
    )
    result = response["result"]
    assert response["ok"] is True and result["passed"] is False
    assert result["failures"][0]["kind"] == "schema"
    assert service.schema_violations  # the fake refused it exactly as the contract says


def test_expired_samples(invoke, service):
    service.override(
        "POST",
        r"^/v1/mappings/test$",
        404,
        {
            "error": {
                "code": "sample_not_found",
                "message": "expired",
                "request_id": "req_2",
                "details": {"sample_ids": ["smp_1"]},
            }
        },
    )
    response = invoke(
        "test_mapping", {"site": SITE, "mappings": [_mapping()], "sample_ids": ["smp_1"]}
    )
    assert response["error_code"] == "sample_not_found"
    assert "Fetch samples again" in response["error"]


def test_get_mappings_and_activate_with_expected_version(invoke, service):
    current = invoke("get_mappings", {"site": SITE})["result"]
    assert current["has_active_mapping"] is True
    assert current["expected_versions"] == {"product": 3, "spare_part": 2}
    changed = copy.deepcopy(current["mappings"][0])
    changed["label"] = "Lamp product page (reviewed)"
    response = invoke(
        "activate_mapping",
        {
            "site": SITE,
            "mappings": [changed],
            "expected_versions": current["expected_versions"],
            "note": "review",
        },
    )
    assert response["ok"] is True, response
    result = response["result"]
    assert result["all_activated"] is True
    assert result["activated"][0]["version"] == 4 and result["activated"][0]["changed"] is True
    body = service.calls("PUT", f"/v1/sites/{SITE}/mappings")[0]["body"]
    assert body["origin"]["platform_job_id"] and body["origin"]["note"] == "review"
    # Activating the same content again is a no-op.
    again = invoke(
        "activate_mapping",
        {"site": SITE, "mappings": [changed], "expected_versions": {"product": 4}},
    )
    assert again["result"]["activated"][0]["changed"] is False
    assert not service.schema_violations


def test_version_conflict_is_reported_or_fails_on_request(invoke, service):
    stale = copy.deepcopy(invoke("get_mappings", {"site": SITE})["result"]["mappings"][0])
    stale["label"] = "built on version 2"
    response = invoke(
        "activate_mapping", {"site": SITE, "mappings": [stale], "expected_version": 2}
    )
    assert response["ok"] is True
    assert response["result"]["conflict"] is True
    assert response["result"]["conflicts"][0]["active_version"] == 3
    response = invoke(
        "activate_mapping",
        {"site": SITE, "mappings": [stale], "expected_version": 2, "fail_on_conflict": True},
    )
    assert response["error_code"] == "mapping_version_conflict"
    assert "Nothing was overwritten" in response["error"]


def test_new_template_and_invalid_mapping(invoke, service):
    new = _mapping()
    new["template_id"] = "accessory"
    response = invoke("activate_mapping", {"site": SITE, "mappings": [new]})
    assert response["result"]["activated"][0]["version"] == 1
    assert (
        invoke("activate_mapping", {"site": SITE, "mappings": []})["error_code"]
        == "mapping_invalid"
    )


def test_export_and_import_round_trip(invoke, service):
    export = invoke("export_mappings")
    assert export["ok"] is True, export
    assert export["result"]["template_count"] >= 2
    service.mappings.clear()
    imported = invoke("import_mappings", {"export": export["result"]["export"]})
    assert imported["ok"] is True, imported
    assert imported["result"]["activated"] == export["result"]["template_count"]
    again = invoke("import_mappings", {"export": export["result"]["export"]})
    assert again["result"]["unchanged"] == export["result"]["template_count"]
    assert (
        invoke("import_mappings", {"export": {"format": "other"}})["error_code"]
        == "invalid_payload"
    )
    assert not service.schema_violations


def test_mapping_request_and_failure_report(invoke, service):
    request_id = "mrq_01JCXB1G0Z9Y8X7W6V5T4S3R2Q"
    response = invoke("get_mapping_request", {"mapping_request_id": request_id})
    assert response["ok"] is True, response
    result = response["result"]
    assert result["is_open"] is True and result["reason"] == "incomplete"
    assert result["expected_versions"] == {"spare_part": 1}
    assert result["previous_mappings"] and result["sample_ids"]
    failed = invoke(
        "fail_mapping_request",
        {"mapping_request_id": request_id, "failure_reason": "price never filled", "attempts": 3},
    )
    assert failed["ok"] is True and failed["result"]["already_closed"] is False
    body = service.calls("POST", f"/v1/mapping-requests/{request_id}/fail")[0]["body"]
    assert body["attempts"] == 3 and body["platform_job_id"]
    service.closed_requests[request_id] = "expired"
    closed = invoke("fail_mapping_request", {"mapping_request_id": request_id})
    assert closed["ok"] is True and closed["result"]["already_closed"] is True
    assert closed["result"]["status"] == "expired"
    assert not service.schema_violations


def test_max_refinements_is_clamped_to_the_rework_limit(invoke, service):
    """m9: more than 5 refinements would hit rework_limit and pause for a person."""
    failing = example("post_mappings_test.response.json")
    failing["passed"] = False
    service.override("POST", r"^/v1/mappings/test$", 200, failing)
    response = invoke(
        "test_mapping",
        {
            "site": SITE,
            "mappings": [_mapping()],
            "sample_ids": ["smp_1"],
            "attempt": 5,
            "max_refinements": 9,
        },
    )
    result = response["result"]
    assert result["max_refinements"] == 5
    assert result["attempt"] == 6 and result["can_refine"] is False
    response = invoke(
        "test_mapping",
        {"site": SITE, "mappings": [_mapping()], "sample_ids": ["smp_1"], "max_refinements": -3},
    )
    assert response["result"]["max_refinements"] == 0


def test_agent_samples_share_one_size_budget():
    """m10: 20 samples of 64 KiB each would put about 1.3 MB into one prompt and into state."""
    from handlers.mappings import SAMPLE_HTML_FOR_AGENT, SAMPLES_HTML_FOR_AGENT, _agent_samples

    rows = [
        {"sample_id": f"smp_{i}", "url": f"https://x.example/{i}", "distilled_html": "y" * 70000}
        for i in range(20)
    ]
    samples = _agent_samples(rows)
    total = sum(len(s["distilled_html"]) for s in samples)
    assert total <= SAMPLES_HTML_FOR_AGENT
    assert all(s["distilled_html_truncated"] for s in samples)
    few = _agent_samples(rows[:2])
    assert [len(s["distilled_html"]) for s in few] == [SAMPLE_HTML_FOR_AGENT] * 2


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "no mapping"),
        ("   ", "no mapping"),
        ("[{'schema_version': 1}]", "not valid JSON"),
        ('{"schema_version": 1', "not valid JSON"),
        ('"just a string"', "must be a JSON list"),
        ("42", "must be a JSON list"),
        ('[{"schema_version": 1}, "x"]', "must be a mapping object"),
    ],
)
def test_mapping_text_that_is_not_a_list_is_a_clear_failure(invoke, service, text, message):
    """The agent's mappings_json is text; a bad one becomes a failure for the next refinement."""
    response = invoke(
        "test_mapping", {"site": SITE, "mappings_json": text, "sample_ids": ["smp_1"]}
    )
    assert response["ok"] is True, response
    result = response["result"]
    assert result["passed"] is False and result["failures"][0]["kind"] == "format"
    assert message in result["failures"][0]["message"]
    assert not service.calls("POST", "/v1/mappings/test")


def test_mapping_text_in_a_fence_or_as_one_object_is_read(invoke, service):
    mapping = _mapping()
    for text in (
        "```json\n" + json.dumps([mapping]) + "\n```",
        json.dumps(mapping),
        json.dumps({"mappings": [mapping]}),
    ):
        response = invoke(
            "test_mapping", {"site": SITE, "mappings_json": text, "sample_ids": ["smp_1"]}
        )
        assert response["result"]["mappings"] == [mapping], text
    sent = service.calls("POST", "/v1/mappings/test")
    assert len(sent) == 3 and all(call["body"]["mappings"] == [mapping] for call in sent)
