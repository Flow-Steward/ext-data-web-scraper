"""0.1.20: service Batch B (301145b): link rules, duplicates, one run per site, the stall
watchdog, and the run list with completeness (one call for the Runs page)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fake_service import example, schema_errors
from test_pages_0_1_18 import query  # noqa: F401 - the page query fixture
from test_templates import SCRAPE

ROOT = Path(__file__).resolve().parents[1]
RUN = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"
RUNNING = "run_01JCZB2D9F4GH6JK8LM0NP2QR4"
LINKS_SKIPPED = {
    "sensitive_path": 15500,
    "facet_params": 11340,
    "file_extension": 25,
    "canonical": 4,
}


def _listing(**changes):
    listing = example("get_runs.response.json")
    for item in listing["items"]:
        item.update(changes.get(item["run_id"], {}))
    assert not schema_errors("run_summary.schema.json#/$defs/run_list", listing)
    return listing


# ----------------------------------------------------------------------- Runs


def test_runs_table_is_one_call_with_progress_and_missed(query, service):  # noqa: F811
    listing = _listing(
        **{
            RUNNING: {
                "completeness": {
                    "target_pages_found": 120,
                    "pages_collected": 37,
                    "records": 35,
                    "not_collected": 0,
                    "not_collected_by_reason": {},
                    "progress": {
                        "collected": 37,
                        "expected": 120,
                        "estimate": False,
                        "text": "37 of 120",
                    },
                }
            },
            RUN: {
                "outcome": "partial",
                "completeness": {
                    "target_pages_found": 1400,
                    "pages_collected": 1318,
                    "records": 1318,
                    "not_collected": 82,
                    "not_collected_by_reason": {"fetch_failed": 80, "blocked": 2},
                },
            },
        }
    )
    service.override("GET", r"^/v1/runs$", 200, listing)
    rows = {row["run_id"]: row for row in query("runs_table")["result"]["items"]}
    assert rows[RUNNING]["progress"] == "37 of 120"
    assert rows[RUN]["progress"] == "1318 of 1400"
    assert rows[RUN]["why_stopped"] == "finished, partial, 82 pages missed"
    assert rows[RUN]["can_recollect"] is True and rows[RUNNING]["can_recollect"] is False
    assert len(service.calls("GET", "/v1/runs")) == 1  # no read per run


def test_queued_behind_the_same_site(query, service):  # noqa: F811
    listing = _listing(
        **{
            RUNNING: {
                "state": "queued",
                "queued_reason": {
                    "code": "same_site_running",
                    "run_id": RUN,
                    "message": "Waiting: run run_01JCXA7Q3M5VZ8K2N4P6R8T0W1 of this site is still running",
                },
            }
        }
    )
    service.override("GET", r"^/v1/runs$", 200, listing)
    rows = {row["run_id"]: row for row in query("runs_table")["result"]["items"]}
    assert rows[RUNNING]["why_stopped"] == "waiting: another run of this site is running"
    assert rows[RUNNING]["can_stop"] is True


def test_stalled_in_words(query, service):  # noqa: F811
    listing = _listing(**{RUN: {"stop_reason": "stalled", "outcome": "partial"}})
    service.override("GET", r"^/v1/runs$", 200, listing)
    rows = {row["run_id"]: row for row in query("runs_table")["result"]["items"]}
    assert rows[RUN]["why_stopped"].startswith("stalled, partial")
    from handlers.runs import STOP_REASON_TEXT

    assert "nothing moved for too long" in STOP_REASON_TEXT["stalled"]


# ----------------------------------------------------------------- Run detail


def test_run_detail_shows_skipped_links_duplicates_and_waiting(query, service):  # noqa: F811
    run = example("get_run.response.json")
    run["counters"]["links_skipped"] = LINKS_SKIPPED
    run["counters"]["pages_by_class"]["duplicate"] = 4
    assert not schema_errors("run_summary.schema.json", run)
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    content = query("run_report", {"run_id": RUN})["result"]["content"]
    assert "Skipped links: 15 500 account/cart/wishlist, 11 340 filters, 25 files." in content
    assert "4 pages skipped as duplicates" in content

    queued = example("get_run.response.json")
    queued.update(
        {
            "state": "queued",
            "outcome": None,
            "stop_reason": None,
            "result": None,
            "queued_reason": {
                "code": "same_site_running",
                "run_id": RUNNING,
                "message": "Waiting until the other run of lumen-shop.example ends",
            },
        }
    )
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, queued)
    content = query("run_report", {"run_id": RUN})["result"]["content"]
    assert "- Waiting until the other run of lumen-shop.example ends." in content


def test_summary_and_start_say_the_run_waits(invoke, service):
    from conftest import ROOT_SCOPE

    run = example("get_run.response.json")
    run.update(
        {
            "state": "queued",
            "outcome": None,
            "stop_reason": None,
            "result": None,
            "queued_reason": {
                "code": "same_site_running",
                "run_id": RUNNING,
                "message": "Waiting for the other run",
            },
        }
    )
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    message = invoke("get_run_summary", {"run_id": RUN})["result"]["message"]
    assert "Waiting for the other run." in message

    started = example("post_runs.response.json")
    started.update(
        {
            "state": "queued",
            "queued_reason": {
                "code": "same_site_running",
                "run_id": RUNNING,
                "message": "Waiting for the other run",
            },
        }
    )
    started["platform"] = {
        "job_id": ROOT_SCOPE["job_id"],
        "step_id": "start_run",
        "project_id": "prj_1",
        "connection_id": "conn_scraper_1",
    }
    service.override("POST", r"^/v1/runs$", 201, started)
    config = example("post_config_validate.response.json")["config"]
    hint = invoke("start_run", {"config": config})["result"]["run_page_hint"]
    assert hint.startswith("Waiting for the other run. Scraper run ")


# ------------------------------------------------------------------- analysis


def test_analyze_site_names_the_link_rules(invoke, service):
    answer = example("post_analyze.response.json")
    answer["link_rules"] = {
        "rules": [
            {
                "name": "sensitive_path",
                "on": True,
                "description": "account, cart and checkout pages",
            },
            {"name": "facet_params", "on": False, "description": "filter and sort variants"},
        ],
        "trap_pattern_budget": 100,
    }
    assert not schema_errors("analysis.schema.json", answer)
    service.override("POST", r"^/v1/analyze$", 200, answer)
    config = example("post_config_validate.response.json")["config"]
    text = invoke("analyze_site", {"config": config})["result"]["link_rules"]
    assert text == (
        "The crawl skips: account, cart and checkout pages. Switched off for this run: "
        "facet_params. Trap budget: a URL pattern stops being followed after 100 pages without "
        "a product page."
    )


def test_analyze_site_without_link_rules_from_an_older_service(invoke, service):
    config = example("post_config_validate.response.json")["config"]
    assert invoke("analyze_site", {"config": config})["result"]["link_rules"] == ""


# ------------------------------------------------------------------- run form


def test_link_rules_and_trap_budget_reach_the_service(invoke, service):
    response = invoke(
        "validate_config",
        {
            "site_url": "https://www.lumen-shop.example/",
            "link_rules_off": "facet_params, Canonical",
            "trap_pattern_budget": "0",
        },
    )
    assert response["ok"], response
    layer = service.calls("POST", "/v1/config/validate")[0]["body"]["run_inputs"]
    assert layer["link_rules_off"] == ["facet_params", "canonical"]
    assert layer["trap_pattern_budget"] == 0
    assert not service.schema_violations


def test_an_unknown_rule_is_refused_before_the_service(invoke, service):
    response = invoke(
        "validate_config",
        {"site_url": "https://www.lumen-shop.example/", "link_rules_off": ["filters"]},
    )
    assert response["error_code"] == "invalid_payload"
    assert "'filters' is not a rule" in response["error"] and "facet_params" in response["error"]
    assert not service.calls("POST", "/v1/config/validate")


def test_the_rule_names_are_the_contract_s():
    from handlers.settings import LINK_RULES

    job = json.loads((ROOT / "service_contract/schemas/job_config.schema.json").read_text())
    assert list(LINK_RULES) == job["$defs"]["link_rule"]["enum"]


def test_run_form_inputs():
    rows = {row["name"]: row for row in SCRAPE["settings"]["callable"]["expected_inputs"]}
    for name, kind in (("link_rules_off", "array"), ("trap_pattern_budget", "number")):
        assert rows[name]["type"] == kind and rows[name]["required"] is False
        assert "default" not in rows[name]
    assert "Empty: every rule on" in rows["link_rules_off"]["description"]
    assert "0: off. Empty: 100." in rows["trap_pattern_budget"]["description"]
    validate = next(s for s in SCRAPE["steps"] if s["step_id"] == "validate_settings")
    assert (
        validate["input_mapping_jsonb"]["link_rules_off"]
        == "state.inputs.link_rules_off|default:null"
    )
    analyze = next(s for s in SCRAPE["steps"] if s["step_id"] == "analyze_site")
    assert analyze["output_mapping_jsonb"]["link_rules"] == "state.link_rules"


@pytest.mark.parametrize(
    "words",
    [
        "## Links the scraper skips",
        "switch off `facet_params`",
        "**The run waits: another run of this site is running**",
        "**The run stopped: stalled**",
        "**Links not to skip** (`link_rules_off`)",
        "**Trap budget (pages)** (`trap_pattern_budget`)",
    ],
)
def test_guide(words):
    assert words in (ROOT / "docs/setup-guide.md").read_text(encoding="utf-8")


def test_bundle_versions_follow_the_release():
    version = yaml.safe_load((ROOT / "extension.yaml").read_text())["version"]
    assert SCRAPE["version"] == version
