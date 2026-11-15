"""0.1.9: every setting is a run input (A29), a Test Run ends plainly (A25), the final
message says where results appear (A28), and the product page selector binds the AI
mapping (A30)."""

from __future__ import annotations

import copy
import json

from conftest import make_payload, samples_descriptor
from fake_service import example
from test_templates import EVENTS, IMPL, SCRAPE, SCRAPE_INPUTS, _action, _connector
from workflow_sim import Walk

from handlers.mappings import selector_alternatives, target_selector_warnings
from handlers.router import handle_request

PRODUCT = example("mapping.product.json")
SPARE = example("mapping.spare_part.json")


# ------------------------------------------------------------- run inputs (A29)


def test_every_setting_travels_in_the_run_inputs_layer_typed(invoke, service):
    response = invoke(
        "validate_config",
        {
            "site_url": "https://www.lumen-shop.example/",
            "extraction_instructions": "Lamps: name, price.",
            "max_records": "3",
            "target_page_selector": " div.product-detail ",
            "url_include": None,
            "url_exclude": "/blog/\n/about",
            "discovery_mode": None,
            "debug": "true",
            "max_pages": "5000",
            "challenge_click": "false",
            "use_proxy_tiers": "1,2",
            "headers": "Accept-Language: de-DE",
            "delay_min_seconds": "1.5",
            "max_concurrency": "2",
            "structured_data_types": '["Product"]',
            "rebuild_mapping": "yes",
        },
    )
    assert response["ok"], response
    body = service.calls("POST", "/v1/config/validate")[0]["body"]
    layer = body["run_inputs"]
    assert layer["target_page_selector"] == "div.product-detail"
    assert layer["url_exclude"] == ["/blog/", "/about"]
    assert layer["max_records"] == 3 and layer["max_pages"] == 5000
    assert layer["debug"] is True and layer["challenge_click"] is False
    assert layer["use_proxy_tiers"] == [1, 2] and layer["max_concurrency"] == 2
    assert layer["headers"] == {"Accept-Language": "de-DE"}
    assert layer["delay_min_seconds"] == 1.5 and layer["structured_data_types"] == ["Product"]
    assert layer["rebuild_mapping"] is True
    assert layer["url_include"] is None  # empty: the profile's default
    assert body.get("site_settings") == {}
    result = response["result"]
    assert result["target_page_selector"] == "div.product-detail"
    assert result["rebuild_mapping"] is True
    assert not service.schema_violations


def test_the_run_form_wins_over_an_old_site_settings_step(invoke, service):
    response = invoke(
        "validate_config",
        {
            "site_url": "https://www.lumen-shop.example/",
            "max_pages": "300",
            "site_settings": {"max_pages": "9000", "target_page_selector": ".old"},
        },
    )
    assert response["ok"], response
    config = response["result"]["config"]
    assert config["max_pages"] == 300 and config["target_page_selector"] == ".old"


def test_a_run_with_every_field_empty_uses_the_defaults(service, tmp_path):
    walk = Walk(
        SCRAPE,
        connector=_connector(service, tmp_path),
        invoke=_action(service, tmp_path, []),
        child=IMPL,
    )
    outcome, _state = walk.run(
        {
            "site_url": SCRAPE_INPUTS["site_url"],
            "extraction_instructions": SCRAPE_INPUTS["extraction_instructions"],
        }
    )
    assert outcome == "stop_success", walk.failures
    layer = service.calls("POST", "/v1/config/validate")[0]["body"]["run_inputs"]
    assert all(
        value is None
        for key, value in layer.items()
        if key not in {"site_url", "extraction_instructions"}
    )


# -------------------------------------------------------- Test Run (A25), results (A28)


def _suppressing(service, tmp_path):
    """The host in a Test Run: `Start run` (an external effect) answers suppressed."""
    connector = _connector(service, tmp_path)

    def run(step, inputs):
        if step["override"]["connector"]["operation_id"] == "start_run":
            return {
                "test_mode_status": "suppressed",
                "external_effect_status": "suppressed",
                "definitely_no_external_effect": True,
            }
        return connector(step, inputs)

    return run


def test_a_test_run_ends_plainly_without_a_crawl(service, tmp_path):
    walk = Walk(
        SCRAPE,
        connector=_suppressing(service, tmp_path),
        invoke=_action(service, tmp_path, []),
        child=IMPL,
    )
    outcome, state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "stop_success", walk.failures
    assert walk.trace[-2:] == ["crawl_started", "test_run_only"]
    assert "scraper_run_id" not in state
    stop = next(s for s in SCRAPE["steps"] if s["step_id"] == "test_run_only")
    assert stop["override"]["stop_success"]["message"] == (
        "Test run: the mapping was built and tested; no crawl was started. Use Run workflow "
        "with the acknowledgement to scrape."
    )
    assert not service.calls("POST", "/v1/runs")


def test_without_the_branch_the_test_run_failed_on_the_missing_run_id(service, tmp_path):
    """Row 53: the 0.1.8 shape - success stop right after Start run, scraper_run_id required."""
    old = copy.deepcopy(SCRAPE)
    for edge in old["execution_graph"]["edges"]:
        if edge["source"]["id"] == "start_crawl":
            edge["target"]["id"] = "done"
    for row in old["settings"]["callable"]["expected_outputs"]:
        if row["name"] == "scraper_run_id":
            row["required"] = True
    walk = Walk(
        old,
        connector=_suppressing(service, tmp_path),
        invoke=_action(service, tmp_path, []),
        child=IMPL,
    )
    outcome, _state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "failed" and "scraper_run_id" in walk.failures[-1]


def test_a_real_run_names_its_run_id_and_where_the_result_appears(service, tmp_path):
    walk = Walk(
        SCRAPE,
        connector=_connector(service, tmp_path),
        invoke=_action(service, tmp_path, []),
        child=IMPL,
    )
    outcome, state = walk.run(dict(SCRAPE_INPUTS))
    # 0.1.23: the job waits for the run (the walker's default: it ends at once).
    assert outcome == "stop_success" and walk.trace[-1] == "done"
    assert "wait_for_run" in walk.trace
    run_id = state["scraper_run_id"]
    hint = state["run_page_hint"]
    assert run_id in hint and "Run detail" in hint and "Runs" in hint
    assert "Scrape a site (0.1.23 and later) waits" in hint and "result.jsonl" in hint
    assert "'Scraper events'" in hint  # an older copy's runs
    message = next(s for s in SCRAPE["steps"] if s["step_id"] == "done")["override"][
        "stop_success"
    ]["message"]
    for words in ("result.jsonl", "result_csv", "Result panel", "run_summary"):
        assert words in message
    outputs = {row["name"]: row for row in SCRAPE["settings"]["callable"]["expected_outputs"]}
    assert outputs["scraper_run_id"]["required"] is False


# ---------------------------------------------------- product page selector (A30)


def test_selector_lists_split_outside_brackets_and_quotes():
    assert selector_alternatives("[itemtype$='Product'], .part-sheet") == [
        "[itemtype$='Product']",
        ".part-sheet",
    ]
    # compared as text after normalising whitespace on both sides
    assert selector_alternatives("a[title='x, y'] >  b") == ["a[title='x,y']>b"]
    assert selector_alternatives(" ") == []


def test_a_template_without_the_product_page_selector_gets_a_warning():
    assert target_selector_warnings([PRODUCT, SPARE], "") == []
    assert (
        target_selector_warnings([PRODUCT, SPARE], "div.product-detail, section.part-sheet") == []
    )
    blog = copy.deepcopy(PRODUCT)
    blog["template_id"] = "page"
    blog["match"] = {"url_patterns": ["/[^/]+$"]}
    assert target_selector_warnings([PRODUCT, blog], "div.product-detail") == [
        {
            "kind": "target_page_selector",
            "template_id": "page",
            "message": "page: match.selector does not include the product page selector "
            "'div.product-detail' (it has none); the service still skips pages without it.",
        }
    ]
    inside = copy.deepcopy(PRODUCT)
    inside["match"]["selector"] = "main  >  div.product-detail"
    assert target_selector_warnings([inside], "div.product-detail") == []


def test_test_mapping_warns_but_does_not_fail_without_the_selector(invoke, service):
    """The service skips a page without the selector itself, so the test only warns."""
    sample_ids = ["smp_01JCX9Q1A2B3C4D5E6F7G8H9J0"]
    response = invoke(
        "test_mapping",
        {
            "site": "lumen-shop.example",
            "mappings_json": json.dumps([SPARE]),
            "sample_ids": sample_ids,
            "target_page_selector": "div.product-detail",
        },
    )
    assert response["ok"], response
    result = response["result"]
    assert result["passed"] is True and result["failures"] == []
    selector_rows = [r for r in result["warnings"] if r["kind"] == "target_page_selector"]
    assert [row["template_id"] for row in selector_rows] == ["spare_part"]
    assert (
        "Warning: spare_part: match.selector does not include the product page selector "
        "'div.product-detail' (it is 'section.part-sheet'); the service still skips pages "
        "without it." in result["summary"]
    )
    assert service.calls("POST", "/v1/mappings/test")
    plain = invoke(
        "test_mapping",
        {
            "site": "lumen-shop.example",
            "mappings_json": json.dumps([PRODUCT]),
            "sample_ids": sample_ids,
        },
    )["result"]
    assert all(row["kind"] != "target_page_selector" for row in plain["warnings"])
    assert "match.selector" not in plain["summary"]


def test_a_failed_candidate_hands_the_selector_warning_to_the_agent(invoke, service):
    failing = example("post_mappings_test.response.json")
    failing["passed"] = False
    service.override("POST", r"^/v1/mappings/test$", 200, failing)
    result = invoke(
        "test_mapping",
        {
            "site": "lumen-shop.example",
            "mappings_json": json.dumps([SPARE]),
            "sample_ids": ["smp_01JCX9Q1A2B3C4D5E6F7G8H9J0"],
            "target_page_selector": "div.product-detail",
        },
    )["result"]
    assert result["passed"] is False
    assert {"kind": "target_page_selector", "template_id": "spare_part"}.items() <= (
        result["failures"][-1].items()
    )


def test_the_agent_s_samples_file_states_the_selector(service, tmp_path):
    descriptor = samples_descriptor(tmp_path)
    samples = example("post_samples.response.json")["samples"]
    payload = make_payload(
        "write_samples_file",
        {"site": "lumen-shop.example", "samples": samples, "target_page_selector": ".product"},
        service_url=service.url,
        artifacts={"inputs": [], "outputs": [descriptor]},
    )
    response = handle_request(payload)
    assert response["ok"], response
    text = open(descriptor["_path"], encoding="utf-8").read()
    assert "Product page selector (set by the operator): .product" in text
    assert "Put it into every template's match.selector" in text


def test_the_selector_reaches_the_agent_and_every_test(service, tmp_path):
    seen: dict = {}
    connector = _connector(service, tmp_path)

    def recording(step, inputs):
        seen.setdefault(step["step_id"], inputs)
        return connector(step, inputs)

    def action(step, inputs):
        seen["build_mapping"] = inputs
        return _action(service, tmp_path, [PRODUCT, SPARE])(step, inputs)

    service.mappings.clear()
    walk = Walk(SCRAPE, connector=recording, invoke=action, child=IMPL)
    outcome, _state = walk.run(
        {**SCRAPE_INPUTS, "target_page_selector": "div.product-detail, section.part-sheet"}
    )
    assert outcome == "stop_success", walk.failures
    wanted = "div.product-detail, section.part-sheet"
    assert seen["build_mapping"]["target_page_selector"] == wanted
    assert seen["retest_mapping"]["target_page_selector"] == wanted
    impl = {s["step_id"]: s for s in IMPL["steps"]}
    for step_id in ("write_samples", "write_mapping", "test_candidate"):
        assert "target_page_selector" in impl[step_id]["input_mapping_jsonb"], step_id


def test_a_stored_mapping_is_not_rebuilt_when_a_selector_is_added(service, tmp_path):
    """The stored mappings (div.product-detail, section.part-sheet) still pass; the
    template without the selector is named in the summary instead."""
    walk = Walk(
        SCRAPE,
        connector=_connector(service, tmp_path),
        invoke=_action(service, tmp_path, []),
        child=IMPL,
    )
    outcome, state = walk.run({**SCRAPE_INPUTS, "target_page_selector": "div.product-detail"})
    assert outcome == "stop_success", walk.failures
    assert "build_mapping" not in walk.trace and "test_stored_mapping" in walk.trace
    assert "Warning: spare_part: match.selector does not include" in state["mapping_summary"]


def test_a_repair_passes_the_job_s_selector_on():
    steps = {s["step_id"]: s for s in EVENTS["steps"]}
    assert (
        steps["mapping_request"]["output_mapping_jsonb"]["target_page_selector"]
        == "state.request.target_page_selector"
    )
    assert (
        steps["repair_mapping"]["override"]["invoke_workflow"]["input_mapping"][
            "target_page_selector"
        ]
        == "state.request.target_page_selector"
    )


def test_get_mapping_request_returns_the_selector(invoke, service):
    request = example("get_mapping_request.response.json")
    result = invoke("get_mapping_request", {"mapping_request_id": request["mapping_request_id"]})[
        "result"
    ]
    assert result["target_page_selector"] == "[itemtype$='Product'], .part-sheet"


# ------------------------------------------------- fields never filled (A26)


def test_fields_never_filled_is_a_warning_in_the_summary(invoke, service):
    answer = example("post_mappings_test.response.json")
    answer["warnings"] = [
        {
            "field": "mappings[1].fields",
            "code": "fields_never_filled",
            "message": "template spare_part: image_urls filled on 0 of 1 matched samples (the "
            "test passed on the required fields: title, price, currency); check the mapping's "
            "sources, or whether the samples are really target pages",
        }
    ]
    from fake_service import schema_errors

    assert not schema_errors("mapping_test.schema.json#/$defs/response", answer)
    service.override("POST", r"^/v1/mappings/test$", 200, answer)
    result = invoke(
        "test_mapping",
        {
            "site": "lumen-shop.example",
            "mappings_json": json.dumps([PRODUCT, SPARE]),
            "sample_ids": ["smp_01JCX9Q1A2B3C4D5E6F7G8H9J0"],
            "target_page_selector": "div.product-detail",
        },
    )["result"]
    assert result["passed"] is True
    kinds = [(row["kind"], row["template_id"]) for row in result["warnings"]]
    assert kinds == [("fields_never_filled", "spare_part"), ("target_page_selector", "spare_part")]
    assert result["warnings"][0]["message"].endswith(
        "really target pages. The crawl would leave these fields empty; if the samples are "
        "not product pages, set the product page selector."
    )
    assert "Warning: template spare_part: image_urls filled on 0 of 1" in result["summary"]
    assert [t["empty_fields"] for t in result["templates"]] == [[], ["image_urls"]]


def test_empty_fields_without_the_service_warning_still_warn(invoke, service):
    """The contract example has empty_fields but no warnings[]: the extension words it."""
    result = invoke(
        "test_mapping",
        {
            "site": "lumen-shop.example",
            "mappings_json": json.dumps([PRODUCT, SPARE]),
            "sample_ids": ["smp_01JCX9Q1A2B3C4D5E6F7G8H9J0"],
        },
    )["result"]
    n = result["templates"][1]["matched_samples"]
    assert result["warnings"] == [
        {
            "kind": "fields_never_filled",
            "template_id": "spare_part",
            "message": "template spare_part: image_urls filled on 0 of "
            f"{n} matched sample{'s' if n != 1 else ''}; check the mapping's "
            "sources. The crawl would leave these fields empty; if the samples are not product "
            "pages, set the product page selector.",
        }
    ]
    assert "Warning: template spare_part" in result["summary"]


def test_browser_os_and_canvas_noise_follow_browser_on_the_run_form(invoke, service):
    rows = SCRAPE["settings"]["callable"]["expected_inputs"]
    names = [row["name"] for row in rows]
    assert names[names.index("browser") + 1 : names.index("browser") + 3] == [
        "browser_os",
        "canvas_noise",
    ]
    by_name = {row["name"]: row for row in rows}
    assert by_name["browser_os"]["options"] == ["windows", "macos", "linux", "host"]
    assert by_name["canvas_noise"]["options"] == ["auto", "off", "on"]
    # Owner's matrix (row 64): the host OS passes Cloudflare, every Windows claim fails.
    assert "host (the default)" in by_name["browser_os"]["description"]
    assert "usually refused" in by_name["browser_os"]["description"]
    assert by_name["browser_os"]["description"].endswith("Empty: host.")
    for name in ("browser_os", "canvas_noise"):
        assert "default" not in by_name[name] and "Camoufox only" in (by_name[name]["description"])
    response = invoke(
        "validate_config",
        {
            "site_url": "https://www.lumen-shop.example/",
            "browser_os": " Linux ",
            "canvas_noise": "OFF",
        },
    )
    assert response["ok"], response
    layer = service.calls("POST", "/v1/config/validate")[0]["body"]["run_inputs"]
    assert layer["browser_os"] == "linux" and layer["canvas_noise"] == "off"
    assert not service.schema_violations


# ------------------------------------------------------- empty result (A36)

RUN = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"


def _empty_run() -> dict:
    """Row 70: the owner stopped the run after 18 pages; no record was extracted."""
    run = example("get_run.response.json")
    run["state"] = "completed"
    run["outcome"] = "partial"
    run["stop_reason"] = "stopped"
    run["counters"]["pages_fetched"] = 18
    run["counters"]["records_extracted"] = 0
    for template in run["templates"]:
        template["record_count"] = 0
    if run.get("result"):
        run["result"].update({"record_count": 0, "size_bytes": 0, "sha256": EMPTY_SHA})
        for row in run["result"].get("templates", []):
            row["record_count"] = 0
    return run


EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_a_run_without_records_is_handled_without_get_result(service, tmp_path):
    from fake_service import schema_errors
    from test_templates import EVENT_INPUTS, ROOT_SCOPE, _tabular

    run = _empty_run()
    assert not schema_errors("run_summary.schema.json", run)
    service.run_patch = run  # every read: Claim event reads the run too (0.1.23)
    reads: list = []
    scope = {**ROOT_SCOPE, "step_id": "claim_event"}
    walk = Walk(EVENTS, connector=_connector(service, tmp_path, scope), tabular=_tabular(reads))
    outcome, state = walk.run(dict(EVENT_INPUTS))
    assert outcome == "stop_success" and walk.trace[-1] == "handled", walk.failures
    assert "get_result" not in walk.trace and "read_result" not in walk.trace and reads == []
    assert walk.trace[-5:] == [
        "has_records",
        "mark_processed",
        "mark_completed",
        "complete_event",
        "handled",
    ]
    assert state["run"]["has_records"] is False and state["run"]["record_count"] == 0
    assert state["run"]["no_records_message"] == (
        "The run produced no records from 18 pages loaded: it was stopped by hand. There is "
        "no result file; nothing was stored."
    )
    assert not service.calls("GET", f"/v1/runs/{RUN}/result")
    stages = [
        c["body"]["stage"]
        for c in service.calls("POST", "/v1/events/")
        if c["path"].endswith("/stage")
    ]
    assert stages == ["processed", "completed"]
    assert service.calls("POST", "/v1/events/") and service.calls("POST", "/v1/events/")[-1][
        "path"
    ].endswith("/complete")


def test_a_run_with_records_still_gets_its_result(service, tmp_path):
    from test_templates import EVENT_INPUTS, ROOT_SCOPE, _tabular

    reads: list = []
    scope = {**ROOT_SCOPE, "step_id": "claim_event"}
    walk = Walk(EVENTS, connector=_connector(service, tmp_path, scope), tabular=_tabular(reads))
    outcome, state = walk.run(dict(EVENT_INPUTS))
    assert outcome == "stop_success" and "get_result" in walk.trace
    assert state["run"]["has_records"] is True and state["run"]["no_records_message"] == ""


def test_get_result_on_an_empty_run_says_so_instead_of_uploading_nothing(invoke, service):
    descriptor = example("get_run_result.response.json")
    descriptor.update(
        {"record_count": 0, "size_bytes": 0, "sha256": EMPTY_SHA, "stop_reason": "stopped"}
    )
    for row in descriptor.get("templates", []):
        row["record_count"] = 0
    service.override("GET", rf"^/v1/runs/{RUN}/result$", 200, descriptor)
    response = invoke("get_result", {"run_id": RUN})
    assert response["error_code"] == "result_empty"
    assert response["error"].startswith(
        "The run produced no records (stop reason stopped), so there is no result file to store."
    )
    assert not service.calls("GET", f"/v1/runs/{RUN}/result/download")


def test_stop_reasons_read_as_words():
    from fake_service import SCHEMAS

    from handlers.runs import STOP_REASON_TEXT

    contract = json.loads((SCHEMAS / "run_summary.schema.json").read_text(encoding="utf-8"))
    assert set(STOP_REASON_TEXT) == set(contract["$defs"]["stop_reason"]["enum"])
