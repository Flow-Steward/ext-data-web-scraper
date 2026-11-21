"""Product variants (ROUND2 §2/§3, service 2f63b48): from the samples to the mapping agent,
the variants block, the mapping test's variant figures and the workflows."""

from __future__ import annotations

import copy
import json

from conftest import make_payload, samples_descriptor
from fake_service import example, schema_errors
from test_templates import IMPL, SCRAPE, SCRAPE_INPUTS, _connector
from workflow_sim import StepFailed, Walk

from handlers.mappings import structural_problems, variant_summary
from handlers.router import handle_request
from handlers.samples_file import SAMPLES_FILE_MAX_CHARS, render_samples

SAMPLE_IDS = [row["sample_id"] for row in example("post_samples.response.json")["samples"]]

#: A WooCommerce variable product: lens covers marked no / EU / USA at 24 / 54 / 54 EUR,
#: the same parent SKU on every variant on purpose (owner, xenonled.eu).
WOO_SOURCE = {
    "source": {
        "kind": "embedded_json",
        "selector": "form.variations_form",
        "attribute": "data-product_variations",
        "format": "woocommerce",
    },
    "count": 3,
    "option_names": ["Lens cover markings"],
    "example": {
        "options": {"Lens cover markings": "No markings"},
        "price": 24.0,
        "currency": "EUR",
        "sku": "BMW-F10-KP-2009-2017",
        "availability": "InStock",
    },
    "raw_keys": ["attributes", "display_price", "sku", "variation_id", "image"],
}
#: A Shopify product: Format CD / 12" vinyl, prices in minor units (owner, propermusic.com).
SHOPIFY_SOURCE = {
    "source": {"kind": "shop_json", "platform": "shopify", "currency": "GBP"},
    "count": 2,
    "option_names": ["Format"],
    "example": {"options": {"Format": "CD"}, "price": 11.99, "sku": "PRM-CD-1"},
    "raw_keys": ["id", "option1", "price", "sku", "available"],
}
MICRODATA = [{"@type": "Product", "name": "BMW lens cover", "offers": {"price": "24.00"}}]

WOO_MAPPING = example("mapping.variants.json")
SHOPIFY_MAPPING = {
    "schema_version": 1,
    "template_id": "release",
    "label": "Release (Shopify)",
    "match": {"url_patterns": ["/products/[^/?]+$"]},
    "fields": [
        {
            "name": "title",
            "type": "text",
            "required": True,
            "sources": [{"kind": "meta", "name": "og:title"}],
        },
        {
            "name": "price",
            "type": "price",
            "required": True,
            "sources": [{"kind": "meta", "name": "product:price:amount"}],
        },
        {
            "name": "currency",
            "type": "currency",
            "sources": [{"kind": "meta", "name": "product:price:currency"}],
        },
    ],
    "variants": {
        "sources": [{"kind": "shop_json", "platform": "shopify", "currency": "GBP"}],
        "fields": [
            {"name": "format", "option": "Format"},
            {"name": "price", "path": "price"},
            {"name": "sku", "path": "sku"},
        ],
    },
}


def _samples(source: dict) -> dict:
    answer = example("post_samples.response.json")
    for row in answer["samples"]:
        row["variant_sources"] = [copy.deepcopy(source)]
        row["microdata"] = copy.deepcopy(MICRODATA)
    assert not schema_errors("samples.schema.json", answer)
    return answer


def _test_answer(template_id: str, *, variants: bool, fill: int = 3) -> dict:
    """The service's mapping test for a template that matched both samples."""
    answer = example("post_mappings_test.response.json")
    template = copy.deepcopy(answer["templates"][0])
    template.update(template_id=template_id, matched_samples=2, passed=True, empty_fields=[])
    if variants:
        template.update(
            variant_count=6,
            samples_with_variants=2,
            variant_sources_used={"embedded_json": 2},
            variant_fields=[
                {
                    "name": "price",
                    "variant_count": 6,
                    "filled_count": 6,
                    "fill_rate": 1.0,
                    "example_values": [24.0, 54.0],
                },
                {
                    "name": "markings",
                    "variant_count": 6,
                    "filled_count": fill * 2,
                    "fill_rate": fill / 3,
                    "example_values": ["EU sticker"],
                },
            ],
        )
    answer["templates"] = [template]
    for row in answer["samples"]:
        row["template_id"] = template_id
        if variants:
            row["variants"] = {
                "count": 3,
                "source": "embedded_json",
                "source_index": 0,
                "items": [],
            }
    answer["warnings"] = []
    assert not schema_errors("mapping_test.schema.json#/$defs/response", answer)
    return answer


# ---------------------------------------------------------------- the mappings


def test_the_variant_mappings_follow_the_contract():
    for mapping in (WOO_MAPPING, SHOPIFY_MAPPING):
        assert not schema_errors("mapping.schema.json", mapping), mapping["template_id"]
        assert structural_problems([mapping]) == []


def test_a_wrong_variants_block_is_named_for_the_agent():
    bad = copy.deepcopy(WOO_MAPPING)
    bad["variants"]["sources"] = []
    bad["variants"]["fields"].append({"name": "x", "path": "sku", "option": "Size"})
    problems = structural_problems([bad])
    assert any("variants.sources needs 1 to 5 sources" in p for p in problems)
    assert any("variants field x needs exactly one of path" in p for p in problems)


def test_the_skill_example_with_variants_is_a_valid_mapping():
    import yaml
    from fake_service import ROOT

    skill = yaml.safe_load((ROOT / "artifacts/skills/build_extraction_mapping.yaml").read_text())
    outputs = [row["output"]["mappings"] for row in skill["examples"]]
    with_variants = [m for rows in outputs for m in rows if "variants" in m]
    assert with_variants
    for mapping in (m for rows in outputs for m in rows):
        assert not schema_errors("mapping.schema.json", mapping), mapping["template_id"]
    text = "\n".join(skill["instructions"])
    for words in (
        "variant_sources",
        "exactly as it is",
        "never treat equal SKUs as duplicates",
        "minor units",
    ):
        assert words in text, words


# ------------------------------------------------------------- samples -> agent


def test_fetch_samples_hands_the_variant_data_to_the_agent(invoke, service):
    config = invoke("validate_config", {"site_url": SCRAPE_INPUTS["site_url"]})["result"]["config"]
    service.override("POST", r"^/v1/samples$", 200, _samples(WOO_SOURCE))
    result = invoke("fetch_samples", {"config": config})["result"]
    assert result["variant_sample_ids"] == SAMPLE_IDS
    assert result["samples"][0]["variant_sources"][0]["source"] == WOO_SOURCE["source"]
    assert result["samples"][0]["microdata"] == MICRODATA
    assert result["variant_summary"].startswith(
        "2 of 2 samples carry variants (one record per variant).\n"
    )
    assert "embedded_json 3 variants (options Lens cover markings)" in result["variant_summary"]
    plain = invoke("fetch_samples", {"config": config})["result"]
    assert plain["variant_sample_ids"] == [] and plain["variant_summary"] == ""


def test_the_samples_file_shows_the_sources_to_copy(service, tmp_path):
    descriptor = samples_descriptor(tmp_path)
    samples = _samples(SHOPIFY_SOURCE)["samples"]
    payload = make_payload(
        "write_samples_file",
        {"site": "propermusic.example", "samples": samples},
        service_url=service.url,
        artifacts={"inputs": [], "outputs": [descriptor]},
    )
    assert handle_request(payload)["ok"]
    text = open(descriptor["_path"], encoding="utf-8").read()
    assert (
        "### Variant sources found on this page - copy one `source` into variants.sources" in text
    )
    assert '"platform": "shopify"' in text and '"currency": "GBP"' in text
    assert "### Microdata (schema.org itemprop; source kind `microdata`)" in text


def test_the_samples_file_budget_holds_with_variants_and_microdata():
    rows = [
        {
            "sample_id": f"smp_{i}",
            "url": f"https://x.example/p/{i}",
            "distilled_html": "<div>" + "y" * 90000 + "</div>",
            "jsonld": [{"@type": "Product", "description": "d" * 40000}],
            "meta": {"k": "v" * 9000},
            "variant_sources": [{**WOO_SOURCE, "raw_keys": ["k" * 50] * 400}] * 5,
            "microdata": [{"@type": "Product", "description": "m" * 20000}],
        }
        for i in range(50)
    ]
    previous = [{"template_id": "t", "notes": "p" * 70000}]
    task = {
        "previous_mappings": previous,
        "failures": [{"m": "f" * 70000}],
        "service_failures": {"x": "s" * 70000},
    }
    text, _report = render_samples("x.example", rows, task)
    assert len(text) <= SAMPLES_FILE_MAX_CHARS


def test_variant_summary_counts_every_sample():
    rows = [
        {"url": "https://a.example/p/1", "variant_sources": [WOO_SOURCE]},
        {"url": "https://a.example/p/2"},
    ]
    assert (
        variant_summary(rows).splitlines()[0]
        == "1 of 2 samples carry variants (one record per variant)."
    )


# ------------------------------------------------------------------ the test


def test_a_template_without_variants_on_variant_samples_fails(invoke, service):
    plain = copy.deepcopy(WOO_MAPPING)
    del plain["variants"]
    service.override(
        "POST", r"^/v1/mappings/test$", 200, _test_answer("lens_cover", variants=False)
    )
    result = invoke(
        "test_mapping",
        {
            "site": "lumen-shop.example",
            "mappings_json": json.dumps([plain]),
            "sample_ids": SAMPLE_IDS,
            "variant_sample_ids": SAMPLE_IDS,
        },
    )["result"]
    assert result["passed"] is False
    missing = [f for f in result["failures"] if f["kind"] == "variants_missing"]
    assert missing and missing[0]["message"].startswith(
        "template lens_cover: 2 matched samples carry variants (see variant_sources) but the "
        "mapping has no variants block"
    )
    assert "Not passed: template lens_cover: 2 matched samples carry variants" in result["summary"]


def test_variant_figures_are_read_and_low_fill_warns(invoke, service):
    service.override(
        "POST", r"^/v1/mappings/test$", 200, _test_answer("lens_cover", variants=True, fill=1)
    )
    result = invoke(
        "test_mapping",
        {
            "site": "lumen-shop.example",
            "mappings_json": json.dumps([WOO_MAPPING]),
            "sample_ids": SAMPLE_IDS,
            "variant_sample_ids": SAMPLE_IDS,
        },
    )["result"]
    assert result["passed"] is True
    template = result["templates"][0]
    assert template["variant_count"] == 6 and template["variant_fields"][1]["filled"] == "2/6"
    assert (
        "lens_cover: 6 variants on 2 of 2 samples (embedded_json 2); price 6/6, markings 2/6"
        in result["summary"]
    )
    assert [w["kind"] for w in result["warnings"] if w["kind"].startswith("variant")] == [
        "variant_fill"
    ]
    assert "variant field markings filled on 2/6 variants" in result["summary"]


def test_a_variants_block_that_finds_nothing_warns(invoke, service):
    answer = _test_answer("lens_cover", variants=True)
    answer["templates"][0].update(
        variant_count=0,
        samples_with_variants=0,
        variant_sources_used={"none": 2},
        variant_fields=[],
    )
    service.override("POST", r"^/v1/mappings/test$", 200, answer)
    result = invoke(
        "test_mapping",
        {
            "site": "lumen-shop.example",
            "mappings_json": json.dumps([WOO_MAPPING]),
            "sample_ids": SAMPLE_IDS,
        },
    )["result"]
    assert any(w["kind"] == "variants_not_found" for w in result["warnings"])
    assert "lens_cover: 0 variants on 0 of 2 samples" in result["summary"]


# --------------------------------------------------------------- the workflows


def _agent_sequence(*answers):
    """A scripted mapping agent: one answer per call, the last one repeated."""
    calls: list[dict] = []

    def agent(_step, inputs):
        calls.append(inputs)
        mappings = answers[min(len(calls), len(answers)) - 1]
        for mapping in mappings:
            assert not schema_errors("mapping.schema.json", mapping), mapping["template_id"]
        return {"mappings_json": json.dumps(mappings), "notes": "scripted"}

    return agent, calls


def _invoke_child(service, tmp_path, agent):
    def run(_step, inputs):
        walk = Walk(IMPL, connector=_connector(service, tmp_path), agent=agent)
        outcome, state = walk.run(inputs, raw_inputs=True)
        if outcome == "failed":
            raise StepFailed("the build_mapping child failed: " + "; ".join(walk.failures))
        return {
            name: state.get(name)
            for name in ("mappings", "passed", "field_results", "failures", "attempts", "summary")
        }

    return run


def _walk(service, tmp_path, source, mapping, template_answers, agent_answers):
    service.mappings.clear()
    # Match this synthetic source, rather than inheriting the unrelated lamp
    # selector from the shared config example. Coverage warnings now trigger
    # bounded review and must not consume these source-specific test answers.
    validated = example("post_config_validate.response.json")
    validated["config"]["target_page_selector"] = mapping["match"].get("selector")
    service.override("POST", r"^/v1/config/validate$", 200, validated)
    service.override("POST", r"^/v1/samples$", 200, _samples(source))
    for answer in template_answers:
        service.override("POST", r"^/v1/mappings/test$", 200, answer)
    agent, calls = _agent_sequence(*agent_answers)
    walk = Walk(
        SCRAPE,
        connector=_connector(service, tmp_path),
        invoke=_invoke_child(service, tmp_path, agent),
        child=IMPL,
    )
    outcome, state = walk.run(dict(SCRAPE_INPUTS))
    return walk, outcome, state, calls


def test_scrape_a_site_builds_a_woocommerce_variants_mapping(service, tmp_path):
    plain = copy.deepcopy(WOO_MAPPING)
    del plain["variants"]
    walk, outcome, state, calls = _walk(
        service,
        tmp_path,
        WOO_SOURCE,
        WOO_MAPPING,
        [
            _test_answer("lens_cover", variants=False),  # first candidate: no variants block
            _test_answer("lens_cover", variants=True),  # refined candidate
            _test_answer("lens_cover", variants=True),  # test before activation
        ],
        [[plain], [WOO_MAPPING]],
    )
    assert outcome == "stop_success", walk.failures
    assert len(calls) == 2 and calls[0]["variant_sample_ids"] == SAMPLE_IDS
    feedback = json.dumps(calls[1]["failures"])
    assert "variants_missing" in feedback
    assert state["variant_sample_ids"] == SAMPLE_IDS and "embedded_json" in state["variant_summary"]
    put = service.calls("PUT", "/v1/sites/")[-1]["body"]["mapping"]
    assert put["variants"]["sources"][0] == WOO_SOURCE["source"]
    assert {"name": "markings", "option": "Lens cover markings"} in put["variants"]["fields"]
    assert "6 variants on 2 of 2 samples" in state["mapping_summary"]
    assert not service.schema_violations


def test_scrape_a_site_builds_a_shopify_variants_mapping(service, tmp_path):
    walk, outcome, state, calls = _walk(
        service,
        tmp_path,
        SHOPIFY_SOURCE,
        SHOPIFY_MAPPING,
        [_test_answer("release", variants=True), _test_answer("release", variants=True)],
        [[SHOPIFY_MAPPING]],
    )
    assert outcome == "stop_success", walk.failures
    assert len(calls) == 1
    put = service.calls("PUT", "/v1/sites/")[-1]["body"]["mapping"]
    assert put["variants"]["sources"] == [SHOPIFY_SOURCE["source"]]
    assert {"name": "format", "option": "Format"} in put["variants"]["fields"]
    assert not service.schema_violations


def test_records_with_variants_are_ordinary_result_lines():
    """record.variant (contract row 37): one record per variant, the same SKU allowed."""
    record = example("record.variant.json")
    assert not schema_errors("record.schema.json", record)
    second = copy.deepcopy(record)
    second["variant"].update(index=2, options={"Lens cover markings": "USA sticker"})
    second["record_id"] = "rec_" + "1" * 32
    assert second["data"]["sku"] == record["data"]["sku"]
    assert not schema_errors("record.schema.json", second)
