"""A23: protection traces in plain words, the protection error, the timeout hint, Run detail.

The traces follow the service contract (`analysis.schema.json#/$defs/protection_trace`).
`protection_not_passed` (a warning code of /v1/analyze and /v1/samples) and the
samples answer's `stopped` are the service change made in parallel (finding A22);
the vendored contract carries them as placeholders until it is re-synced.
"""

from __future__ import annotations

import copy
import re
import time

import pytest
from conftest import SECRETS, assert_no_secrets
from fake_service import TOKEN, example, schema_errors

from handlers import budget
from handlers import protection as text

RUN = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"
SITE_URL = "https://www.lumen-shop.example/"
SHOT = "shot_01M7ABMYEX0HWWAEWRFH0BXEA4"

#: Acceptance log row 51, on the fixture site: wait timed out, one click, solver off.
CHALLENGED = {
    "url": "https://www.lumen-shop.example/kategorie/leuchten-c1331",
    "at": "2026-11-13T11:45:00Z",
    "browser": "camoufox",
    "proxy_tier": 1,
    "exit_ip": None,
    "challenge": "interstitial",
    "http_status": 403,
    "steps": [
        {"step": "wait", "outcome": "timed_out", "seconds": 15.0},
        {
            "step": "click",
            "outcome": "still_challenged",
            "frame_found_via": "frames",
            "x": 523,
            "y": 291,
            "seconds": 15.5,
        },
        {"step": "solver", "outcome": "skipped", "reason": "not_requested"},
    ],
    "passed_at_step": None,
    "cookies": ["cf_clearance", "cf_chl_rc_ni"],
    "final_title": "Just a moment...",
    "screenshot_id": SHOT,
    "environment": {
        "exit": {
            "ip": "203.0.113.7",
            "timezone": "Europe/Riga",
            "locale": "lv-LV",
            "latitude": 56.9,
            "longitude": 24.1,
            "country": "LV",
            "source": "echo+geoip",
        },
        "browser_view": {
            "timezone": "Europe/Riga",
            "language": "lv-LV",
            "platform": "Win32",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 "
            "Firefox/158.0",
            "webgl_renderer": "NVIDIA GeForce GTX 980",
        },
        "preset_id": "fp_3f9a1c27d0b84e65",
    },
}

EXPECTED_PARAGRAPH = (
    "www.lumen-shop.example/kategorie/leuchten-c1331: Cloudflare interstitial challenge "
    "(HTTP 403). Waited 15 s — still challenged; clicked the checkbox once — still "
    "challenged; solver not switched on for this run (use_solver). Not passed. "
    "Proxy tier 1, exit LV (Europe/Riga, lv-LV), browser Firefox 158 on Windows, fingerprint "
    'fp_3f9a1c27d0b84e65. Page title "Just a moment...". Screenshot: shot_01M7ABMYEX0HWWAEWRFH0BXEA4 (kept by the '
    "service while samples are kept)."
)

WARNING = {
    "field": "site_url",
    "code": "protection_not_passed",
    "message": "The Cloudflare challenge on www.lumen-shop.example was not passed; the "
    "remaining URLs of this host were not tried.",
}


def _config(invoke) -> dict:
    return invoke("validate_config", {"site_url": SITE_URL, "site_settings": {}})["result"][
        "config"
    ]


def _analysis(*traces, warnings=(), passed_at=None) -> dict:
    answer = example("post_analyze.response.json")
    answer["protection"]["passed_at_step"] = passed_at
    answer["protection_trace"] = [copy.deepcopy(t) for t in traces]
    answer["warnings"] = list(warnings)
    assert not schema_errors("analysis.schema.json", answer)
    return answer


def _samples(*traces, samples=None, warnings=(), stopped=None) -> dict:
    answer = example("post_samples.response.json")
    if samples is not None:
        answer["samples"] = samples
    answer["protection_trace"] = [copy.deepcopy(t) for t in traces]
    answer["warnings"] = list(warnings)
    if stopped is not None:
        answer["stopped"] = stopped
    assert not schema_errors("samples.schema.json", answer)
    return answer


# ------------------------------------------------------------------ the text


def test_the_owner_s_trace_reads_as_one_plain_paragraph():
    assert text.describe_trace(CHALLENGED) == EXPECTED_PARAGRAPH
    assert "203.0.113.7" not in EXPECTED_PARAGRAPH  # the exit IP stays in the raw trace
    assert "https://" not in EXPECTED_PARAGRAPH


def test_every_step_outcome_has_words():
    trace = {
        "url": "https://shop.example/p?id=1",
        "browser": "chromium",
        "proxy_tier": None,
        "challenge": "turnstile",
        "steps": [
            {"step": "wait", "outcome": "passed", "seconds": 4.3},
            {"step": "next_proxy_tier", "outcome": "moved", "to_tier": 2},
            {"step": "click", "outcome": "frame_not_found", "frame_found_via": "none"},
            {"step": "click", "outcome": "passed"},
            {"step": "solver", "outcome": "skipped", "reason": "not_allowed_by_service"},
            {"step": "solver", "outcome": "error", "code": "solver_unreachable"},
            {"step": "next_proxy_tier", "outcome": "skipped", "reason": "limit_reached"},
            {"step": "wait", "outcome": "hard_block"},
            {"step": "fingerprint", "outcome": "rotated"},
        ],
        "passed_at_step": "click",
        "cookies": [],
        "final_title": "Shop",
    }
    paragraph = text.describe_trace(trace)
    for words in (
        "shop.example/p?id=1: Turnstile challenge.",
        "Waited 4.3 s — passed",
        "moved to proxy tier 2",
        "click: the checkbox frame was not found",
        "clicked the checkbox — passed",
        "solver not allowed by the service",
        "solver failed (solver_unreachable)",
        "next proxy tier skipped: the run's limit was reached",
        "wait: the site then refused the visitor (hard block)",
        "fingerprint: rotated",
        "Passed (click).",
        "No proxy, browser Chromium.",
    ):
        assert words in paragraph, (words, paragraph)
    assert "Page title" not in paragraph and "Screenshot" not in paragraph


def test_the_summary_is_empty_without_a_challenge_and_names_the_screenshot_fetch():
    assert text.summary([]) == ""
    assert text.summary([], protection={"kind": "none"}) == ""
    whole = text.summary([CHALLENGED], warnings=[WARNING])
    paragraphs = whole.split("\n\n")
    assert paragraphs[0] == EXPECTED_PARAGRAPH
    assert paragraphs[1] == f"The service says: {WARNING['message']}"
    assert paragraphs[-1] == text.SCREENSHOT_HOW
    assert "GET /v1/screenshots/<id>" in text.SCREENSHOT_HOW
    many = text.summary([CHALLENGED] * 12)
    assert many.count("Cloudflare") == text.MAX_PARAGRAPHS and "…and 2 more" in many


def test_a_browser_environment_that_does_not_match_the_exit_is_named():
    trace = copy.deepcopy(CHALLENGED)
    trace["environment"]["browser_view"]["timezone"] = "UTC"
    assert "exit LV (Europe/Riga, lv-LV), but the browser reported UTC" in (
        text.describe_trace(trace)
    )


# ------------------------------------------------------------ Analyze site


def test_analyze_site_gives_the_protection_in_plain_words(invoke, service):
    config = _config(invoke)
    service.override("POST", r"^/v1/analyze$", 200, _analysis(CHALLENGED))
    response = invoke("analyze_site", {"config": config})
    assert response["ok"], response
    result = response["result"]
    assert result["protection_summary"].startswith(EXPECTED_PARAGRAPH)
    assert result["protection_not_passed"] is True
    assert result["analysis"]["protection_trace"][0]["screenshot_id"] == SHOT  # raw kept
    assert_no_secrets(response)


def test_analyze_site_with_a_passed_challenge_and_with_none(invoke, service):
    config = _config(invoke)
    passed = invoke("analyze_site", {"config": config})["result"]  # the contract example
    assert "Passed (click)." in passed["protection_summary"]
    assert passed["protection_not_passed"] is False
    calm = _analysis()
    calm["protection"] = {"kind": "none", "challenge": None, "passed_at_step": None}
    service.override("POST", r"^/v1/analyze$", 200, calm)
    quiet = invoke("analyze_site", {"config": config})["result"]
    assert quiet["protection_summary"] == "" and quiet["protection_not_passed"] is False


def test_analyze_site_names_the_service_s_protection_warning(invoke, service):
    config = _config(invoke)
    service.override("POST", r"^/v1/analyze$", 200, _analysis(CHALLENGED, warnings=[WARNING]))
    result = invoke("analyze_site", {"config": config})["result"]
    assert WARNING["message"] in result["protection_summary"]
    assert result["protection_not_passed"] is True


# ----------------------------------------------------------- Fetch samples


def test_samples_stopped_by_the_protection_fail_in_plain_words(invoke, service):
    """The service of A22: stops early and says so (`stopped`, `protection_not_passed`)."""
    config = _config(invoke)
    stopped = {
        "reason": "protection_not_passed",
        "host": "www.lumen-shop.example",
        "url": CHALLENGED["url"],
        "skipped_urls": 9,
    }
    service.override(
        "POST",
        r"^/v1/samples$",
        200,
        _samples(CHALLENGED, samples=[], warnings=[WARNING], stopped=stopped),
    )
    response = invoke("fetch_samples", {"config": config})
    assert response["error_code"] == "protection_not_passed"
    assert response["retryable"] is False
    message = response["error"]
    assert message.startswith(
        "The site's protection check was not passed on www.lumen-shop.example, so no sample "
        "page could be fetched. What to try: the machine's own OS for the browser (leave "
        "Browser OS empty or host on the run form): a Windows or macOS claim on a Linux "
        "service is usually refused by protection checks; another proxy tier, best a "
        "residential proxy with sticky sessions"
    )
    assert "running again later" in message and "use_solver" in message
    assert EXPECTED_PARAGRAPH in message
    assert "9 more URLs were not tried" in message
    assert text.SCREENSHOT_HOW in message
    assert "did not finish" not in message  # not a timeout
    result = response["result"]
    assert result["stopped"] == stopped and result["protection_trace"][0]["url"]
    assert result["protection_summary"].startswith(EXPECTED_PARAGRAPH)
    assert TOKEN not in str(response)
    assert_no_secrets(response)


def test_today_s_service_without_the_new_fields_is_read_from_the_traces(invoke, service):
    config = _config(invoke)
    service.override("POST", r"^/v1/samples$", 200, _samples(CHALLENGED, CHALLENGED, samples=[]))
    response = invoke("fetch_samples", {"config": config})
    assert response["error_code"] == "protection_not_passed"
    assert "on www.lumen-shop.example" in response["error"]
    assert response["error"].count("Cloudflare interstitial") == 2


def test_no_samples_without_a_challenge_stays_no_samples(invoke, service):
    config = _config(invoke)
    service.override("POST", r"^/v1/samples$", 200, _samples(samples=[]))
    response = invoke("fetch_samples", {"config": config})
    assert response["error_code"] == "no_samples"


def test_samples_that_passed_carry_the_summary(invoke, service):
    config = _config(invoke)
    passed = copy.deepcopy(CHALLENGED)
    passed["steps"][1]["outcome"] = "passed"
    passed["passed_at_step"] = "click"
    service.override("POST", r"^/v1/samples$", 200, _samples(passed))
    result = invoke("fetch_samples", {"config": config})["result"]
    assert "clicked the checkbox once — passed" in result["protection_summary"]
    assert result["sample_count"] >= 1
    assert invoke("fetch_samples", {"config": config})["result"]["protection_summary"] == ""


def test_a_samples_timeout_after_a_failed_analysis_names_the_protection(
    invoke, service, monkeypatch
):
    config = _config(invoke)
    monkeypatch.setattr(budget, "MIN_CALL_SECONDS", 1)
    monkeypatch.setattr(budget, "budget_seconds", lambda: 12.5)  # 2 s for the call
    service.delays[("POST", r"/v1/samples$")] = 4.0
    seen = text.summary([CHALLENGED])
    begin = time.monotonic()
    response = invoke(
        "fetch_samples",
        {
            "config": config,
            "analysis_protection_summary": seen,
            "analysis_protection_not_passed": True,
        },
    )
    assert time.monotonic() - begin < 4.0
    assert response["error_code"] == "service_timeout" and response["retryable"] is True
    message = response["error"]
    assert message.startswith("The scraper service did not finish the sample pages within")
    assert (
        "The site's protection is the likely cause: the analysis met a protection check and "
        "did not pass it" in message
    )
    assert "What to try:" in message and EXPECTED_PARAGRAPH in message
    assert "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS" in message
    assert message.rstrip().endswith(text.SCREENSHOT_HOW)


def test_a_samples_timeout_with_no_time_left_names_a_passed_protection(
    invoke, service, monkeypatch
):
    config = _config(invoke)
    monkeypatch.setattr(budget, "budget_seconds", lambda: 20.0)
    response = invoke(
        "fetch_samples",
        {"config": config, "analysis_protection_summary": "x: Turnstile challenge. Passed."},
    )
    assert response["error_code"] == "service_timeout"
    assert "needed the protection route to pass it" in response["error"]
    assert not service.calls("POST", "/v1/samples")


def test_a_timeout_without_an_analysis_keeps_the_general_words(invoke, service, monkeypatch):
    config = _config(invoke)
    monkeypatch.setattr(budget, "budget_seconds", lambda: 20.0)
    response = invoke("fetch_samples", {"config": config, "analysis_protection_summary": None})
    assert "On a protected site the service runs the protection route" in response["error"]
    assert "likely cause" not in response["error"]


# ---------------------------------------------------------------- Run detail

#: The platform's action-result summary (extension-action-result-summary.ts, 0.9.16):
#: keys like these are hidden or blanked, and only the first 12 plain values are shown.
RUNTIME_FIELD = re.compile(
    r"(?:^|_)(?:command|argv|args|cwd|path|paths|bundle|stdout|stderr|trace|traceback|duration|"
    r"elapsed|install|account|correlation|request)(?:_|$)",
    re.IGNORECASE,
)
SENSITIVE_FIELD = re.compile(
    r"(?:authorization|cookie|credential|password|private[_-]?key|secret|token)", re.IGNORECASE
)


def _shown(result: dict) -> dict:
    plain = [
        (key, value)
        for key, value in result.items()
        if not RUNTIME_FIELD.search(key) and not isinstance(value, dict)
    ]
    return dict(plain[:12])


def test_run_detail_shows_the_job_s_traces(invoke, service):
    run = example("get_run.response.json")
    passed = copy.deepcopy(CHALLENGED)
    passed["passed_at_step"] = "click"
    passed["steps"][1]["outcome"] = "passed"
    run["protection"]["traces"] = [CHALLENGED, passed]
    run["protection"]["challenges_met"] = 6
    assert not schema_errors("run_summary.schema.json", run)
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    response = invoke("show_run", {"run_id": RUN})
    assert response["ok"], response
    shown = _shown(response["result"])
    assert not any(SENSITIVE_FIELD.search(key) for key in shown)
    pages = shown["challenged_pages"]
    assert pages.startswith(f"1) {EXPECTED_PARAGRAPH}\n2) ")
    assert "clicked the checkbox once — passed" in pages
    assert "(The first 2 of 6 challenged pages.)" in pages
    assert f"GET {service.url}/v1/screenshots/<id>" in pages
    assert "stored_profile_data" in shown and "result" in shown
    assert "proxy tier now" in shown["protection"] and "Repairs:" in shown["templates"]
    assert response["result"]["details"]["protection"]["traces"][0]["screenshot_id"] == SHOT
    assert TOKEN not in str(response)
    assert all(value not in str(response) for value in SECRETS.values())


@pytest.mark.parametrize(
    ("traces", "met", "expected"),
    [([], 0, "none"), ([], 3, "kept no trace"), (None, 3, "kept no trace")],
)
def test_run_detail_without_traces(invoke, service, traces, met, expected):
    run = example("get_run.response.json")
    run["protection"]["challenges_met"] = met
    if traces is None:
        del run["protection"]["traces"]
    else:
        run["protection"]["traces"] = traces
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    pages = invoke("show_run", {"run_id": RUN})["result"]["challenged_pages"]
    assert expected in pages


# ---------------------------------------------------------------- the template


def test_scrape_a_site_stops_at_fetch_samples_with_the_protection_named(service, tmp_path):
    from test_templates import IMPL, SCRAPE, SCRAPE_INPUTS, _action, _connector
    from workflow_sim import Walk

    seen: dict = {}
    connector = _connector(service, tmp_path)

    def recording(step, inputs):
        seen[step["step_id"]] = inputs
        return connector(step, inputs)

    stopped = {
        "reason": "protection_not_passed",
        "host": "www.lumen-shop.example",
        "url": CHALLENGED["url"],
        "skipped_urls": 9,
    }
    service.override("POST", r"^/v1/analyze$", 200, _analysis(CHALLENGED))
    service.override(
        "POST", r"^/v1/samples$", 200, _samples(CHALLENGED, samples=[], stopped=stopped)
    )
    walk = Walk(SCRAPE, connector=recording, invoke=_action(service, tmp_path, []), child=IMPL)
    outcome, state = walk.run(dict(SCRAPE_INPUTS))
    assert outcome == "failed" and walk.trace[-1] == "fetch_samples"
    assert walk.failures == ["fetch_samples: fetch_samples: protection_not_passed"]
    assert state["protection_summary"].startswith(EXPECTED_PARAGRAPH)
    assert state["protection_not_passed"] is True
    assert seen["fetch_samples"]["analysis_protection_summary"] == state["protection_summary"]
    assert seen["fetch_samples"]["analysis_protection_not_passed"] is True
    assert not service.schema_violations


def test_url_list_mode_passes_no_analysis_to_the_samples(service, tmp_path):
    from test_templates import IMPL, SCRAPE, SCRAPE_INPUTS, _action, _connector
    from workflow_sim import Walk

    seen: dict = {}
    connector = _connector(service, tmp_path)

    def recording(step, inputs):
        seen[step["step_id"]] = inputs
        return connector(step, inputs)

    inputs = {
        **SCRAPE_INPUTS,
        "discovery_mode": "url_list",
        "urls": ["https://www.lumen-shop.example/ersatzteile/a"],
    }
    walk = Walk(SCRAPE, connector=recording, invoke=_action(service, tmp_path, []), child=IMPL)
    outcome, state = walk.run(inputs)
    assert outcome == "stop_success", walk.failures
    assert seen["fetch_samples"]["analysis_protection_summary"] is None
    assert seen["fetch_samples"]["analysis_protection_not_passed"] is None
    assert state["sample_protection_summary"] == ""


def test_stop_messages_stay_within_what_the_platform_shows():
    """workflow_run_projection.build_terminal_stop_error replaces a longer or templated reason."""
    from test_templates import SCRAPE

    for step in SCRAPE["steps"]:
        for kind in ("stop_fail", "stop_success"):
            message = (step.get("override") or {}).get(kind, {}).get("message")
            if message:
                assert len(message) <= 512 and "{{" not in message, step["step_id"]
    # 0.1.23: no "Crawl started" stop any more (the job waits for the run); the
    # protection is explained by the steps' own protection_summary outputs.
    assert all(s["step_id"] != "started" for s in SCRAPE["steps"])


def test_the_service_s_stopped_example_fails_in_plain_words(invoke, service):
    """The service's own answer (contract example post_samples.response.stopped.json)."""
    config = _config(invoke)
    stopped = example("post_samples.response.stopped.json")
    assert not schema_errors("samples.schema.json", stopped)
    service.override("POST", r"^/v1/samples$", 200, stopped)
    response = invoke("fetch_samples", {"config": config})
    assert response["error_code"] == "protection_not_passed"
    message = response["error"]
    assert message.startswith(
        "The site's protection check was not passed on www.lumen-shop.example, so no sample"
    )
    assert "exit DE (Europe/Berlin, de-DE), browser Firefox 158 on Windows, fingerprint " in message
    assert "fp_3f9a1c27d0b84e65" in message and "185.12.34.56" not in message
    assert "so 2 more URLs were not tried" in message
    assert stopped["warnings"][1]["message"] in message


def test_a_stop_with_no_skipped_url_says_so_without_a_count():
    stopped = {
        "reason": "protection_not_passed",
        "host": "shop.example",
        "url": "https://shop.example/p",
        "skipped_urls": 0,
    }
    assert text.stopped_text(stopped) == (
        "The service stopped early: the protection of shop.example was not passed."
    )
    assert text.not_passed([], stopped=stopped) is True


def test_canvas_readback_and_a_repeat_challenge_are_named():
    trace = copy.deepcopy(CHALLENGED)
    trace["environment"].update(
        {
            "browser_os": "windows",
            "canvas_noise": "auto",
            "canvas_readback": "noise",
            "webgl_readback": "noise",
        }
    )
    trace["passed_earlier"] = {"url": "https://www.lumen-shop.example/", "step": "click"}
    assert not schema_errors("analysis.schema.json#/$defs/protection_trace", trace)
    paragraph = text.describe_trace(trace)
    assert paragraph.startswith(
        "www.lumen-shop.example/kategorie/leuchten-c1331: Cloudflare interstitial challenge "
        "(HTTP 403). The host had passed earlier (click, on www.lumen-shop.example) and was "
        "challenged again. Waited 15 s"
    )
    assert "fingerprint fp_3f9a1c27d0b84e65, canvas noise." in paragraph
    trace["environment"].update({"canvas_readback": "exact", "webgl_readback": "unavailable"})
    assert ", canvas exact, WebGL unreadable." in text.describe_trace(trace)
    assert text.advice([trace]).startswith(
        "What to try: the machine's own OS for the browser (leave Browser OS empty or host on "
        "the run form): a Windows or macOS claim on a Linux service is usually refused by "
        "protection checks; another proxy tier"
    )
    trace["environment"]["browser_os"] = "host"
    trace["environment"]["browser_view"]["user_agent"] = (
        "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0"
    )
    advice = text.advice([trace])
    assert (
        advice.startswith("What to try: another proxy tier")
        and "Browser OS empty or host" in advice
    )


def test_counts_are_singular_or_plural():
    from handlers.common import count

    assert [count(n, "page") for n in (0, 1, 2)] == ["0 pages", "1 page", "2 pages"]
    assert count(1, "entry", "entries") == "1 entry" and count(3, "retry", "retries") == "3 retries"
    one = {
        "reason": "protection_not_passed",
        "host": "h.example",
        "url": "https://h.example/",
        "skipped_urls": 1,
    }
    assert text.stopped_text(one).endswith("so 1 more URL was not tried.")
    one["skipped_urls"] = 4
    assert text.stopped_text(one).endswith("so 4 more URLs were not tried.")
    assert text.summary([CHALLENGED] * 11).endswith(
        "…and 1 more challenged page.\n\n" + text.SCREENSHOT_HOW
    )


def test_no_samples_carries_the_service_s_explanation_in_order(invoke, service):
    """A35: the warnings' messages, then explanation.summary and its hints (service 1b46904)."""
    config = _config(invoke)
    answer = _samples(
        samples=[],
        warnings=[
            {
                "field": "site_url",
                "code": "target_pages_guessed",
                "message": "No structured data; guessed /p/* (12 URLs) by URL shape.",
            },
            {"field": "urls[0]", "code": "invalid_value", "message": "https://x.example/p/1: 404"},
        ],
    )
    answer["explanation"] = {
        "summary": "Sitemap: 1630 URLs; 3 URL-shape groups probed (12, 40, 7 pages); 0 pages "
        "with product signals; 0 challenged. Set the product page selector.",
        "counts": {"loaded": 59, "challenged": 0},
        "hints": [
            {"kind": "target_page_selector", "message": "Set the product page selector."},
            {
                "kind": "discovery_mode",
                "message": "Try Discovery = crawl from a category page",
                "value": "crawl",
            },
        ],
    }
    assert not schema_errors("samples.schema.json", answer)  # the service's fa9071d shape
    service.override("POST", r"^/v1/samples$", 200, answer)
    response = invoke("fetch_samples", {"config": config})
    assert response["error_code"] == "no_samples"
    message = response["error"]
    assert message.startswith(
        "No target page could be sampled. What the scraper service tried:\n"
        "No structured data; guessed /p/* (12 URLs) by URL shape.\n"
        "https://x.example/p/1: 404\n"
        "Sitemap: 1630 URLs; 3 URL-shape groups probed (12, 40, 7 pages); 0 pages with product "
        "signals; 0 challenged. Set the product page selector.\n"
        "Suggested: Try Discovery = crawl from a category page\n"
        "Check the site URL"
    )
    assert message.count("Set the product page selector.") == 1  # in the summary already
    assert response["result"]["explanation"]["counts"]["loaded"] == 59


def test_no_samples_without_an_explanation_keeps_the_general_advice(invoke, service):
    config = _config(invoke)
    service.override("POST", r"^/v1/samples$", 200, _samples(samples=[]))
    message = invoke("fetch_samples", {"config": config})["error"]
    assert message.startswith("No target page could be sampled. Check the site URL")


def test_run_detail_names_pages_that_were_not_loaded(invoke, service):
    """A37: page_fetch_failed (severity warning) shows before debug lines, in words."""
    run = example("get_run.response.json")
    run["errors"] = [
        {"code": "debug", "severity": "debug", "message": "queue 3", "at": "2026-11-13T19:00:00Z"},
        {
            "code": "page_fetch_failed",
            "severity": "warning",
            "message": "https://www.lumen-shop.example/p/2: the page did not finish loading",
            "at": "2026-11-13T19:00:01Z",
        },
    ]
    assert not schema_errors("run_summary.schema.json", run)
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    errors = invoke("show_run", {"run_id": RUN})["result"]["errors"]
    assert errors.startswith(
        "page not loaded: https://www.lumen-shop.example/p/2: the page did not finish loading; "
        "debug: queue 3"
    )
