"""AI-1: the mapping agent gets the samples as one whole text file, not as a prompt excerpt."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from conftest import make_payload, samples_descriptor
from page_fixtures import samples

from handlers.samples_file import (
    JSONLD_MAX_CHARS,
    SAMPLES_FILE_MAX_CHARS,
    html_budget,
    render_samples,
)

ROOT = Path(__file__).resolve().parents[1]
IMPL = yaml.safe_load((ROOT / "artifacts/workflows/impl_build_mapping.yaml").read_text())
SKILL = yaml.safe_load((ROOT / "artifacts/skills/build_extraction_mapping.yaml").read_text())
#: agent_file_runtime.py:27 on flow-steward/main: a text file with these is refused.
PLATFORM_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def test_every_sample_is_in_the_file_whole():
    rows = samples()
    text, report = render_samples("books.toscrape.com", rows, {"extraction_instructions": "Books"})
    assert "truncated" not in text
    for row in rows:
        assert row["url"] in text and row["title"] in text and row["sample_id"] in text
        assert json.dumps(row["template_hint"], ensure_ascii=False) in text
        assert row["distilled_html"] in text  # complete, not an excerpt
        for value in row["meta"].values():
            assert value in text
        for node in row["jsonld"]:
            assert json.dumps(node, ensure_ascii=False, indent=1).splitlines()[1] in text
    assert [r["html_shortened"] for r in report] == [False] * 4
    assert not PLATFORM_CONTROL.search(text)


def test_the_budget_holds_for_twenty_large_samples():
    rows = [
        {
            "sample_id": f"smp_{i}",
            "url": f"https://x.example/p/{i}",
            "distilled_html": "<div>" + "y" * 90000 + "</div>",
            "jsonld": [{"@type": "Product", "description": "d" * 40000}],
            "meta": {"k": "v" * 9000},
        }
        for i in range(20)
    ]
    text, report = render_samples("x.example", rows)
    assert len(text) <= SAMPLES_FILE_MAX_CHARS
    assert all(r["html_chars"] == html_budget(20) and r["html_shortened"] for r in report)
    assert html_budget(20) == 19660 and html_budget(4) == 65536
    assert "### JSON-LD (shortened)" in text and len(text) > 20 * (19660 + JSONLD_MAX_CHARS // 2)


def test_control_characters_and_fences_cannot_break_the_file():
    row = {
        "sample_id": "smp_x",
        "url": "https://x.example/\x0c",
        "distilled_html": "<p>a\x00b\x85c ```` d</p>",
        "jsonld": [],
        "meta": {},
    }
    text, _report = render_samples("x.example", [row])
    assert not PLATFORM_CONTROL.search(text)
    assert "`````html\n<p>a b c ```` d</p>\n`````" in text


def test_a_refinement_carries_the_previous_candidate_and_its_failures():
    task = {
        "extraction_instructions": "Lamps: name and price. "
        * 60,  # > 800: an input excerpt cuts it
        "fields": ["title", "price"],
        "required_fields": ["title"],
        "attempt": 1,
        "previous_mappings": [{"template_id": "product", "fields": [{"name": "price"}]}],
        "failures": [{"kind": "fill", "field": "price", "message": "0 of 4 samples"}],
    }
    text, _report = render_samples("x.example", samples()[:1], task)
    assert task["extraction_instructions"].strip() in text
    assert "### Previous candidate" in text and '"template_id": "product"' in text
    assert "0 of 4 samples" in text and "Attempt: 1" in text


def test_the_action_stores_samples_md(tmp_path):
    from handlers.router import handle_request

    descriptor = samples_descriptor(tmp_path)
    payload = make_payload(
        "write_samples_file",
        {"site": "books.toscrape.com", "samples": samples()},
        service_url="http://127.0.0.1:9",
        artifacts={"inputs": [], "outputs": [descriptor]},
    )
    response = handle_request(payload)
    assert response["ok"], response
    result = response["result"]
    assert result["artifact_handle"] == "artifact:art_samples" and result["sample_count"] == 4
    stored = Path(descriptor["_path"]).read_text(encoding="utf-8")
    assert stored.startswith("# Extraction mapping") and len(stored) == result["size_chars"]
    assert samples()[3]["distilled_html"] in stored


def test_the_agent_reads_the_file_and_gets_no_samples_input():
    steps = {s["step_id"]: s for s in IMPL["steps"]}
    write = steps["write_samples"]
    agent = steps["write_mapping"]
    assert write["override"]["connector"]["operation_id"] == "write_samples_file"
    assert write["output_mapping_jsonb"]["artifact_handle"] == "state.samples_file"
    assert agent["override"]["agent"]["file_bindings"] == [
        {"name": "samples", "source": "state.samples_file"}
    ]
    assert "samples" not in agent["input_mapping_jsonb"]
    edges = IMPL["settings"]["execution_graph"]["edges"]
    rework = next(e for e in edges if e["rework"])
    assert rework["target"]["id"] == "write_samples"  # every refinement gets a fresh file
    policies = yaml.safe_load((ROOT / "contracts/artifact_policies.yaml").read_text())["policies"]
    grant = next(p for p in policies if p["operation_id"] == "write_samples_file")["outputs"][0]
    assert (grant["filename"], grant["content_type"]) == ("samples.md", "text/markdown")


def test_the_skill_declares_its_structured_output():
    """AI-2: an installed skill reaches the runtime as its bundle document; only
    `output_schema` (not expected_outputs) becomes artifacts.* in the model's schema."""
    schema = SKILL["output_schema"]
    assert schema["required"] == ["mappings_json"]
    # JSON text, not objects: strict structured output cannot carry free-form objects.
    assert schema["properties"]["mappings_json"]["type"] == "string"
    assert schema["properties"]["notes"]["type"] == "string"
    assert {row["name"] for row in SKILL["expected_outputs"]} == set(schema["properties"])
