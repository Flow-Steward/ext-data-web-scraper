"""The `scraper_profile` connection, turned into the contract's `connection_profile`.

The platform's connection form stores flat fields (a repeatable list cannot hold
secret values; see docs/DEV-LOG.md), so the three proxy tiers are fixed field
groups `proxy_tier1_*` .. `proxy_tier3_*`. This module is the only place that
knows those names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from handlers.client import ServiceClient
from handlers.common import Scrubber, as_dict, as_text, is_unset
from handlers.errors import ScraperError

TIERS = (1, 2, 3)
PROXY_PROVIDERS = ("generic", "oxylabs", "bright_data", "decodo", "custom_list")
PROXY_PROTOCOLS = ("http", "https", "socks5")
SOLVER_PROVIDERS = ("capsolver", "two_captcha", "anti_captcha", "capmonster_cloud")

#: Endpoint presets. A preset only pre-fills protocol, host and port when the
#: profile leaves them empty; the service builds the provider's own username
#: parameters for country, city and sticky sessions. Taken from the providers'
#: public documentation; an operator can always overwrite them.
PROXY_PRESETS: dict[str, dict[str, Any]] = {
    "oxylabs": {"protocol": "http", "host": "pr.oxylabs.io", "port": 7777},
    "bright_data": {"protocol": "http", "host": "brd.superproxy.io", "port": 33335},
    "decodo": {"protocol": "http", "host": "gate.decodo.com", "port": 7000},
    "generic": {},
    "custom_list": {},
}

#: Connection default field -> job_settings key (`profile.defaults`).
DEFAULT_FIELDS: dict[str, str] = {
    "default_discovery_mode": "discovery_mode",
    "default_fetch_mode": "fetch_mode",
    "default_browser": "browser",
    "default_max_concurrency": "max_concurrency",
    "default_max_requests_per_minute": "max_requests_per_minute",
    "default_delay_min_seconds": "delay_min_seconds",
    "default_delay_max_seconds": "delay_max_seconds",
    "default_max_retries": "max_retries",
    "default_block_heavy_resources": "block_heavy_resources",
    "default_sample_count": "sample_count",
    "default_required_fill_threshold": "required_fill_threshold",
    "default_max_refinements": "max_refinements",
    "default_max_repairs_per_template": "max_repairs_per_template",
    "default_max_repairs_per_job": "max_repairs_per_job",
    "default_max_clicks_per_job": "max_clicks_per_job",
}
INTEGER_DEFAULTS = {
    "max_concurrency",
    "max_requests_per_minute",
    "max_retries",
    "sample_count",
    "max_refinements",
    "max_repairs_per_template",
    "max_repairs_per_job",
    "max_clicks_per_job",
}
NUMBER_DEFAULTS = {"delay_min_seconds", "delay_max_seconds", "required_fill_threshold"}
BOOLEAN_DEFAULTS = {"block_heavy_resources"}

SECRET_FIELDS = (
    "service_token",
    "webhook_signing_secret",
    "solver_api_key",
    *[f"proxy_tier{n}_{name}" for n in TIERS for name in ("username", "password", "proxy_urls")],
)


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or is_unset(value):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or is_unset(value):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = as_text(value).lower()
    if text in {"true", "yes", "1", "on"}:
        return True
    if text in {"false", "no", "0", "off"}:
        return False
    return None


@dataclass
class Profile:
    connection_id: str
    profile_name: str
    service_url: str
    token: str
    timeout_seconds: int | None
    webhook_url: str
    webhook_secret: str
    proxy_tiers: list[dict[str, Any]] = field(default_factory=list)
    solver: dict[str, Any] | None = None
    defaults: dict[str, Any] = field(default_factory=dict)
    scrubber: Scrubber = field(default_factory=Scrubber)

    def client(self, timeout_seconds: int | None = None) -> ServiceClient:
        return ServiceClient(
            self.service_url, self.token, timeout_seconds=timeout_seconds or self.timeout_seconds
        )

    def contract_profile(
        self, *, with_defaults: bool = True, with_webhook: bool = True
    ) -> dict[str, Any]:
        """`connection_profile.schema.json` body; carries secrets, never returned or logged."""
        body: dict[str, Any] = {"profile_name": self.profile_name[:200]}
        body["proxy_tiers"] = [dict(tier) for tier in self.proxy_tiers]
        body["solver"] = dict(self.solver) if self.solver else None
        if with_defaults:
            body["defaults"] = dict(self.defaults)
        if with_webhook:
            body["webhook"] = self.webhook()
        return body

    def webhook(self) -> dict[str, str] | None:
        if self.webhook_url and self.webhook_secret:
            return {"url": self.webhook_url, "signing_secret": self.webhook_secret}
        return None

    def require_webhook(self) -> dict[str, str]:
        hook = self.webhook()
        if hook is None:
            missing = []
            if not self.webhook_url:
                missing.append("webhook URL")
            if not self.webhook_secret:
                missing.append("webhook signing secret")
            raise ScraperError(
                "invalid_connection",
                "The scraper profile has no "
                + " and no ".join(missing)
                + ". Create the signed incoming webhook on the imported 'Scraper events' "
                "workflow and copy its URL and secret into this profile (setup guide, step 3).",
                no_external_effect=True,
            )
        if len(self.webhook_secret) < 16:
            raise ScraperError(
                "invalid_connection",
                "The webhook signing secret is shorter than 16 characters. Use the secret "
                "the platform generated for the signed webhook.",
                no_external_effect=True,
            )
        return hook

    def tier_summary(self) -> list[dict[str, Any]]:
        """What the run page and test results may show: no credentials."""
        rows = []
        for tier in self.proxy_tiers:
            rows.append(
                {
                    "tier": tier["tier"],
                    "provider": tier["provider"],
                    "endpoint": (
                        f"{tier.get('protocol', '')}://{tier.get('host', '')}:{tier.get('port', '')}"
                        if tier.get("host")
                        else f"{len(tier.get('proxy_urls') or [])} listed proxies"
                    ),
                    "auth_mode": tier.get("auth_mode"),
                    "country": tier.get("country"),
                    "session_mode": tier.get("session_mode"),
                }
            )
        return rows


def split_proxy_urls(value: Any) -> list[str]:
    """The proxy list is one masked field: URLs separated by spaces, commas or new lines."""
    text = str(value or "").replace(",", " ")
    return [part for part in text.split() if part and not part.startswith("#")]


def _solves(value: Any) -> int:
    number = _int(value)
    return 20 if number is None else max(number, 0)


def proxy_url_secrets(url: str) -> list[str]:
    """The `user:password` part of a proxy URL, also percent-decoded, and the password alone.

    The whole URL is scrubbed too, but a message that quotes the proxy in another
    form (without the scheme, or with the host resolved) must not show the password.
    """
    netloc = urlsplit(url if "//" in url else f"//{url}").netloc
    userinfo, at, _host = netloc.rpartition("@")
    if not at or not userinfo:
        return []
    _user, colon, password = userinfo.partition(":")
    values = [userinfo, unquote(userinfo)]
    if colon and password:
        values += [password, unquote(password)]
    return [value for value in values if value]


def _tier_from_config(n: int, config: dict[str, Any], secrets: dict[str, Any]) -> dict | None:
    prefix = f"proxy_tier{n}_"
    provider = as_text(config.get(f"{prefix}provider")).lower()
    if not provider or provider == "none":
        return None
    if provider not in PROXY_PROVIDERS:
        raise ScraperError(
            "invalid_connection",
            f"Proxy tier {n}: unknown provider '{provider}'.",
            no_external_effect=True,
        )
    preset = PROXY_PRESETS.get(provider, {})
    tier: dict[str, Any] = {"tier": n, "provider": provider}
    # An empty select shows its first option in the form, so an unset field means
    # exactly that option: protocol http, user_password, rotating (the form's
    # `defaults` store the same values on a new profile).
    protocol = as_text(config.get(f"{prefix}protocol")).lower() or preset.get("protocol", "")
    if not protocol and provider != "custom_list":
        protocol = "http"
    host = as_text(config.get(f"{prefix}host")) or preset.get("host", "")
    port = _int(config.get(f"{prefix}port")) or preset.get("port")
    if protocol:
        if protocol not in PROXY_PROTOCOLS:
            raise ScraperError(
                "invalid_connection",
                f"Proxy tier {n}: protocol must be http, https or socks5.",
                no_external_effect=True,
            )
        tier["protocol"] = protocol
    if host:
        tier["host"] = host
    if port:
        tier["port"] = int(port)
    auth_mode = as_text(config.get(f"{prefix}auth_mode")).lower() or "user_password"
    if auth_mode not in {"user_password", "ip_allowlist"}:
        raise ScraperError(
            "invalid_connection",
            f"Proxy tier {n}: authentication must be 'user_password' or 'ip_allowlist'.",
            no_external_effect=True,
        )
    tier["auth_mode"] = auth_mode
    username = as_text(secrets.get(f"{prefix}username"))
    password = as_text(secrets.get(f"{prefix}password"))
    if auth_mode == "user_password":
        if username:
            tier["username"] = username
        if password:
            tier["password"] = password
    if provider == "custom_list":
        urls = split_proxy_urls(secrets.get(f"{prefix}proxy_urls"))
        if urls:
            tier["proxy_urls"] = urls
    country = as_text(config.get(f"{prefix}country")).upper()
    tier["country"] = country or None
    city = as_text(config.get(f"{prefix}city"))
    tier["city"] = city or None
    session_mode = as_text(config.get(f"{prefix}session_mode")).lower() or "rotating"
    if session_mode not in {"rotating", "sticky"}:
        raise ScraperError(
            "invalid_connection",
            f"Proxy tier {n}: session must be 'rotating' or 'sticky'.",
            no_external_effect=True,
        )
    tier["session_mode"] = session_mode
    if session_mode == "sticky":
        tier["sticky_minutes"] = _int(config.get(f"{prefix}sticky_minutes")) or 10
    return tier


def profile_from_connection(connection: Any) -> Profile:
    connection = as_dict(connection)
    if not connection:
        raise ScraperError(
            "invalid_connection",
            "No saved scraper profile was given. On the Profiles page, save the profile "
            "first: the test buttons use its saved values. In a workflow, choose a "
            "'Scraper profile' connection on this step.",
            no_external_effect=True,
        )
    config = as_dict(connection.get("config"))
    secrets = as_dict(connection.get("secrets"))
    scrubber = Scrubber(as_text(secrets.get(name)) for name in SECRET_FIELDS)
    for n in TIERS:
        for url in split_proxy_urls(secrets.get(f"proxy_tier{n}_proxy_urls")):
            scrubber.add(url, *proxy_url_secrets(url))
    tiers = [tier for n in TIERS if (tier := _tier_from_config(n, config, secrets))]
    solver = None
    solver_provider = as_text(config.get("solver_provider")).lower()
    if solver_provider and solver_provider != "none":
        if solver_provider not in SOLVER_PROVIDERS:
            raise ScraperError(
                "invalid_connection",
                f"Unknown challenge solver '{solver_provider}'.",
                no_external_effect=True,
            )
        solver = {
            "provider": solver_provider,
            "api_key": as_text(secrets.get("solver_api_key")),
            "endpoint": as_text(config.get("solver_endpoint")) or None,
            # 0 is a valid setting (no paid solve in a run); only an empty field means 20.
            "max_solves_per_job": _solves(config.get("solver_max_solves_per_job")),
            "challenge_types": ["turnstile"],
        }
        if not solver["api_key"]:
            raise ScraperError(
                "invalid_connection",
                "The challenge solver is selected but its API key is not stored.",
                no_external_effect=True,
            )
    defaults: dict[str, Any] = {}
    for field_name, key in DEFAULT_FIELDS.items():
        raw = config.get(field_name)
        if is_unset(raw):
            continue
        if key in INTEGER_DEFAULTS:
            value: Any = _int(raw)
        elif key in NUMBER_DEFAULTS:
            value = _number(raw)
        elif key in BOOLEAN_DEFAULTS:
            value = _bool(raw)
        else:
            value = as_text(raw).lower()
        if value is not None and value != "":
            defaults[key] = value
    return Profile(
        connection_id=as_text(connection.get("connection_id") or connection.get("id")),
        profile_name=as_text(
            config.get("profile_name") or connection.get("display_name") or connection.get("name"),
            "Scraper profile",
        ),
        service_url=as_text(config.get("service_url") or config.get("base_url")),
        token=as_text(secrets.get("service_token")),
        timeout_seconds=_int(config.get("request_timeout_seconds")),
        webhook_url=as_text(config.get("webhook_url")),
        webhook_secret=as_text(secrets.get("webhook_signing_secret")),
        proxy_tiers=tiers,
        solver=solver,
        defaults=defaults,
        scrubber=scrubber,
    )
