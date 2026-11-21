"""The author receives the shipped scalar variant contract, even with large samples."""

import json
import re
from pathlib import Path

from handlers.samples_file import MAX_SAMPLES, SAMPLES_FILE_MAX_CHARS, render_samples

SCHEMA = (
    Path(__file__).resolve().parents[1] / "service_contract/schemas/mapping.schema.json"
)


def _attached_schema(text):
    expected_id = json.loads(SCHEMA.read_text())["$id"]
    for match in re.finditer(r"(?ms)^(`{3,})json\n(.*?)\n\1$", text):
        try:
            value = json.loads(match.group(2))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("$id") == expected_id:
            return value
    raise AssertionError(
        "the actual shipped mapping.schema.json must be attached whole"
    )


def test_samples_attach_the_actual_shipped_mapping_schema():
    text, _ = render_samples(
        "example.test",
        [
            {
                "sample_id": "smp_one",
                "url": "https://example.test/item",
                "distilled_html": "<h1>Item</h1>",
            }
        ],
    )
    attached = _attached_schema(text)
    assert attached == json.loads(SCHEMA.read_text())
    variant_field = attached["$defs"]["variant_field"]
    assert variant_field["additionalProperties"] is False
    assert (
        "multiple" not in variant_field["properties"]
        and "required" not in variant_field["properties"]
    )
    assert {"multiple", "required"} <= attached["$defs"]["field"]["properties"].keys()
    assert text.index(attached["$id"]) < text.index("## Sample 1")


def test_schema_is_whole_with_maximum_samples_and_large_refinement_feedback():
    rows = [
        {
            "sample_id": f"smp_{index}",
            "url": f"https://example.test/item/{index}",
            "distilled_html": "<section>" + "h" * 90000 + "</section>",
            "jsonld": [{"@type": "Product", "description": "j" * 40000}],
            "meta": {"description": "m" * 9000},
            "variant_sources": [
                {
                    "source": {"kind": "embedded_json", "selector": "script#state"},
                    "count": 2,
                    "example": {"description": "v" * 9000},
                }
            ],
            "variant_candidates": [
                {
                    "source": {
                        "kind": "embedded_json",
                        "selector": "script#state",
                        "path": "choices",
                    },
                    "count": 2,
                    "raw_examples": [{"description": "c" * 9000}],
                }
            ],
            "microdata": [{"@type": "Product", "description": "d" * 9000}],
        }
        for index in range(MAX_SAMPLES)
    ]
    task = {
        "previous_mappings": [{"notes": "p" * 90000}],
        "failures": [{"message": "f" * 90000}],
        "service_failures": {"message": "s" * 90000},
    }
    text, report = render_samples("example.test", rows, task)
    assert _attached_schema(text) == json.loads(SCHEMA.read_text())
    assert len(text) <= SAMPLES_FILE_MAX_CHARS
    assert len(report) == MAX_SAMPLES
    assert all(row["html_shortened"] for row in report)
