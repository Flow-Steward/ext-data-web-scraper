"""Optional application-state evidence reaches the mapping author intact."""

from fake_service import schema_errors

from handlers.mappings import (
    _agent_samples,
    structural_problems,
    variant_sample_ids,
    variant_warnings,
)
from handlers.samples_file import render_samples


def test_optional_candidates_reach_agent_without_requiring_variants():
    candidate = {
        "source": {
            "kind": "embedded_json",
            "selector": "script#state",
            "path": "state.choices",
            "format": "generic",
        },
        "count": 2,
        "raw_examples": [
            {"code": "a", "enabled": True},
            {"code": "b", "enabled": False},
        ],
        "raw_paths": ["code", "enabled"],
        "evidence": "Array of objects; inspect whether these belong to this item",
    }
    samples = [
        {
            "sample_id": "s1",
            "url": "https://example.test/item",
            "variant_candidates": [candidate],
        }
    ]
    projected = _agent_samples(samples)
    assert projected[0]["variant_candidates"] == [candidate]
    assert variant_sample_ids(projected) == []
    text, _ = render_samples("example.test", projected)
    assert "state.choices" in text
    assert "enabled" in text
    assert "inspect" in text.lower()


def test_extension_contract_accepts_generic_filter_and_dom_variant_lookup():
    mapping = {
        "schema_version": 1,
        "template_id": "entry",
        "match": {"selector": "h1"},
        "fields": [
            {
                "name": "allowed",
                "type": "boolean",
                "sources": [{"kind": "meta", "name": "allowed"}],
            }
        ],
        "record_filter": [{"field": "allowed", "op": "eq", "value": True}],
        "variants": {
            "sources": [{"kind": "embedded_json", "selector": "script#state"}],
            "fields": [
                {
                    "name": "description",
                    "selector_template": '.detail[data-choice="{id}"]',
                    "extract": "html",
                    "type": "html",
                    "fallback_to_page": False,
                },
            ],
        },
    }
    assert schema_errors("mapping.schema.json", mapping) == []
    assert structural_problems([mapping]) == []


def test_all_excluded_variants_are_not_reported_as_missing_sources_or_fields():
    templates = [
        {
            "template_id": "entry",
            "matched_samples": 1,
            "variant_count": 0,
            "samples_with_variants": 1,
            "variant_sources_used": {"embedded_json": 1},
            "variant_fields": [{"name": "description", "fill_rate": 0.0, "filled": "0/0"}],
        }
    ]
    assert variant_warnings(templates, 0.9) == []


def test_nested_candidate_parent_context_reaches_the_mapping_author():
    candidate = {
        "source": {
            "kind": "embedded_json",
            "selector": "script#state",
            "path": "editions",
            "children_path": "packages",
            "format": "generic",
        },
        "count": 3,
        "parent_count": 2,
        "raw_examples": [{"sku": "001", "size": "small"}],
        "raw_paths": ["sku", "size"],
        "parent_examples": [{"year": "2023", "notes": "Earlier edition"}],
        "context_paths": ["raw.sku", "parent.raw.year", "parent.raw.notes"],
        "evidence": "Child packages with their corresponding edition context",
    }
    projected = _agent_samples(
        [{"sample_id": "s1", "url": "https://example.test/item", "variant_candidates": [candidate]}]
    )
    assert projected[0]["variant_candidates"] == [candidate]
    text, _ = render_samples("example.test", projected)
    assert "parent.raw.year" in text
    assert "Earlier edition" in text
    assert variant_sample_ids(projected) == []


def test_extension_contract_accepts_nested_context_source():
    mapping = {
        "schema_version": 1,
        "template_id": "entry",
        "match": {"selector": "h1"},
        "fields": [
            {
                "name": "title",
                "type": "text",
                "sources": [{"kind": "css", "selector": "h1", "extract": "text"}],
            }
        ],
        "variants": {
            "sources": [
                {
                    "kind": "embedded_json",
                    "selector": "script#state",
                    "path": "editions",
                    "children_path": "packages",
                }
            ],
            "fields": [
                {"name": "year", "path": "parent.raw.year", "fallback_to_page": False},
                {"name": "sku", "path": "raw.sku", "fallback_to_page": False},
            ],
        },
    }
    assert schema_errors("mapping.schema.json", mapping) == []
    assert structural_problems([mapping]) == []
