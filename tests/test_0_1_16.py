"""0.1.16: the completeness sentence is said once (A38); a url_list run whose pages went into
the URL filter is told where to put them (A39)."""

from __future__ import annotations

import json
from pathlib import Path

from test_round2b import COMPARISON, COMPLETENESS, RUN, _run

ROOT = Path(__file__).resolve().parents[1]
SAID = "Found 3 target pages, collected 3, none missing."


def test_summary_message_says_the_completeness_once(invoke, service):
    run = _run(
        state="completed",
        outcome="complete",
        stop_reason="finished",
        explanation={"sentence": f"The run finished: {SAID.lower()}", "advice": ""},
        completeness={**COMPLETENESS, "sentence": SAID},
    )
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    result = invoke("get_run_summary", {"run_id": RUN})["result"]
    assert result["message"].lower().count(SAID.rstrip(".").lower()) == 1
    assert result["completeness"] == SAID


def test_summary_keeps_the_comparison_warning_when_the_sentence_is_dropped(invoke, service):
    run = _run(
        state="completed",
        outcome="partial",
        stop_reason="finished",
        explanation={"sentence": f"The run finished. {COMPLETENESS['sentence']}", "advice": ""},
        completeness=COMPLETENESS,
        comparison=COMPARISON,
    )
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    message = invoke("get_run_summary", {"run_id": RUN})["result"]["message"]
    assert message.count(COMPLETENESS["sentence"]) == 1
    assert message.endswith(COMPARISON["warning"])


def test_summary_keeps_a_completeness_the_explanation_does_not_say(invoke, service):
    run = _run(
        state="completed",
        outcome="partial",
        stop_reason="finished",
        explanation={"sentence": "The run finished.", "advice": ""},
        completeness=COMPLETENESS,
    )
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    message = invoke("get_run_summary", {"run_id": RUN})["result"]["message"]
    assert message.endswith("The run finished. " + COMPLETENESS["sentence"])


def _refused_url_list(invoke, service, **inputs):
    service.override(
        "POST",
        r"^/v1/config/validate$",
        200,
        {
            "valid": False,
            "errors": [
                {"field": "urls", "code": "required", "message": "url_list mode needs urls"}
            ],
            "warnings": [],
        },
    )
    return invoke(
        "validate_config",
        {"site_url": "https://shop.example", "discovery_mode": "url_list", **inputs},
    )


def test_url_list_pages_in_the_url_filter_are_pointed_to_the_page_list(invoke, service):
    response = _refused_url_list(invoke, service, url_include=["https://shop.example/products/a"])
    assert response["error_code"] == "config_invalid"
    assert "url_list mode needs urls" in response["error"]
    assert "put them into 'Pages to scrape (url_list)'" in response["error"]


def test_url_list_without_any_addresses_gets_no_filter_hint(invoke, service):
    response = _refused_url_list(invoke, service)
    assert response["error_code"] == "config_invalid"
    assert "Pages to scrape" not in response["error"]


def test_the_two_url_fields_cannot_be_confused_on_the_run_form():
    bundle = json.loads((ROOT / "templates" / "scrape_site.bundle.json").read_text("utf-8"))
    labels: dict[str, dict] = {}

    def walk(node):
        if isinstance(node, dict):
            if node.get("name") in {"urls", "url_include"} and "label" in node:
                labels[node["name"]] = node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(bundle)
    assert labels["urls"]["label"] == "Pages to scrape (url_list)"
    assert labels["url_include"]["label"] == "Extract only URLs matching (regex)"
    assert "Pages to scrape (url_list)" in labels["url_include"]["description"]
