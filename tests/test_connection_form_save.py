"""What the platform's connection form really saves (ported save logic, see connection_form_sim).

The stand refused to save an edited profile: with `data.config_fields` declared,
the browser wrote "" for every hidden integer field, and the connection type's
schema refused it. These tests run the ported save over create and edit, with
every tier and the solver hidden and with each one shown, and check the result
against the connection type's `config_schema` and against the handlers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from connection_form_sim import edit_values, field_path, prepare_connection_save
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
PROFILE = yaml.safe_load((ROOT / "ui/components/profile_connection_form.yaml").read_text())
TYPE = yaml.safe_load((ROOT / "contracts/connection_types.yaml").read_text())["connection_types"][0]
SCHEMA = TYPE["config_schema"]
VALIDATOR = Draft202012Validator(SCHEMA)

BASE = {
    "display_name": "Scraper local",
    "profile_name": "Scraper local",
    "service_url": "http://web-scraper:8080",
    "service_token": "typed-token-value-0001",
}
SHOWN = {
    # Every kind of tier, typed the way an HTML input hands it over (strings).
    1: {
        "proxy_tier1_provider": "generic",
        "proxy_tier1_host": "dc.example.net",
        "proxy_tier1_port": "3128",
        "proxy_tier1_auth_mode": "ip_allowlist",
    },
    2: {
        "proxy_tier2_provider": "oxylabs",
        "proxy_tier2_username": "user-x",
        "proxy_tier2_password": "pass-x-0001",
        "proxy_tier2_country": "de",
        "proxy_tier2_session_mode": "sticky",
        "proxy_tier2_sticky_minutes": "30",
    },
    3: {
        "proxy_tier3_provider": "custom_list",
        "proxy_tier3_proxy_urls": "http://u:p-0001@10.0.0.1:3128",
    },
    "solver": {
        "solver_provider": "capsolver",
        "solver_api_key": "solver-key-0001",
        "solver_max_solves_per_job": "0",
    },
}
CASES = {
    "all hidden": {},
    "tier 1 shown": SHOWN[1],
    "tier 2 shown": SHOWN[2],
    "tier 3 shown": SHOWN[3],
    "solver shown": SHOWN["solver"],
    "all shown": {**SHOWN[1], **SHOWN[2], **SHOWN[3], **SHOWN["solver"]},
}


def _check(config: dict) -> None:
    errors = sorted(VALIDATOR.iter_errors(config), key=lambda e: list(e.path))
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors]
    assert "" not in [v for k, v in config.items() if k != "profile_name"], config


def _profile(config: dict, secrets: dict):
    from handlers.profile import profile_from_connection

    stored = {**secrets}  # the secrets kept from earlier saves are added by the caller
    return profile_from_connection({"connection_id": "c1", "config": config, "secrets": stored})


@pytest.mark.parametrize("case", sorted(CASES))
def test_create_saves_a_config_the_schema_accepts(case):
    config, secrets = prepare_connection_save(PROFILE, {**BASE, **CASES[case]})
    _check(config)
    assert secrets["service_token"] == BASE["service_token"]
    assert "tier" not in config or config["tier"] in {"all", "1", "2", "3"}
    profile = _profile(config, secrets)
    assert {t["tier"] for t in profile.proxy_tiers} == {
        n for n in (1, 2, 3) if f"proxy_tier{n}_provider" in CASES[case]
    }


@pytest.mark.parametrize("case", sorted(CASES))
def test_editing_an_existing_profile_saves_a_config_the_schema_accepts(case):
    """The stand's blocker: open, fill the webhook fields, Save connection."""
    created, secrets = prepare_connection_save(PROFILE, {**BASE, **CASES[case]})
    existing = {"connection_id": "c1", "connection_config": created}
    values = edit_values(PROFILE, created, "c1")
    values["webhook_url"] = "http://compact-runtime:8080/webhook/workflows/wh_1"
    values["webhook_signing_secret"] = "generated-credential-0001"
    config, typed = prepare_connection_save(PROFILE, values, existing)
    _check(config)
    assert config["webhook_url"].endswith("/wh_1")
    assert typed == {"webhook_signing_secret": "generated-credential-0001"}
    profile = _profile(config, {**secrets, **typed})
    assert profile.webhook() is not None
    assert len(profile.proxy_tiers) == len(
        [n for n in (1, 2, 3) if f"proxy_tier{n}_provider" in CASES[case]]
    )


@pytest.mark.parametrize("tier", [1, 2, 3])
def test_switching_a_tier_back_to_not_used(tier):
    """A hidden field keeps its last stored value; the profile ignores a tier set to none."""
    created, secrets = prepare_connection_save(PROFILE, {**BASE, **CASES["all shown"]})
    values = edit_values(PROFILE, created, "c1")
    values[f"proxy_tier{tier}_provider"] = "none"
    config, _typed = prepare_connection_save(
        PROFILE, values, {"connection_id": "c1", "connection_config": created}
    )
    _check(config)
    assert config[f"proxy_tier{tier}_provider"] == "none"
    profile = _profile(config, secrets)
    assert tier not in {t["tier"] for t in profile.proxy_tiers}
    assert len(profile.proxy_tiers) == 2
    body = profile.contract_profile()
    assert tier not in {t["tier"] for t in body["proxy_tiers"]}


def test_clearing_a_visible_integer_removes_it():
    created, _secrets = prepare_connection_save(
        PROFILE, {**BASE, "default_max_concurrency": "4", **SHOWN["solver"]}
    )
    assert created["default_max_concurrency"] == 4 and created["solver_max_solves_per_job"] == 0
    values = edit_values(PROFILE, created, "c1")
    values["default_max_concurrency"] = ""
    values["solver_max_solves_per_job"] = ""
    config, _typed = prepare_connection_save(
        PROFILE, values, {"connection_id": "c1", "connection_config": created}
    )
    _check(config)
    assert "default_max_concurrency" not in config and "solver_max_solves_per_job" not in config


def test_every_saved_key_is_a_schema_property():
    """Without config_fields the browser saves every visible non-secret field."""
    values = {**BASE, **CASES["all shown"]}
    for field in PROFILE["fields"]:
        values.setdefault(field_path(field), "")
    config, _secrets = prepare_connection_save(PROFILE, values)
    assert set(config) <= set(SCHEMA["properties"]), set(config) - set(SCHEMA["properties"])
