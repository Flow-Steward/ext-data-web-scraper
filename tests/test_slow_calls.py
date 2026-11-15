"""Analysis and samples run the site's protection route: they wait to the action's deadline."""

from __future__ import annotations

import time

import pytest

from handlers import budget

SITE_URL = "https://www.lumen-shop.example/"


def _config(invoke) -> dict:
    return invoke("validate_config", {"site_url": SITE_URL, "site_settings": {}})["result"][
        "config"
    ]


@pytest.mark.parametrize(("configured", "expected"), [(None, 270), ("600", 570), ("3600", 3570)])
def test_slow_calls_get_the_rest_of_the_operation_timeout(monkeypatch, configured, expected):
    if configured is None:
        monkeypatch.delenv(budget.OPERATION_TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(budget.OPERATION_TIMEOUT_ENV, configured)
    seconds = budget.slow_call_timeout(time.monotonic(), "x")
    assert expected - 2 <= seconds <= expected  # not the old constant 180


@pytest.mark.parametrize(
    ("action", "path"), [("fetch_samples", "/v1/samples"), ("analyze_site", "/v1/analyze")]
)
def test_no_time_left_fails_before_calling(invoke, service, monkeypatch, action, path):
    config = _config(invoke)
    monkeypatch.setattr(budget, "budget_seconds", lambda: 20.0)
    response = invoke(action, {"config": config})
    assert response["error_code"] == "service_timeout" and response["retryable"] is True
    assert "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS" in response["error"]
    assert not service.calls("POST", path)


@pytest.mark.parametrize(
    ("action", "path"), [("fetch_samples", r"/v1/samples$"), ("analyze_site", r"/v1/analyze$")]
)
def test_a_slow_protection_route_ends_at_the_deadline(invoke, service, monkeypatch, action, path):
    config = _config(invoke)
    monkeypatch.setattr(budget, "MIN_CALL_SECONDS", 1)
    monkeypatch.setattr(budget, "budget_seconds", lambda: 12.5)  # 2 s for the call
    service.delays[("POST", path)] = 4.0
    begin = time.monotonic()
    response = invoke(action, {"config": config})
    assert time.monotonic() - begin < 4.0
    assert response["error_code"] == "service_timeout" and response["retryable"] is True
    assert "protection route" in response["error"]
    assert "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS" in response["error"]
