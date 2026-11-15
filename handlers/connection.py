"""Connection form checks: the service, each proxy tier, the challenge solver."""

from __future__ import annotations

from typing import Any

from handlers.common import as_dict, as_list, as_text
from handlers.context import ActionContext
from handlers.errors import TEST_CODE_TEXT, ScraperError

TEST_TIMEOUT_SECONDS = 45


def _health(ctx: ActionContext) -> dict[str, Any]:
    health = as_dict(ctx.profile.client().get("/health"))
    versions = [as_text(v) for v in as_list(as_dict(health.get("service")).get("api_versions"))]
    if "v1" not in versions:
        raise ScraperError(
            "service_api_unsupported",
            "The service at this URL does not offer scraper API v1 (it reports "
            f"{', '.join(versions) or 'no versions'}). Check the service URL and version.",
        )
    return health


def test_connection(ctx: ActionContext) -> dict[str, Any]:
    profile = ctx.profile
    health = _health(ctx)
    service = as_dict(health.get("service"))
    # Contract: `browsers: [{name, present, version}]` (get_health.response.json).
    browsers = [as_dict(row) for row in as_list(health.get("browsers"))]
    protection = as_dict(health.get("protection"))
    limits = as_dict(health.get("limits"))
    secrets_key = as_dict(health.get("secrets_key"))
    problems = [as_text(item) for item in as_list(health.get("problems")) if as_text(item)]
    present = sorted(
        f"{as_text(row.get('name'))} {as_text(row.get('version'))}".strip()
        for row in browsers
        if row.get("present") and as_text(row.get("name"))
    )
    notes: list[str] = []
    if not profile.webhook_url or not profile.webhook_secret:
        notes.append(
            "Webhook URL or signing secret is missing: runs cannot report back until "
            "'Scraper events' has its incoming webhook (setup guide, step 3)."
        )
    if not secrets_key.get("configured", True):
        notes.append("The service has no job-secret key; it cannot start runs.")
    result = {
        "status": as_text(health.get("status"), "unknown"),
        "service": f"{as_text(service.get('name'), 'web-scraper-service')} "
        f"{as_text(service.get('version'))}".strip(),
        "api_versions": ", ".join(as_text(v) for v in as_list(service.get("api_versions"))),
        "browsers": ", ".join(present) or "none",
        "max_pages_per_job": limits.get("max_pages_per_job"),
        "max_result_mib": round(int(limits.get("max_result_bytes") or 0) / 1048576, 1)
        if limits.get("max_result_bytes")
        else None,
        "checkbox_click_allowed": bool(protection.get("challenge_click_allowed")),
        "solvers_allowed": bool(protection.get("solvers_allowed")),
        "proxy_tiers_configured": len(profile.proxy_tiers),
        "solver_configured": profile.solver["provider"] if profile.solver else "none",
        "problems": "; ".join(problems) or "none",
        "notes": " ".join(notes) or "none",
    }
    return result


def _tier_line(row: dict[str, Any]) -> str:
    tier = row.get("tier")
    if row.get("ok"):
        parts = [f"tier {tier}: OK", f"exit IP {as_text(row.get('exit_ip'), '?')}"]
        if row.get("country"):
            parts.append(f"country {row.get('country')}")
        if row.get("latency_ms") is not None:
            parts.append(f"{row.get('latency_ms')} ms")
        return ", ".join(parts)
    code = as_text(row.get("code"))
    text = TEST_CODE_TEXT.get(code, as_text(row.get("message"), "failed"))
    message = as_text(row.get("message"))
    if message and message not in text:
        text = f"{text} ({message})"
    return f"tier {tier}: FAILED - {text}"


def test_proxy(ctx: ActionContext) -> dict[str, Any]:
    profile = ctx.profile
    if not profile.proxy_tiers:
        raise ScraperError(
            "proxy_tier_not_configured",
            "This scraper profile has no proxy tier. Fill in at least the provider of "
            "tier 1 and save the connection.",
            no_external_effect=True,
        )
    configured = [tier["tier"] for tier in profile.proxy_tiers]
    requested = ctx.input.get("tier")
    tiers = configured
    if requested not in (None, "", [], "all"):
        try:
            number = int(str(requested))
        except ValueError as exc:
            raise ScraperError(
                "invalid_payload", "Tier must be 1, 2 or 3.", no_external_effect=True
            ) from exc
        if number not in configured:
            raise ScraperError(
                "proxy_tier_not_configured",
                f"Proxy tier {number} is not configured in this profile "
                f"(configured: {', '.join(map(str, configured))}).",
                no_external_effect=True,
            )
        tiers = [number]
    target_url = ctx.text("target_url")
    client = profile.client(TEST_TIMEOUT_SECONDS)
    rows = []
    body_profile = profile.contract_profile(with_defaults=False, with_webhook=False)
    for number in tiers:
        body: dict[str, Any] = {"profile": body_profile, "tier": number}
        if target_url:
            body["target_url"] = target_url
        answer = as_dict(client.post("/v1/proxies/test", body))
        answer.setdefault("tier", number)
        rows.append(answer)
    result: dict[str, Any] = {f"tier_{row.get('tier')}": _tier_line(row) for row in rows}
    result["tiers_tested"] = len(rows)
    result["tiers_failed"] = sum(1 for row in rows if not row.get("ok"))
    result["tier_results"] = [
        {
            key: row.get(key)
            for key in (
                "tier",
                "ok",
                "code",
                "exit_ip",
                "country",
                "city",
                "latency_ms",
                "target_http_status",
            )
        }
        for row in rows
    ]
    if result["tiers_failed"]:
        raise ScraperError(
            "proxy_check_failed",
            "Proxy check: " + "; ".join(_tier_line(row) for row in rows) + ".",
            result=result,
        )
    return result


def test_captcha_solver(ctx: ActionContext) -> dict[str, Any]:
    profile = ctx.profile
    if not profile.solver:
        raise ScraperError(
            "solver_not_configured",
            "This scraper profile has no challenge solver. Choose a solver provider, "
            "store its API key and save the connection.",
            no_external_effect=True,
        )
    client = profile.client(TEST_TIMEOUT_SECONDS)
    answer = as_dict(
        client.post(
            "/v1/solvers/test",
            {"profile": profile.contract_profile(with_defaults=False, with_webhook=False)},
        )
    )
    result = {
        "provider": as_text(answer.get("provider"), profile.solver["provider"]),
        "ok": bool(answer.get("ok")),
        "balance": answer.get("balance"),
        "balance_currency": answer.get("balance_currency"),
        "latency_ms": answer.get("latency_ms"),
    }
    if not answer.get("ok"):
        code = as_text(answer.get("code"))
        text = TEST_CODE_TEXT.get(code, "The solver check failed.")
        message = as_text(answer.get("message"))
        if message and message not in text:
            text = f"{text} {message}"
        result["code"] = code
        raise ScraperError("solver_check_failed", f"Solver check: {text}", result=result)
    if result["balance"] is None:
        result["balance"] = "not reported by the provider"
    return result
