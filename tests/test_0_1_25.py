"""0.1.25: texts only. A queued start says it is queued (A70a); Run detail compares a run
with the previous full run only after it ends (A70b); a failed Fetch samples gives the
service's explanation once (A71). Catalog metadata for the public release.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import ROOT_SCOPE
from fake_service import example, schema_errors
from test_pages_0_1_18 import query  # noqa: F401 - the page query fixture
from test_protection import _config, _samples
from test_round2b import COMPARISON, COMPLETENESS, _run

ROOT = Path(__file__).resolve().parents[1]
RUN = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"
RUNNING = "run_01JCZB2D9F4GH6JK8LM0NP2QR4"
CATEGORIES = {
    "ai",
    "cms",
    "crm",
    "data",
    "developer-tools",
    "documents",
    "ecommerce",
    "email",
    "finance",
    "hr",
    "inventory",
    "it",
    "marketing",
    "media",
    "messaging",
    "operations",
    "other",
    "productivity",
    "sales",
    "support",
}


# ------------------------------------------------------------ A70a: queued start


def _start(service, state: str) -> str:
    started = example("post_runs.response.json")
    started["state"] = state
    if state == "queued":
        started["queued_reason"] = {
            "code": "same_site_running",
            "run_id": RUNNING,
            "message": "Another run of this site is running (run "
            f"{RUNNING}); this run starts when it ends",
        }
    started["platform"] = {
        "job_id": ROOT_SCOPE["job_id"],
        "step_id": "start_run",
        "project_id": "prj_1",
        "connection_id": "conn_scraper_1",
    }
    assert not schema_errors("run_summary.schema.json", started)
    service.override("POST", r"^/v1/runs$", 201, started)
    return started["run_id"]


def test_a_queued_start_says_it_is_queued(invoke, service):
    run_id = _start(service, "queued")
    config = example("post_config_validate.response.json")["config"]
    hint = invoke("start_run", {"config": config})["result"]["run_page_hint"]
    assert hint.startswith("Another run of this site is running (run ")
    assert "this run starts when it ends." in hint
    assert f"Scraper run {run_id} is queued." in hint
    assert "has started" not in hint


def test_a_started_run_still_says_it_has_started(invoke, service):
    run_id = _start(service, "running")
    config = example("post_config_validate.response.json")["config"]
    hint = invoke("start_run", {"config": config})["result"]["run_page_hint"]
    assert hint.startswith(f"Scraper run {run_id} has started.")
    assert "is queued" not in hint


# --------------------------------------------- A70b: comparison after the end only


RUNNING_EXPLANATION = {"sentence": "Running: 4 target pages collected so far."}


def _crawling(state: str = "running") -> dict:
    """The service's answer while the run crawls: it already compares, and appends the
    warning to its explanation (web-scraper-service app/jobs/manager.py)."""
    return _run(
        state=state,
        outcome=None,
        stop_reason=None,
        finished_at=None,
        result=None,
        completeness={
            **COMPLETENESS,
            "progress": {
                "collected": 4,
                "expected": 20,
                "estimate": True,
                "text": "4 of 20 so far",
            },
        },
        comparison=COMPARISON,
        explanation={
            "sentence": RUNNING_EXPLANATION["sentence"] + " " + COMPARISON["warning"],
        },
    )


@pytest.mark.parametrize("state", ["queued", "running", "stopping", "waiting_for_credentials"])
def test_run_detail_does_not_compare_a_run_that_has_not_ended(query, service, state):  # noqa: F811
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, _crawling(state))
    content = query("run_report", {"run_id": RUN})["result"]["content"]
    assert COMPARISON["warning"] not in content
    if state == "running":
        assert "Running: 4 target pages collected so far." in content
        assert "4 of 20 so far" in content


def test_show_run_and_the_summary_do_not_compare_it_either(invoke, service):
    for _call in range(2):  # an override answers once
        service.override("GET", rf"^/v1/runs/{RUN}$", 200, _crawling())
    shown = invoke("show_run", {"run_id": RUN})["result"]
    assert COMPARISON["warning"] not in shown["records"]
    assert COMPARISON["warning"] not in shown["outcome"]
    summary = invoke("get_run_summary", {"run_id": RUN})["result"]
    assert summary["comparison_warning"] == ""
    assert summary["explanation"] == RUNNING_EXPLANATION["sentence"]
    assert COMPARISON["warning"] not in summary["completeness"]


def test_a_finished_run_keeps_the_comparison(query, invoke, service):  # noqa: F811
    run = _run(
        state="completed",
        outcome="partial",
        completeness=COMPLETENESS,
        comparison=COMPARISON,
        explanation={"sentence": "The run ended. " + COMPARISON["warning"]},
    )
    for _call in range(2):  # an override answers once
        service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    assert COMPARISON["warning"] in query("run_report", {"run_id": RUN})["result"]["content"]
    summary = invoke("get_run_summary", {"run_id": RUN})["result"]
    assert summary["comparison_warning"] == COMPARISON["warning"]
    assert summary["explanation"] == "The run ended. " + COMPARISON["warning"]


# ------------------------------------------------- A71: the service's detail once


SUMMARY = (
    "start page not usable (HTTP 404); probed: /catalogue/* 4; 4 pages tried (4 over HTTP, "
    "0 in the browser, 0 already loaded); 0 samples, 4 failed; {seconds} s. Next: set the "
    "product page selector (a CSS selector only product pages have)."
)


def _no_target_pages(seconds_in_warning: int, seconds_in_summary: int) -> dict:
    answer = _samples(
        samples=[],
        warnings=[
            {
                "field": "sample_count",
                "code": "invalid_value",
                "message": "found 0 of 4 target pages: "
                + SUMMARY.format(seconds=seconds_in_warning),
            }
        ],
    )
    answer["explanation"] = {
        "summary": SUMMARY.format(seconds=seconds_in_summary),
        "counts": {"tried": 4, "failed": 4},
        "hints": [
            {
                "kind": "target_page_selector",
                "message": "set the product page selector (a CSS selector only product pages have)",
            }
        ],
    }
    assert not schema_errors("samples.schema.json", answer)
    return answer


@pytest.mark.parametrize("seconds", [(3, 3), (3, 4)])  # measured once per copy
def test_fetch_samples_gives_the_service_s_explanation_once(invoke, service, seconds):
    config = _config(invoke)
    service.override("POST", r"^/v1/samples$", 200, _no_target_pages(*seconds))
    response = invoke("fetch_samples", {"config": config})
    assert response["error_code"] == "no_samples"
    message = response["error"]
    assert message.count("start page not usable (HTTP 404)") == 1, message
    assert message.count("set the product page selector") == 1, message
    assert message.startswith(
        "No target page could be sampled. What the scraper service tried:\n"
        "found 0 of 4 target pages.\n"
        "start page not usable (HTTP 404); probed: /catalogue/* 4;"
    )


def test_another_count_warning_is_kept_whole(invoke, service):
    answer = _no_target_pages(3, 3)
    answer["warnings"][0]["message"] = "found 0 of 4 target pages: the sitemap was empty"
    service.override("POST", r"^/v1/samples$", 200, answer)
    message = invoke("fetch_samples", {"config": _config(invoke)})["error"]
    assert "found 0 of 4 target pages: the sitemap was empty\n" in message
    assert message.count("start page not usable (HTTP 404)") == 1


# --------------------------------------------------------- catalog metadata


MANIFEST = yaml.safe_load((ROOT / "extension.yaml").read_text(encoding="utf-8"))
DESCRIPTOR = yaml.safe_load((ROOT / "extension-descriptor.yaml").read_text(encoding="utf-8"))


def test_the_installed_card_is_short_and_the_catalog_page_detailed():
    """Flow Steward's installed card and detail page show the root `description` (manifest
    compile, query_presentation) and the marketplace view `identity.description`
    (query_marketplace_projection): one or two lines. The catalog page shows
    `listing.description` (build_index: listing.description, else the root one)."""
    listing = MANIFEST["listing"]
    short = listing["summary"]
    assert len(short) <= 160  # the catalog's MAX_SUMMARY_CHARS
    assert MANIFEST["description"] == short
    assert DESCRIPTOR["identity"]["description"] == short
    assert "\n" not in short
    assert listing["setup_summary"] not in (short, listing["description"])
    description = listing["description"]
    assert description.count("\n\n") >= 3 and len(description) > len(short) * 5
    for words in (
        "result.jsonl",
        "result.csv",
        "Docker",
        "Invoke",
        "supplier's catalogue",
        "competitors' public prices",
        "Politeness presets (gentle, normal, fast)",
        "skipped, or queued",
        "robots.txt is respected by default",
        "does not log in to sites",
        "cannot promise to get past CAPTCHAs",
        "Flow Steward 0.9.18",
    ):
        assert words in description, words
    assert listing["setup_summary"].startswith("Run the Web Scraper service in Docker")


def test_the_category_is_one_the_catalog_knows():
    for listing in (MANIFEST["listing"], DESCRIPTOR["listing"]):
        assert listing["category"] == "data"
        assert listing["categories"] == ["data", "ai"]
        assert set(listing["categories"]) <= CATEGORIES
    assert MANIFEST["listing"]["tags"] == DESCRIPTOR["identity"]["tags"]


def test_the_about_page_matches_0_1_23_and_later():
    about = yaml.safe_load((ROOT / "ui/components/setup_overview.yaml").read_text())["content"]
    assert "`result.jsonl`" in about and "`result.csv`" in about
    assert "stores\n  the result" not in about  # Scraper events no longer holds the files
    assert "does not log in to sites" in about


def test_guide_names_0_1_25():
    guide = (ROOT / "docs/setup-guide.md").read_text(encoding="utf-8")
    assert "0.1.25 changes texts only" in guide
