"""Unknown inclusion values remain actionable even when no rows are selected."""

from fake_service import example

SAMPLE_ID = "smp_01JCX9Q1A2B3C4D5E6F7G8H9J0"


def _mapping():
    mapping = example("mapping.product.json")
    mapping["fields"].append(
        {
            "name": "available",
            "type": "boolean",
            "sources": [{"kind": "meta", "name": "row-enabled"}],
        }
    )
    mapping["variants"] = {
        "sources": [
            {
                "kind": "embedded_json",
                "selector": "script#state",
                "path": "choices",
                "format": "generic",
            }
        ],
        "fields": [
            {
                "name": "available",
                "path": "custom_flag",
                "type": "boolean",
                "fallback_to_page": False,
            }
        ],
    }
    mapping["record_filter"] = [{"field": "available", "op": "eq", "value": True}]
    return mapping


def _response(missing):
    response = example("post_mappings_test.response.json")
    response["templates"] = response["templates"][:1]
    response["samples"] = response["samples"][:1]
    response["passed"] = not missing
    template = response["templates"][0]
    template.update(
        passed=not missing,
        variant_count=0,
        samples_with_variants=1,
        variant_sources_used={"embedded_json": 1},
        variant_fields=[],
        empty_fields=[],
    )
    for field in template["fields"]:
        field.update(
            sample_count=0,
            filled_count=0,
            fill_rate=0.0,
            passed=True,
            example_values=[],
            source_hits=[0] * len(field["source_hits"]),
        )
    sample = response["samples"][0]
    sample["missing_required"] = ["available"] if missing else []
    sample["data"]["available"] = None if missing else False
    sample["variants"] = {
        "count": 3,
        "included_count": 0,
        "excluded_count": 3,
        "source": "embedded_json",
        "source_index": 0,
        "items": [],
    }
    return response


def test_unknown_inclusion_value_produces_sample_field_feedback(invoke, service):
    service.override("POST", r"^/v1/mappings/test$", 200, _response(missing=True))
    result = invoke(
        "test_mapping",
        {
            "site": "lumen-shop.example",
            "mappings": [_mapping()],
            "sample_ids": [SAMPLE_ID],
        },
    )["result"]
    assert result["passed"] is False
    missing = [
        failure
        for failure in result["failures"]
        if failure["kind"] == "missing_required"
    ]
    assert len(missing) == 1
    assert missing[0]["sample_id"] == SAMPLE_ID
    assert missing[0]["template_id"] == "product"
    assert missing[0]["field"] == "available"
    assert "raw." in missing[0]["message"]
    assert SAMPLE_ID in result["summary"] and "available" in result["summary"]
    assert result["sample_results"][0]["missing_required"] == ["available"]
    assert not service.schema_violations


def test_known_false_inclusion_values_are_valid_intentional_exclusions(invoke, service):
    service.override("POST", r"^/v1/mappings/test$", 200, _response(missing=False))
    result = invoke(
        "test_mapping",
        {
            "site": "lumen-shop.example",
            "mappings": [_mapping()],
            "sample_ids": [SAMPLE_ID],
        },
    )["result"]
    assert result["passed"] is True
    assert result["failures"] == []
    assert result["sample_results"][0]["missing_required"] == []
    assert not service.schema_violations
