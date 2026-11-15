"""Connection form checks against the fake service."""

from __future__ import annotations

import pytest
from conftest import SECRETS, profile_config


def test_service_check_reports_version_browsers_and_limits(invoke, service):
    response = invoke("test_connection")
    assert response["ok"] is True, response
    result = response["result"]
    assert result["status"] in {"ok", "degraded"}
    assert "v1" in result["api_versions"]
    assert result["proxy_tiers_configured"] == 3
    assert result["solver_configured"] == "capsolver"
    call = service.calls("GET", "/health")[0]
    assert call["headers"]["authorization"] == f"Bearer {SECRETS['service_token']}"
    assert not service.schema_violations


def test_wrong_token_is_a_clear_error(invoke):
    response = invoke(
        "test_connection", secrets={**SECRETS, "service_token": "wrong-token-value-1234"}
    )
    assert response["ok"] is False
    assert response["error_code"] == "service_unauthorized"
    assert "token" in response["error"]


def test_missing_service_url_and_token(invoke, service):
    response = invoke("test_connection", config=profile_config(""))
    assert response["error_code"] == "invalid_connection"
    assert "service URL" in response["error"]
    secrets = {k: v for k, v in SECRETS.items() if k != "service_token"}
    response = invoke("test_connection", secrets=secrets)
    assert response["error_code"] == "invalid_connection"
    assert "token" in response["error"]


def test_service_url_with_credentials_is_refused(invoke, service):
    url = service.url.replace("http://", "http://user:pw@")
    response = invoke("test_connection", config=profile_config(url))
    assert response["error_code"] == "invalid_connection"


def test_unreachable_service(invoke):
    response = invoke("test_connection", config=profile_config("http://127.0.0.1:9"))
    assert response["ok"] is False
    assert response["error_code"] == "service_unreachable"
    assert response.get("retryable") is True


def test_private_service_needs_the_platform_switch(invoke, service, monkeypatch):
    monkeypatch.delenv("FS_ALLOW_PRIVATE_REMOTE_URLS")
    response = invoke("test_connection")
    assert response["error_code"] == "network_blocked"
    assert "FS_ALLOW_PRIVATE_REMOTE_URLS" in response["error"]


def test_service_without_v1_is_refused(invoke, service):
    from fake_service import example

    health = example("get_health.response.json")
    health["service"]["api_versions"] = ["v2"]
    service.override("GET", r"^/health$", 200, health)
    response = invoke("test_connection")
    assert response["error_code"] == "service_api_unsupported"


def test_every_proxy_tier_is_tested_without_leaking_credentials(invoke, service):
    response = invoke("test_proxy")
    assert response["ok"] is True, response
    assert response["result"]["tiers_tested"] == 3
    bodies = [call["body"] for call in service.calls("POST", "/v1/proxies/test")]
    assert [body["tier"] for body in bodies] == [1, 2, 3]
    tiers = {t["tier"]: t for t in bodies[0]["profile"]["proxy_tiers"]}
    # The preset filled the Oxylabs gateway; the country was upper-cased.
    assert tiers[2]["host"] == "pr.oxylabs.io" and tiers[2]["port"] == 7777
    assert tiers[2]["country"] == "DE" and tiers[2]["sticky_minutes"] == 30
    assert tiers[1]["auth_mode"] == "ip_allowlist" and "password" not in tiers[1]
    assert len(tiers[3]["proxy_urls"]) == 2
    assert "webhook" not in bodies[0]["profile"]
    assert not service.schema_violations


def test_one_tier_and_a_failing_tier(invoke, service):
    from fake_service import example

    response = invoke("test_proxy", {"tier": "2"})
    assert response["ok"] is True
    assert response["result"]["tiers_tested"] == 1
    failed = example("post_proxies_test.response.failed.json")
    service.override("POST", r"^/v1/proxies/test$", 200, failed)
    response = invoke("test_proxy", {"tier": failed["tier"]})
    assert response["ok"] is False
    assert response["error_code"] == "proxy_check_failed"
    assert "FAILED" in response["error"]


def test_unconfigured_tier(invoke, service):
    config = profile_config(service.url, proxy_tier3_provider="none")
    response = invoke("test_proxy", {"tier": 3}, config=config)
    assert response["error_code"] == "proxy_tier_not_configured"
    config = profile_config(
        service.url,
        proxy_tier1_provider="none",
        proxy_tier2_provider="none",
        proxy_tier3_provider="none",
    )
    response = invoke("test_proxy", config=config)
    assert response["error_code"] == "proxy_tier_not_configured"


def test_solver_check(invoke, service):
    response = invoke("test_captcha_solver")
    assert response["ok"] is True, response
    assert response["result"]["provider"] == "capsolver"
    service.override(
        "POST",
        r"^/v1/solvers/test$",
        200,
        {
            "ok": False,
            "checked_at": "2026-11-13T10:00:00Z",
            "latency_ms": 120,
            "code": "solver_auth_failed",
            "message": "key refused",
            "provider": "capsolver",
            "balance": None,
            "balance_currency": None,
        },
    )
    response = invoke("test_captcha_solver")
    assert response["error_code"] == "solver_check_failed"
    assert "refused the API key" in response["error"]
    response = invoke(
        "test_captcha_solver", config=profile_config(service.url, solver_provider="none")
    )
    assert response["error_code"] == "solver_not_configured"


def test_solver_without_key_is_an_incomplete_profile(invoke):
    secrets = {k: v for k, v in SECRETS.items() if k != "solver_api_key"}
    response = invoke("test_captcha_solver", secrets=secrets)
    assert response["error_code"] == "invalid_connection"


def test_every_tier_choice_and_any_filters(invoke, service):
    assert invoke("test_proxy", {"tier": "all"})["result"]["tiers_tested"] == 3
    invoke("list_runs", {"state_filter": "any"})
    assert "state" not in service.calls("GET", "/v1/runs")[0]["query"]
    invoke("show_unextracted", {"run_id": "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1", "reason": "any"})
    assert (
        "reason"
        not in service.calls("GET", "/v1/runs/run_01JCXA7Q3M5VZ8K2N4P6R8T0W1/unextracted")[0][
            "query"
        ]
    )


def test_service_check_names_the_browsers_of_the_health_list(invoke, service):
    """m1: `/health` `browsers` is a list of {name, present, version}."""
    from fake_service import example

    health = example("get_health.response.json")
    assert isinstance(health["browsers"], list)
    response = invoke("test_connection")
    expected = sorted(f"{b['name']} {b['version']}" for b in health["browsers"] if b["present"])
    assert response["result"]["browsers"] == ", ".join(expected)
    health["browsers"][1]["present"] = False
    service.override("GET", r"^/health$", 200, health)
    assert invoke("test_connection")["result"]["browsers"] == expected[0]


def test_action_parameters_accept_what_the_profile_form_sends():
    """B5: the platform's action form coerces a parameter by its declared type
    (manifest-action-support.ts: integer/number -> Number(value), NaN refused), so
    every default and option of a form field with that name must survive it."""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    actions = yaml.safe_load((root / "ui/actions/actions.yaml").read_text())
    form = yaml.safe_load((root / "ui/components/profile_connection_form.yaml").read_text())
    rows = actions if isinstance(actions, list) else actions.get("actions", actions)
    rows = rows if isinstance(rows, list) else rows.get("action_definitions", [])
    fields = {}

    def collect(node):
        if isinstance(node, dict):
            if "path" in node and "widget" in node:
                fields[node["path"]] = node
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(form)
    defaults = {}

    def collect_defaults(node):
        if isinstance(node, dict):
            if isinstance(node.get("defaults"), dict):
                defaults.update(node["defaults"])
            for value in node.values():
                collect_defaults(value)
        elif isinstance(node, list):
            for value in node:
                collect_defaults(value)

    collect_defaults(form)
    checked = 0
    for action in rows:
        for parameter in action.get("parameters") or []:
            field = fields.get(parameter["name"])
            if field is None:
                continue
            values = [option["value"] for option in field.get("options") or []]
            if parameter["name"] in defaults:
                values.append(defaults[parameter["name"]])
            for value in values:
                checked += 1
                if parameter["type"] in {"integer", "number"}:
                    float(str(value))  # would be NaN in the browser
    assert checked >= 5  # the tier select at least
    tier = next(p for a in rows for p in a.get("parameters") or [] if p["name"] == "tier")
    assert tier["type"] == "string" and defaults.get("tier") == "all"


def test_tier_all_and_a_single_tier_as_strings(invoke, service):
    assert invoke("test_proxy", {"tier": "all"})["result"]["tiers_tested"] == 3
    assert invoke("test_proxy", {"tier": "3"})["result"]["tiers_tested"] == 1
    assert invoke("test_proxy", {"tier": "x"})["error_code"] == "invalid_payload"


@pytest.mark.parametrize(("configured", "sent"), [(0, 0), ("0", 0), ("", 20), (None, 20), (7, 7)])
def test_solver_spending_guard_keeps_zero(invoke, service, configured, sent):
    """m2: 0 means no paid solve in a run; only an empty field is the default 20."""
    config = profile_config(service.url, solver_max_solves_per_job=configured)
    assert invoke("test_captcha_solver", config=config)["ok"] is True
    body = service.calls("POST", "/v1/solvers/test")[-1]["body"]
    assert body["profile"]["solver"]["max_solves_per_job"] == sent


def test_unsaved_profile_form_says_save_first(invoke):
    """m4: the renderer sends an empty target for a form that was never saved."""
    for action in ("test_proxy", "test_captcha_solver", "test_connection"):
        response = invoke(action, connection=False)
        assert response["error_code"] == "invalid_connection"
        assert "save the profile first" in response["error"]


def test_proxy_password_is_scrubbed_in_any_form(invoke, service):
    """m7: a message that quotes a listed proxy without its scheme keeps no password."""
    from handlers.profile import proxy_url_secrets

    assert proxy_url_secrets("http://listuser:list-pass-secret-77@10.1.2.3:3128") == [
        "listuser:list-pass-secret-77",
        "listuser:list-pass-secret-77",
        "list-pass-secret-77",
        "list-pass-secret-77",
    ]
    assert proxy_url_secrets("http://10.1.2.3:3128") == []
    assert "p@ss word" in proxy_url_secrets("http://u:p%40ss%20word@h:1")
    service.override(
        "POST",
        r"^/v1/proxies/test$",
        200,
        {
            "tier": 3,
            "ok": False,
            "checked_at": "2026-11-13T10:00:00Z",
            "code": "proxy_auth_failed",
            "message": "proxy listuser:list-pass-secret-77@10.1.2.3 refused; password list-pass-secret-77",
            "exit_ip": None,
            "country": None,
            "latency_ms": None,
            "city": None,
            "target_http_status": None,
        },
    )
    response = invoke("test_proxy", {"tier": "3"})
    assert response["ok"] is False
    assert "list-pass-secret-77" not in str(response) and "[redacted]" in str(response)


def test_untouched_tier_selects_mean_what_the_form_shows(service):
    """An unset select shows its first option; the profile must use exactly that."""
    from pathlib import Path

    import yaml

    from handlers.profile import profile_from_connection

    form = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1] / "ui/components/profile_connection_form.yaml"
        ).read_text()
    )
    text = yaml.safe_dump(form)
    first = {}
    for n in (1, 2, 3):
        for name in ("protocol", "auth_mode", "session_mode"):
            path = f"proxy_tier{n}_{name}"
            assert f"{path}:" in text
            first[name] = name
    config = profile_config(
        service.url,
        proxy_tier1_provider="generic",
        proxy_tier1_host="gw.example.net",
        proxy_tier1_port=8000,
        proxy_tier1_auth_mode=None,
    )
    for key in list(config):
        if key.startswith("proxy_tier1_") and key.endswith(("protocol", "session_mode")):
            config.pop(key)
    profile = profile_from_connection(
        {
            "connection_id": "c",
            "config": config,
            "secrets": {**SECRETS, "proxy_tier1_username": "uuuu", "proxy_tier1_password": "pppp1"},
        }
    )
    tier = profile.proxy_tiers[0]
    assert (tier["protocol"], tier["auth_mode"], tier["session_mode"]) == (
        "http",
        "user_password",
        "rotating",
    )

    def options(field_id):
        def walk(node):
            if isinstance(node, dict):
                if node.get("field_id") == field_id:
                    return node
                for value in node.values():
                    found = walk(value)
                    if found:
                        return found
            elif isinstance(node, list):
                for value in node:
                    found = walk(value)
                    if found:
                        return found
            return None

        return walk(form)["options"][0]["value"]

    assert options("proxy_tier1_protocol") == "http"
    assert options("proxy_tier1_auth_mode") == "user_password"
    assert options("proxy_tier1_session_mode") == "rotating"
    defaults = form["defaults"] if "defaults" in form else {}
    for n in (1, 2, 3):
        assert defaults.get(f"proxy_tier{n}_protocol", "http") == "http"
        assert defaults.get(f"proxy_tier{n}_auth_mode", "user_password") == "user_password"
        assert defaults.get(f"proxy_tier{n}_session_mode", "rotating") == "rotating"
