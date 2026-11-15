"""validate_config, analyze_site and the run life cycle."""

from __future__ import annotations

from conftest import ROOT_SCOPE, input_descriptor, profile_config
from fake_service import example


def _validated(invoke, **inputs):
    response = invoke(
        "validate_config",
        {
            "site_url": "https://www.lumen-shop.example/",
            "extraction_instructions": "Lamps: name, price, article number.",
            "fields": "title, price, currency, part_number",
            "required_fields": ["title", "price"],
            "max_records": "10",
            "site_settings": {
                "sitemap_url": "",
                "max_pages": "5000",
                "challenge_click": "false",
                "use_proxy_tiers": "1,2",
                "headers": "Accept-Language: de-DE",
                "delay_min_seconds": "",
            },
            **inputs,
        },
    )
    return response


def test_validate_merges_layers_and_coerces_canvas_values(invoke, service):
    response = _validated(invoke)
    assert response["ok"] is True, response
    result = response["result"]
    assert result["valid"] is True
    assert result["site"] == "lumen-shop.example"
    assert result["config"]["max_records"] == 10
    body = service.calls("POST", "/v1/config/validate")[0]["body"]
    assert body["run_inputs"]["fields"] == ["title", "price", "currency", "part_number"]
    assert body["run_inputs"]["max_records"] == 10
    settings = body["site_settings"]
    assert settings["max_pages"] == 5000 and settings["challenge_click"] is False
    assert settings["use_proxy_tiers"] == [1, 2]
    assert settings["headers"] == {"Accept-Language": "de-DE"}
    assert settings["sitemap_url"] == "" and settings["delay_min_seconds"] == ""
    # Profile defaults travel only to validate.
    assert body["profile"]["defaults"]["max_concurrency"] == 4
    assert body["profile"]["webhook"]["url"].startswith("https://flow.example.com")
    assert not service.schema_violations


def test_debug_from_the_run_form_wins_over_the_site_setting(invoke, service):
    response = _validated(invoke, debug="true", site_settings={"debug": "false"})
    assert response["ok"] is True, response
    body = service.calls("POST", "/v1/config/validate")[0]["body"]
    # The service merges run_inputs before site_settings, so the run form decides.
    assert body["run_inputs"]["debug"] is True
    assert body["site_settings"]["debug"] is False
    assert not service.schema_violations


def test_without_debug_on_the_run_form_the_site_setting_applies(invoke, service):
    response = _validated(invoke, site_settings={"debug": "true"})
    assert response["ok"] is True, response
    body = service.calls("POST", "/v1/config/validate")[0]["body"]
    assert "debug" not in body["run_inputs"]
    assert body["site_settings"]["debug"] is True


def test_value_above_a_service_limit_names_the_field(invoke, service):
    response = _validated(invoke, site_settings={"max_pages": 250000})
    assert response["ok"] is False
    assert response["error_code"] == "config_invalid"
    assert "max_pages" in response["error"] and "100000" in response["error"]
    assert response["result"]["errors"][0]["field"] == "max_pages"
    assert response.get("definitely_no_external_effect") is True


def test_unknown_site_setting_is_refused_before_calling_the_service(invoke, service):
    response = _validated(invoke, site_settings={"max_page": 10})
    assert response["error_code"] == "invalid_payload"
    assert "max_page" in response["error"]
    assert not service.calls("POST", "/v1/config/validate")


def test_urls_and_url_file_become_the_contract_list(invoke, service, tmp_path):
    csv_file = tmp_path / "articles.csv"
    csv_file.write_text(
        "url;key\nhttps://www.lumen-shop.example/ersatzteile/a;A-1\n"
        "https://www.lumen-shop.example/ersatzteile/b;A-2\n"
        "https://www.lumen-shop.example/ersatzteile/a;A-1\n",
        encoding="utf-8",
    )
    response = invoke(
        "validate_config",
        {
            "discovery_mode": "url_list",
            "urls": [
                "https://www.lumen-shop.example/produkt/x",
                "https://www.lumen-shop.example/produkt/x",
            ],
            "urls_file": {"artifact_handle": "artifact:urls_in"},
            "site_settings": {},
        },
        artifacts={"inputs": [input_descriptor(csv_file)], "outputs": []},
    )
    assert response["ok"] is True, response
    urls = service.calls("POST", "/v1/config/validate")[0]["body"]["run_inputs"]["urls"]
    assert urls == [
        {"url": "https://www.lumen-shop.example/produkt/x"},
        {"url": "https://www.lumen-shop.example/ersatzteile/a", "key": "A-1"},
        {"url": "https://www.lumen-shop.example/ersatzteile/b", "key": "A-2"},
    ]
    assert response["result"]["is_url_list"] is True
    assert response["result"]["url_count"] == 3


def test_plain_url_file_and_bad_lines(invoke, service, tmp_path):
    plain = tmp_path / "urls.txt"
    plain.write_text("https://a.example/1\n\n# comment\nhttps://a.example/2\n", encoding="utf-8")
    response = invoke(
        "validate_config",
        {"discovery_mode": "url_list", "urls_file": "artifact:urls_in"},
        artifacts={"inputs": [input_descriptor(plain)], "outputs": []},
    )
    assert response["ok"] is True, response
    bad = tmp_path / "bad.txt"
    bad.write_text("https://a.example/1\nftp://nope\nnot a url\n", encoding="utf-8")
    response = invoke(
        "validate_config",
        {"discovery_mode": "url_list", "urls_file": "artifact:urls_in"},
        artifacts={"inputs": [input_descriptor(bad)], "outputs": []},
    )
    assert response["error_code"] == "urls_file_invalid"
    assert "line 2" in response["error"] and "line 3" in response["error"]


def test_url_list_above_the_service_cap(invoke, service):
    service.limits_patch = {"max_url_list_entries": 2}
    response = invoke(
        "validate_config",
        {"discovery_mode": "url_list", "urls": [f"https://a.example/{i}" for i in range(3)]},
    )
    assert response["error_code"] == "url_list_too_long"
    assert "at most 2" in response["error"]


def _config(invoke):
    return _validated(invoke)["result"]["config"]


def test_analyze_recommends_when_auto(invoke, service):
    config = _config(invoke)
    config["discovery_mode"] = "auto"
    response = invoke("analyze_site", {"config": config})
    assert response["ok"] is True, response
    assert response["result"]["discovery_mode"] == "sitemap_and_crawl"
    assert response["result"]["protection"] == "challenge"
    assert "webhook" not in service.calls("POST", "/v1/analyze")[0]["body"]["profile"]
    config["discovery_mode"] = "crawl"
    assert invoke("analyze_site", {"config": config})["result"]["discovery_mode"] == "crawl"
    assert invoke("analyze_site", {})["error_code"] == "invalid_payload"


def test_start_run_is_idempotent_per_job_and_step(invoke, service):
    config = _config(invoke)
    first = invoke("start_run", {"config": config})
    assert first["ok"] is True, first
    again = invoke("start_run", {"config": config})
    assert again["result"]["run_id"] == first["result"]["run_id"]
    assert first["result"]["created"] is True and again["result"]["created"] is False
    keys = {call["headers"]["idempotency-key"] for call in service.calls("POST", "/v1/runs")}
    assert keys == {f"fs:{ROOT_SCOPE['job_id']}:start_run"}
    body = service.calls("POST", "/v1/runs")[0]["body"]
    assert body["platform"]["job_id"] == ROOT_SCOPE["job_id"]
    assert body["platform"]["connection_id"] == "conn_scraper_1"
    assert "defaults" not in body["profile"]
    assert not service.schema_violations
    # A rework re-enters the step with a new execution id: still the same run.
    scope = {**ROOT_SCOPE, "step_execution_id": "2b0f6c1e-8d3a-4c55-9a51-0d6b1f1f0099"}
    assert (
        invoke("start_run", {"config": config}, scope=scope)["result"]["run_id"]
        == first["result"]["run_id"]
    )


def test_same_step_with_another_configuration_is_refused(invoke, service):
    config = _config(invoke)
    assert invoke("start_run", {"config": config})["ok"] is True
    changed = {**config, "max_pages": 10}
    response = invoke("start_run", {"config": changed})
    assert response["error_code"] == "idempotency_key_reused"
    assert "already started a run" in response["error"]


def _host_scope(index, *, step_execution_id, attempt=1, group_id=None, for_each_id="each_site"):
    """`runtime_context` as the host serialises it (runtime_identity.as_context on
    flow-steward/main: loop = {for_each_id, index} of the innermost For each)."""
    return {
        **ROOT_SCOPE,
        "tenant_id": "acc_1",
        "workspace_id": "",
        "step_execution_id": step_execution_id,
        "attempt": attempt,
        "group_id": group_id,
        "for_each_id": for_each_id,
        "loop": {} if index is None else {"for_each_id": "each_site", "index": index},
        "test_mode": False,
    }


def test_loop_index_makes_its_own_run(invoke, service):
    config = _config(invoke)
    first = _host_scope(0, step_execution_id="aaaaaaaa-0000-4000-8000-000000000001")
    second = _host_scope(1, step_execution_id="aaaaaaaa-0000-4000-8000-000000000002")
    one = invoke("start_run", {"config": config}, scope=first)
    two = invoke("start_run", {"config": config}, scope=second)
    assert one["ok"] and two["ok"], (one, two)
    assert one["result"]["run_id"] != two["result"]["run_id"]
    assert one["result"]["idempotency_key"].endswith(":start_run:0")
    assert two["result"]["idempotency_key"].endswith(":start_run:1")
    platforms = [call["body"]["platform"] for call in service.calls("POST", "/v1/runs")]
    assert [p["loop_position"] for p in platforms] == ["0", "1"]
    # A retry or re-entry of item 0 gets a new step_execution_id but the same index:
    # same key, so the service hands back the run it already started.
    again = invoke(
        "start_run",
        {"config": config},
        scope=_host_scope(0, step_execution_id="aaaaaaaa-0000-4000-8000-0000000000ff", attempt=2),
    )
    assert again["result"]["run_id"] == one["result"]["run_id"]
    # A step in a Group inside the For each: the immediate owner is the Group, the
    # loop still names the item.
    grouped = _host_scope(
        2, step_execution_id="aaaaaaaa-0000-4000-8000-000000000003", group_id="g", for_each_id=None
    )
    assert invoke("start_run", {"config": config}, scope=grouped)["result"][
        "idempotency_key"
    ].endswith(":2")


def test_outside_a_loop_there_is_no_position(invoke, service):
    config = _config(invoke)
    root = _host_scope(
        None, step_execution_id="aaaaaaaa-0000-4000-8000-000000000001", for_each_id=None
    )
    response = invoke("start_run", {"config": config}, scope=root)
    assert response["ok"], response
    assert response["result"]["idempotency_key"].endswith(":start_run")
    assert "loop_position" not in service.calls("POST", "/v1/runs")[0]["body"]["platform"]


def test_a_for_each_without_an_index_is_refused(invoke, service):
    config = _config(invoke)
    scope = _host_scope(None, step_execution_id="aaaaaaaa-0000-4000-8000-000000000001")
    response = invoke("start_run", {"config": config}, scope=scope)
    assert response["ok"] is False and response["error_code"] == "invalid_host_envelope"
    assert not service.calls("POST", "/v1/runs")


def test_start_without_webhook_explains_setup_step(invoke, service):
    config = _config(invoke)
    response = invoke(
        "start_run", {"config": config}, config=profile_config(service.url, webhook_url="")
    )
    assert response["error_code"] == "invalid_connection"
    assert "Scraper events" in response["error"]
    assert not service.calls("POST", "/v1/runs")


def test_start_outside_a_workflow_is_refused(invoke, service):
    config = _config(invoke)
    response = invoke(
        "start_run",
        {"config": config},
        scope={**ROOT_SCOPE, "job_id": "", "step_id": None, "step_execution_id": None},
    )
    assert response["error_code"] == "invalid_payload"


def test_start_timeout_says_the_run_may_exist(invoke, service):
    config = _config(invoke)
    service.delays[("POST", r"^/v1/runs$")] = 12
    response = invoke("start_run", {"config": config})
    service.delays.clear()
    assert response["error_code"] == "service_timeout"
    assert response.get("retryable") is True


def test_service_refuses_merged_config_on_start(invoke, service):
    config = _config(invoke)
    service.override("POST", r"^/v1/runs$", 422, example("error.validation_failed.json"))
    response = invoke("start_run", {"config": config})
    assert response["error_code"] == "config_invalid"
    assert "Problems:" in response["error"]


def test_stop_resume_and_summary(invoke, service):
    stopped = invoke("stop_run", {"run_id": "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"})
    assert stopped["ok"] is True, stopped
    resumed = invoke("resume_run", {"run_id": "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"})
    assert resumed["ok"] is True
    body = service.calls("POST", "/v1/runs/run_01JCXA7Q3M5VZ8K2N4P6R8T0W1/resume")[0]["body"]
    assert body["profile"]["webhook"]["signing_secret"]
    summary = invoke("get_run_summary", {"run_id": "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"})
    assert summary["ok"] is True
    result = summary["result"]
    assert result["outcome"] == "complete" and result["terminal"] is True
    assert {t["template_id"] for t in result["templates"]} == {"product", "spare_part"}
    assert "config" not in result and "profile" not in result
    assert not service.schema_violations


def test_unknown_run(invoke, service):
    service.override(
        "GET",
        r"^/v1/runs/run_",
        404,
        {"error": {"code": "run_not_found", "message": "no run", "request_id": "req_1"}},
    )
    response = invoke("get_run_summary", {"run_id": "run_01NOPE"})
    assert response["error_code"] == "run_not_found"
    assert "(request req_1)" in response["error"]
    assert invoke("stop_run", {})["error_code"] == "invalid_payload"
