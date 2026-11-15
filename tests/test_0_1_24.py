"""0.1.24: Scrape a site callable with Invoke (A68); a start on a busy site ends 'Skipped'
(A66, service row 75); a cancelled job's crawl is named on Run detail (A67, Core 0.9.18 has
no cancel hook); service rc-next12 in the extension: politeness, page timeout, sale prices,
unusable answers, the price checks of the mapping test (rows 69-74).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fake_service import error_body, example, schema_errors
from test_0_1_23 import (
    SCRAPES,
    WAITING_PLATFORM,
    _edges,
    _scrape,
    _step,
    _test_answer,
    _test_inputs,
)
from test_pages_0_1_18 import query  # noqa: F401 - the page query fixture
from test_templates import SCRAPE_INPUTS

ROOT = Path(__file__).resolve().parents[1]
RUN = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"
BUSY_RUN = "run_01JCZB2D9F4GH6JK8LM0NP2QR4"
OPERATIONS = {
    op["operation_id"]: op
    for op in yaml.safe_load((ROOT / "contracts/operation_manifest.yaml").read_text())["operations"]
}
FORMS = {
    form["operation_id"]: form
    for form in yaml.safe_load((ROOT / "contracts/step_ui_manifest.yaml").read_text())["forms"]
}


def _busy(service, *, state="running", started_at="2026-11-15T08:10:12Z") -> None:
    """The service's answer to a start while a run of the site goes on (README 4.13)."""
    service.override(
        "POST",
        r"^/v1/runs$",
        409,
        error_body(
            "site_busy",
            f"A run of lumen-shop.example is already running since {started_at} (run "
            f"{BUSY_RUN}); this start was skipped.",
            details={
                "run_id": BUSY_RUN,
                "state": state,
                "started_at": started_at if state != "queued" else None,
                "created_at": "2026-11-15T08:09:00Z",
                "site": "lumen-shop.example",
            },
        ),
    )


# ------------------------------------------------------------- A68: Invoke


@pytest.mark.parametrize("variant", sorted(SCRAPES))
def test_scrape_a_site_can_be_called_with_invoke(variant):
    """Core refuses an Invoke of a workflow without `parent_workflow` among its trigger
    channels (`contract_validation_invoke.py`: invoke.child_not_parent_workflow_triggerable)."""
    channels = SCRAPES[variant]["settings"]["callable"]["trigger_channels"]
    assert channels == ["ui", "api", "mcp", "schedule", "parent_workflow"]
    assert SCRAPES[variant]["settings"]["callable"]["enabled"] is True


def test_the_uploaded_yaml_copy_is_callable_too():
    bundle = yaml.safe_load((ROOT / "templates/scrape_site.bundle.yaml").read_text())
    channels = bundle["workflows"][0]["settings"]["callable"]["trigger_channels"]
    assert "parent_workflow" in channels


def test_build_mapping_stays_an_action_only():
    workflow = yaml.safe_load((ROOT / "artifacts/workflows/impl_build_mapping.yaml").read_text())
    assert workflow["settings"]["callable"]["trigger_channels"] == ["parent_workflow"]


# ---------------------------------------------------------- A66: a busy site


def test_site_busy_has_its_own_code_and_a_plain_sentence():
    from handlers.errors import from_service_error

    body = error_body(
        "site_busy",
        "A run of shop.example is already running since … (run x); this start was skipped.",
        details={
            "run_id": BUSY_RUN,
            "state": "running",
            "started_at": "2026-11-15T08:10:12Z",
            "created_at": "2026-11-15T08:09:00Z",
            "site": "shop.example",
        },
    )
    assert not schema_errors("error.schema.json", body)
    error = from_service_error(409, body)
    assert error.code == "site_busy" and error.retryable is False
    assert error.message == (
        f"Skipped: shop.example is already being scraped (run {BUSY_RUN}, since "
        "2026-11-15 08:10 UTC). No new crawl was started; follow that run on Web Scraper > Runs."
    )
    assert error.result["details"] == {
        "run_id": BUSY_RUN,
        "state": "running",
        "started_at": "2026-11-15T08:10:12Z",
        "created_at": "2026-11-15T08:09:00Z",
        "site": "shop.example",
    }
    queued = from_service_error(
        409,
        error_body(
            "site_busy",
            "busy",
            details={
                "run_id": BUSY_RUN,
                "state": "queued",
                "started_at": None,
                "created_at": "2026-11-15T08:09:00Z",
                "site": "shop.example",
            },
        ),
    )
    assert f"(run {BUSY_RUN}, queued since 2026-11-15 08:09 UTC)" in queued.message


def test_start_run_fails_with_site_busy_unless_asked_to_succeed(invoke, service):
    config = example("post_config_validate.response.json")["config"]
    _busy(service)
    failed = invoke("start_run", {"config": config})
    assert failed["ok"] is False and failed["error_code"] == "site_busy"
    assert failed["error"].startswith("Skipped: lumen-shop.example is already being scraped")
    assert "site_busy" in OPERATIONS["start_run"]["error_codes"]
    _busy(service)
    skipped = invoke("start_run", {"config": config, "skipped_start_succeeds": True})
    assert skipped["ok"] is True
    result = skipped["result"]
    assert result["skipped"] is True and result["run_id"] == ""
    assert result["busy_run_id"] == BUSY_RUN and result["site"] == "lumen-shop.example"
    assert result["skipped_reason"] == (
        f"Skipped: lumen-shop.example is already being scraped (run {BUSY_RUN}, since "
        "2026-11-15 08:10 UTC). No new crawl was started; follow that run on Web Scraper > Runs."
    )
    started = invoke("start_run", {"config": config, "skipped_start_succeeds": True})["result"]
    assert started["skipped"] is False and started["run_id"] and started["skipped_reason"] == ""


@pytest.mark.parametrize("variant", sorted(SCRAPES))
def test_scrape_a_site_ends_skipped_on_a_busy_site(variant, service, tmp_path):
    workflow = SCRAPES[variant]
    start = _step(workflow, "start_crawl")
    assert start["input_mapping_jsonb"]["skipped_start_succeeds"] == "literal:true"
    assert start["output_mapping_jsonb"]["skipped"] == "state.skipped"
    assert start["output_mapping_jsonb"]["skipped_reason"] == "state.skipped_reason"
    edges = _edges(workflow)
    assert {
        ("start_crawl", "success", "start_skipped"),
        ("start_skipped", "true", "site_busy_skipped"),
        ("start_skipped", "false", "crawl_started"),
    } <= edges
    stop = _step(workflow, "site_busy_skipped")
    assert stop["step_kind"] == "stop_success"
    assert stop["override"]["stop_success"]["message"].startswith("Skipped: a run of this site")
    _busy(service)
    walk = _scrape(service, tmp_path, workflow, None, [])
    outcome, state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_success" and walk.trace[-1] == "site_busy_skipped"
    assert "crawl_started" not in walk.trace and not walk.waits and not walk.artifacts
    assert walk.outputs["skipped"] is True
    assert walk.outputs["skipped_reason"].startswith(
        "Skipped: lumen-shop.example is already being scraped (run "
    )
    assert "scraper_run_id" not in walk.outputs or not walk.outputs["scraper_run_id"]
    assert state["skipped"] is True


@pytest.mark.parametrize("variant", sorted(SCRAPES))
def test_a_started_crawl_still_waits(variant, service, tmp_path):
    service.run_patch = {"platform": WAITING_PLATFORM}

    def wakes(_step, pending):
        return [{"event_type": "run_completed", "correlation_key": pending["correlation_key"]}]

    walk = _scrape(service, tmp_path, SCRAPES[variant], wakes, [])
    outcome, _state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_success" and walk.trace[-1] == "done"
    index = walk.trace.index("start_skipped")
    assert walk.trace[index - 1 : index + 2] == ["start_crawl", "start_skipped", "crawl_started"]
    assert walk.outputs["skipped"] is False


def test_a_test_run_still_ends_without_a_crawl(service, tmp_path):
    walk = _scrape(service, tmp_path, SCRAPES["copy"], None, [], suppress_start=True)
    outcome, _state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_success" and walk.trace[-2:] == ["crawl_started", "test_run_only"]


# --------------------------------------------------- run form (rows 73-75)


def _inputs(variant: str) -> dict:
    return {r["name"]: r for r in SCRAPES[variant]["settings"]["callable"]["expected_inputs"]}


@pytest.mark.parametrize("variant", sorted(SCRAPES))
def test_politeness_page_timeout_and_busy_site_on_the_run_form(variant):
    rows = _inputs(variant)
    politeness = rows["politeness"]
    assert politeness["type"] == "select" and politeness["options"] == ["gentle", "normal", "fast"]
    for preset in (
        "gentle: pause 5-10 s, 1 page at a time, 10 requests a minute, page timeout 120 s",
        "normal: 2-5 s, 2 at a time, 30/min, 60 s",
        "fast: 0.5-1.5 s, 4 at a time, 120/min, 30 s",
        "Empty: normal.",
    ):
        assert preset in politeness["description"]
    timeout = rows["page_timeout_seconds"]
    assert timeout["type"] == "number" and "(10-300)" in timeout["description"]
    assert "Empty: from Politeness (normal: 60)." in timeout["description"]
    busy = rows["when_site_busy"]
    assert busy["type"] == "select" and busy["options"] == ["skip", "queue"]
    assert busy["description"].endswith("Empty: skip.")
    for name in ("delay_min_seconds", "delay_max_seconds", "max_concurrency"):
        assert "overrides Politeness" in rows[name]["description"], name
        assert "normally" not in rows[name]["description"], name
    names = list(rows)
    assert names.index("politeness") == names.index("delay_min_seconds") - 1
    validate = _step(SCRAPES[variant], "validate_settings")["input_mapping_jsonb"]
    for name in ("politeness", "page_timeout_seconds", "when_site_busy"):
        assert validate[name] == f"state.inputs.{name}|default:null"


def test_the_builder_form_offers_the_presets():
    fields = {f["name"]: f for f in FORMS["validate_config"]["fields"]}
    assert [o["value"] for o in fields["politeness"]["options"]] == ["gentle", "normal", "fast"]
    assert [o["value"] for o in fields["when_site_busy"]["options"]] == ["skip", "queue"]
    assert fields["page_timeout_seconds"]["widget_type"] == "integer"


def test_the_new_settings_reach_the_service(invoke, service):
    response = invoke(
        "validate_config",
        {
            "site_url": "https://www.lumen-shop.example/",
            "extraction_instructions": "Lamps",
            "politeness": "Gentle",
            "page_timeout_seconds": "120",
            "when_site_busy": "queue",
            "max_concurrency": None,
        },
    )
    assert response["ok"], response
    layer = service.calls("POST", "/v1/config/validate")[-1]["body"]["run_inputs"]
    assert layer["politeness"] == "gentle"
    assert layer["page_timeout_seconds"] == 120
    assert layer["when_site_busy"] == "queue"
    config = response["result"]["config"]
    assert config["politeness"] == "gentle" and config["when_site_busy"] == "queue"
    assert not service.schema_violations, service.schema_violations


# ------------------------------------------------------- run summary (72-74)


POLITENESS = {
    "preset": "normal",
    "text": "normal: 2–5 s, 2 at a time, 30/min, page timeout 60 s",
    "slowed_hosts": {
        "lumen-shop.example": {
            "factor": 2,
            "since": "2026-11-15T09:15:00Z",
            "reason": "6 errors in the last 20 pages",
        }
    },
}


def _counters(**extra) -> dict:
    return {**example("get_run.response.json")["counters"], **extra}


def test_politeness_and_unusable_answers_in_words():
    from handlers.runs import politeness_text, soft_errors_text

    run = {
        "politeness": POLITENESS,
        "counters": _counters(
            soft_errors=3,
            soft_errors_by_reason={"truncated": 2, "error_page": 1},
            repairs_avoided=1,
        ),
    }
    assert politeness_text(run) == (
        "Politeness: normal: 2–5 s, 2 at a time, 30/min, page timeout 60 s. Slowed down: "
        "lumen-shop.example (x2.0 since 09:15: 6 errors in the last 20 pages)."
    )
    assert soft_errors_text(run) == (
        "3 unusable answers from the site (status 200 but not the page) retried (cut off 2, "
        "error or maintenance page 1); 1 mapping repair avoided: the pages were fine when "
        "fetched again."
    )
    # A service before rc-next12: nothing to say.
    assert politeness_text({}) == "" and soft_errors_text({"counters": {}}) == ""


def test_the_run_summary_carries_them(invoke, service):
    service.run_patch = {
        "politeness": POLITENESS,
        "counters": _counters(soft_errors=3, soft_errors_by_reason={"empty": 3}, repairs_avoided=2),
    }
    result = invoke("get_run_summary", {"run_id": RUN})["result"]
    assert "Politeness: normal: 2–5 s" in result["message"]
    assert "3 unusable answers from the site" in result["message"]
    assert "2 mapping repairs avoided" in result["message"]
    assert result["politeness"]["preset"] == "normal"
    assert result["counters"]["soft_errors"] == 3
    assert result["counters"]["soft_errors_by_reason"] == {"empty": 3}
    assert result["counters"]["repairs_avoided"] == 2
    assert not service.response_violations, service.response_violations
    for variant in SCRAPES:
        mapping = _step(SCRAPES[variant], "check_run")["output_mapping_jsonb"]
        assert mapping["politeness"] == "state.run.politeness"


def test_run_detail_shows_them(query, service):  # noqa: F811
    service.run_patch = {
        "politeness": POLITENESS,
        "counters": _counters(soft_errors=1, soft_errors_by_reason={"too_small": 1}),
    }
    content = query("run_report", {"run_id": RUN})["result"]["content"]
    assert "- Politeness: normal: 2–5 s, 2 at a time, 30/min, page timeout 60 s." in content
    assert "Slowed down: lumen-shop.example (x2.0" in content
    assert (
        "- 1 unusable answer from the site (status 200 but not the page) retried (far too small 1)."
        in content
    )


def test_show_run_keeps_twelve_lines(invoke, service):
    service.run_patch = {
        "politeness": POLITENESS,
        "counters": _counters(soft_errors=2, repairs_avoided=1),
    }
    result = invoke("show_run", {"run_id": RUN})["result"]
    assert len([k for k in result if k != "details"]) == 12
    assert "Politeness: normal" in result["progress"]
    assert "2 unusable answers" in result["pages"]


# ------------------------------------------------- A67: cancel while waiting


def test_core_0918_has_no_cancel_hook_so_run_detail_names_it(query, service):  # noqa: F811
    service.run_patch = {
        "platform": WAITING_PLATFORM,
        "state": "running",
        "outcome": None,
        "stop_reason": None,
        "finished_at": None,
        "result": None,
    }
    content = query("run_report", {"run_id": RUN})["result"]["content"]
    assert "If that job (or the workflow that called it) was cancelled, this crawl still runs" in (
        content
    )
    assert "Stop it with *Stop* on its row on the [Runs](" in content
    # A finished run, or one started otherwise, needs no hint.
    service.run_patch = {"platform": WAITING_PLATFORM}
    assert (
        "was cancelled, this crawl" not in query("run_report", {"run_id": RUN})["result"]["content"]
    )
    service.run_patch = {"state": "running", "outcome": None, "stop_reason": None, "result": None}
    assert (
        "was cancelled, this crawl" not in query("run_report", {"run_id": RUN})["result"]["content"]
    )


def test_show_run_names_it_too(invoke, service):
    service.run_patch = {
        "platform": WAITING_PLATFORM,
        "state": "running",
        "outcome": None,
        "stop_reason": None,
        "result": None,
    }
    state = invoke("show_run", {"run_id": RUN})["result"]["state"]
    assert "If that job was cancelled, this crawl still runs" in state


def test_run_detail_help_and_guide_say_how_to_stop_it():
    help_text = (ROOT / "ui/components/run_detail_help.yaml").read_text()
    assert "Cancelling the" in help_text and "does not stop the crawl" in help_text
    guide = (ROOT / "docs/setup-guide.md").read_text(encoding="utf-8")
    assert "**Cancelling does not stop the crawl.** Flow Steward 0.9.18 tells an extension" in guide


# -------------------------------------------- prices (rows 69-71) and samples


@pytest.mark.parametrize(
    ("code", "plain"),
    [
        ("price_mismatch", "The price the mapping reads differs from the price the page states"),
        ("price_is_old_price", "On a sale page the mapping reads the old, struck-through price"),
    ],
)
def test_price_checks_in_plain_words(invoke, service, code, plain):
    gap = {
        "field": "mappings[0].fields[2]",
        "code": code,
        "message": "template product: `price` differs on 1 matched sample (smp_1: mapped 2499, "
        "the page states 24.99 (schema.org offers.price)); read the machine-readable price first",
    }
    service.override(
        "POST", r"^/v1/mappings/test$", 200, _test_answer(passed=False, failures=[gap])
    )
    result = invoke("test_mapping", _test_inputs())["result"]
    assert result["passed"] is False
    failure = next(f for f in result["failures"] if f["kind"] == "mapping_gap")
    assert failure["code"] == code and failure["message"] == gap["message"]
    assert failure["plain"].startswith(plain)
    assert failure["what_to_change"]
    line = result["templates"][0]["gaps"][0]
    assert line.startswith(plain) and line.endswith(f"Details: {gap['message']}")
    assert f"Not passed: product: {plain}" in result["summary"]


def test_repair_reason_names_the_price_checks():
    from handlers.mappings import GAP_FIXES
    from handlers.pages import MAPPING_GAP_TEXT

    assert "machine-readable" not in GAP_FIXES["price_not_extracted"]
    assert GAP_FIXES["price_mismatch"].startswith("read the price the page states for programs")
    assert "regular_price" in GAP_FIXES["price_is_old_price"]
    assert {"price_mismatch", "price_is_old_price"} <= set(MAPPING_GAP_TEXT)


def test_samples_carry_the_stated_price_and_the_sale():
    from handlers.mappings import _agent_sample
    from handlers.samples_file import render_samples

    sample = example("post_samples.response.json")["samples"][0]
    sample["price_details"] = {
        "machine_price": 24.0,
        "machine_source": "schema.org offers.price",
        "current": 24.0,
        "regular_price": 35.0,
        "on_sale": True,
        "sale_source": "struck-through price",
    }
    agent = _agent_sample(sample)
    assert agent["price_details"]["regular_price"] == 35.0
    text, _report = render_samples("lumen-shop.example", [agent])
    assert "- Price stated for programs: 24 (schema.org offers.price)" in text
    assert (
        "- On sale: current price 24, old price 35 (from struck-through price). Map the current "
        "price; the service records the old one as regular_price." in text
    )


def test_records_with_a_sale_are_valid_and_kept():
    record = json.loads(
        (ROOT / "tests/contract_examples/result.sample.jsonl").read_text().splitlines()[0]
    )
    record.update({"price_status": "shown", "regular_price": 449.0, "on_sale": True})
    assert not schema_errors("record.schema.json", record)


def test_the_skill_and_the_agent_read_the_machine_price_first():
    skill = yaml.safe_load((ROOT / "artifacts/skills/build_extraction_mapping.yaml").read_text())
    rules = " ".join(skill["instructions"])
    assert "Price source - machine-readable first" in rules
    for source in (
        "[itemprop=price]",
        "offers.price",
        "lowPrice",
        "product:price:amount",
        "data-price",
    ):
        assert source in rules, source
    assert "The visible price text only as a fallback" in rules
    assert "never map it as price - the service adds it as regular_price" in rules
    assert "price_mismatch" in rules and "price_is_old_price" in rules
    agent = (ROOT / "artifacts/agents/scraper_mapping_author.yaml").read_text()
    assert "the visible price text only as a fallback" in agent
    assert "price_is_old_price" in agent


# ------------------------------------------------------------- guide, version


@pytest.mark.parametrize(
    "words",
    [
        "the Web Scraper extension's from 0.1.24 on, or a copy",
        "*invoke.child_not_parent_workflow_triggerable*",
        "Put an **If** on `state.scrape.skipped`",
        "- **Politeness** (`politeness`)",
        "- **Page timeout (s)** (`page_timeout_seconds`)",
        "- **When the site is busy** (`when_site_busy`)",
        '**Scrape a site ended "Skipped: site busy"**',
        "`regular_price` (the old price) and `on_sale: true`",
        "`price_status`, `price_max`, `regular_price`,\n  `on_sale`, `currency_from_default`",
        "**With *price_is_old_price***",
        "- **Start skipped?**",
        "0.1.24 lets your own workflows call",
    ],
)
def test_guide(words):
    assert words in (ROOT / "docs/setup-guide.md").read_text(encoding="utf-8")
