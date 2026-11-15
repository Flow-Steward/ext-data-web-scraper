"""Static checks: the manifests agree with each other, with the handlers and with the service contract."""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_SCHEMAS = ROOT / "service_contract" / "schemas"
#: The service repository's contract, by default a checkout next to this one.
UPSTREAM = Path(
    os.environ.get(
        "WEB_SCRAPER_CONTRACT_DIR", str(ROOT.parent / "web-scraper-service" / "contract")
    )
)


def load(path: str):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def schema(name: str) -> dict:
    return json.loads((CONTRACT_SCHEMAS / name).read_text(encoding="utf-8"))


MANIFEST = load("extension.yaml")
DESCRIPTOR = load("extension-descriptor.yaml")
OPERATIONS = {
    op["operation_id"]: op for op in load("contracts/operation_manifest.yaml")["operations"]
}
FORMS = {form["operation_id"]: form for form in load("contracts/step_ui_manifest.yaml")["forms"]}
ACTIONS = {action["action_id"]: action for action in load("ui/actions/actions.yaml")["actions"]}
CONNECTION = load("contracts/connection_types.yaml")["connection_types"][0]
POLICIES = {p["operation_id"]: p for p in load("contracts/artifact_policies.yaml")["policies"]}


def test_identity_matches_between_manifest_and_descriptor():
    assert MANIFEST["extension_id"] == DESCRIPTOR["extension_id"] == "flowsteward.web-scraper"
    assert MANIFEST["version"] == DESCRIPTOR["release_version"]
    assert MANIFEST["kind"] == "tool_provider" and "tool" in MANIFEST["features"]
    assert MANIFEST["runtime"]["contract_version"] == "extension_host_v2"
    assert DESCRIPTOR["runtime"]["contract_version"] == "extension_host_v2"
    # Page queries with a connection (ADR 0067, 1.7.0), grid links and filters and
    # connection slots (ADR 0068, 1.8.0 = Flow Steward 0.9.18), in both manifests.
    assert DESCRIPTOR["runtime"]["compatibility"]["platform_min"] == "1.8.0"
    assert MANIFEST["runtime"]["compatibility"]["platform_min"] == "1.8.0"
    assert MANIFEST["entrypoint"]["mode"] == "subprocess"


def test_canonical_timeout_policy():
    assert MANIFEST["entrypoint"]["timeout_policy"] == {
        "env": "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS",
        "default_seconds": 300,
        "minimum_seconds": 30,
        "maximum_seconds": 3600,
        "headroom_seconds": 30,
    }


def test_start_run_is_an_external_effect():
    effects = {row["operation_id"]: row for row in MANIFEST["external_effects"]}
    assert set(effects) == {"start_run"}
    assert effects["start_run"]["idempotency"] == "required"
    assert effects["start_run"]["test_mode_behavior"] == "suppress"


def test_result_artifact_policy():
    output = POLICIES["get_result"]["outputs"][0]
    assert output["filename"].endswith(".jsonl")
    assert output["max_size_env"] == "FS_EXTENSION_ARTIFACT_MAX_BYTES"
    assert output["max_size_bytes"] == 209715200
    assert output["binding_key"] == "result_artifact_handle"
    assert output["retention_class"] == "run_window"
    urls_file = POLICIES["validate_config"]["inputs"][0]
    assert urls_file["field"] == "urls_file" and urls_file["required"] is False


def test_yaml_only_authoring_files():
    forbidden = [
        *ROOT.glob("extension.json"),
        *ROOT.glob("manifest.json"),
        *ROOT.glob("schemas/*.schema.json"),
        *ROOT.glob("ui/**/*.json"),
        *ROOT.glob("contracts/*.json"),
        *ROOT.glob("locales/*.json"),
    ]
    assert not forbidden


def test_every_workflow_action_pairs_with_its_operation():
    visible = {aid for aid, a in ACTIONS.items() if a.get("workflow_visible")}
    assert visible == set(OPERATIONS)
    for operation_id, operation in OPERATIONS.items():
        action = ACTIONS[operation_id]
        inputs = [
            row["name"] for row in operation["inputs"] if row["value_type"] != "connection_ref"
        ]
        assert [p["name"] for p in action["parameters"]] == inputs, operation_id
        outputs = [row["name"] for row in operation["outputs"]]
        assert action["result_fields"] == [f"result.{name}" for name in outputs], operation_id
        assert operation["result_fields"] == action["result_fields"], operation_id
        assert operation["operation_kind"] in {"source", "action"}
        if operation_id == "build_mapping":
            assert "connection_type_ids" not in operation
            assert action["handler"] == {"mode": "workflow", "workflow_id": "impl_build_mapping"}
        else:
            assert operation["connection_type_ids"] == ["scraper_profile"]
            assert operation["inputs"][0]["value_type"] == "connection_ref"
            assert action["handler"] == {"mode": "extension"}


def test_mutations_and_reads_are_declared_as_such():
    reads = {
        "validate_config",
        "analyze_site",
        "fetch_samples",
        "test_mapping",
        "get_mappings",
        "export_mappings",
        "get_run_summary",
        "get_mapping_request",
        "service_storage",
    }
    for operation_id, operation in OPERATIONS.items():
        expected = "source" if operation_id in reads else "action"
        assert operation["operation_kind"] == expected, operation_id


def test_every_operation_has_a_builder_form_with_its_inputs():
    assert set(FORMS) == set(OPERATIONS)
    for operation_id, form in FORMS.items():
        names = [
            row["name"]
            for row in OPERATIONS[operation_id]["inputs"]
            if row["value_type"] != "connection_ref"
        ]
        assert [field["name"] for field in form["fields"]] == names
        for field in form["fields"]:
            assert field["label"]
            assert field["widget_type"] in {
                "text",
                "textarea",
                "number",
                "integer",
                "boolean",
                "select",
                "json",
                "artifact_picker",
            }


def test_every_extension_action_has_a_handler_and_back():
    from handlers.router import ACTION_HANDLERS, COMPOSITE_ACTIONS

    extension_actions = {aid for aid, a in ACTIONS.items() if a["handler"]["mode"] == "extension"}
    assert extension_actions == set(ACTION_HANDLERS)
    assert {
        aid for aid, a in ACTIONS.items() if a["handler"]["mode"] == "workflow"
    } == COMPOSITE_ACTIONS
    for action in ACTIONS.values():
        assert action["title"] and action["description"]
        assert action["mutates_platform"] is False
        assert action["required_scopes"]


def test_connection_type_matches_the_profile_code_and_contract_enums():
    from handlers.profile import DEFAULT_FIELDS, SECRET_FIELDS

    config = CONNECTION["config_schema"]["properties"]
    secrets = CONNECTION["secret_schema"]["properties"]
    assert set(SECRET_FIELDS) == set(secrets)
    assert set(DEFAULT_FIELDS) <= set(config)
    profile = schema("connection_profile.schema.json")["$defs"]
    job = schema("job_config.schema.json")["$defs"]
    assert set(DEFAULT_FIELDS.values()) <= set(job["job_settings"]["properties"])
    for n in (1, 2, 3):
        assert config[f"proxy_tier{n}_provider"]["enum"][1:] == offered_providers(profile)
        assert config[f"proxy_tier{n}_protocol"]["enum"] == profile["proxy_protocol"]["enum"]
        assert config[f"proxy_tier{n}_auth_mode"]["enum"] == profile["proxy_auth_mode"]["enum"]
        assert (
            config[f"proxy_tier{n}_session_mode"]["enum"] == profile["proxy_session_mode"]["enum"]
        )
    assert config["solver_provider"]["enum"][1:] == profile["solver_provider"]["enum"]
    assert set(config["default_discovery_mode"]["enum"]) <= set(job["discovery_mode"]["enum"])
    assert config["default_fetch_mode"]["enum"] == job["fetch_mode"]["enum"]
    assert config["default_browser"]["enum"] == job["browser"]["enum"]


def test_connection_form_covers_every_field_and_masks_every_secret():
    component = load("ui/components/profile_connection_form.yaml")
    data = component["data"]
    config = CONNECTION["config_schema"]["properties"]
    secrets = CONNECTION["secret_schema"]["properties"]
    assert data["connection_type"] == "scraper_profile"
    # No config_fields: the browser saves the visible non-secret fields (see
    # test_connection_form_save.py); every one of them is a config property.
    assert "config_fields" not in data
    saved = {
        f["path"]
        for f in component["fields"]
        if f["widget"] != "secret" and f["path"] not in {"display_name", "environment"}
    }
    assert saved == set(config), saved ^ set(config)
    assert data["secret_fields"] == list(secrets)
    assert data["test_action"] == "test_connection"
    actions = {
        v["action_id"] for v in component["ui_schema"]["actions"].values() if isinstance(v, dict)
    }
    assert actions == {"test_proxy", "test_captcha_solver"}
    widgets = {field["path"]: field["widget"] for field in component["fields"]}
    for name in secrets:
        assert widgets[name] == "secret", name
    for name in config:
        assert name in widgets, name
    for field in component["fields"]:
        assert field["label"]
        if field["widget"] == "select":
            assert field["options"]


def test_run_inputs_and_site_settings_are_job_settings():
    from handlers.settings import RUN_INPUT_KEYS, SITE_SETTING_KEYS

    job_settings = schema("job_config.schema.json")["$defs"]["job_settings"]["properties"]
    assert set(RUN_INPUT_KEYS) | {"urls"} <= set(job_settings)
    assert set(SITE_SETTING_KEYS) <= set(job_settings)
    validate_inputs = {row["name"] for row in OPERATIONS["validate_config"]["inputs"]}
    assert validate_inputs == set(RUN_INPUT_KEYS) | {
        "urls",
        "urls_file",
        "site_settings",
        "connection_ref",
    }


def test_get_result_outputs_are_the_contract_descriptor():
    descriptor = schema("result_descriptor.schema.json")
    outputs = [row["name"] for row in OPERATIONS["get_result"]["outputs"]]
    # The contract's workflow descriptor first, then what the extension adds (0.1.18).
    assert outputs == [*descriptor["properties"], "has_csv"]
    assert set(descriptor["required"]) <= set(outputs)


def test_contract_enums_used_by_handlers():
    from handlers.errors import FIELD_CODE_TEXT, SERVICE_ERROR_MAP, TEST_CODE_TEXT
    from handlers.profile import PROXY_PRESETS, PROXY_PROVIDERS, SOLVER_PROVIDERS

    codes = set(schema("error.schema.json")["$defs"]["error_code"]["enum"])
    assert set(SERVICE_ERROR_MAP) | set(FIELD_CODE_TEXT) | set(TEST_CODE_TEXT) == codes
    profile = schema("connection_profile.schema.json")["$defs"]
    assert list(PROXY_PROVIDERS) == offered_providers(profile)
    assert set(PROXY_PRESETS) == set(PROXY_PROVIDERS)
    assert list(SOLVER_PROVIDERS) == profile["solver_provider"]["enum"]


def test_icon_is_a_valid_logo():
    data = (ROOT / "icon.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert width == height and 32 <= width <= 512
    assert len(data) <= 100 * 1024


def test_contract_examples_validate():
    from fake_service import EXAMPLES, schema_errors

    pairs = {
        "get_health.response.json": "job_config.schema.json#/$defs/service_health",
        "get_run.response.json": "run_summary.schema.json",
        "get_run_result.response.json": "result_descriptor.schema.json#/$defs/service_result",
        "result_descriptor.json": "result_descriptor.schema.json",
        "post_event_claim.response.json": "event.schema.json#/$defs/claim_response",
        "get_mapping_request.response.json": "mapping_request.schema.json",
        "mapping.product.json": "mapping.schema.json",
    }
    for name, ref in pairs.items():
        assert not schema_errors(ref, json.loads((EXAMPLES / name).read_text())), name
    for line in (EXAMPLES / "result.sample.jsonl").read_text().splitlines():
        assert not schema_errors("record.schema.json", json.loads(line))


@pytest.mark.skipif(
    not (UPSTREAM / "schemas").is_dir(),
    reason=f"service contract not found at {UPSTREAM}; set WEB_SCRAPER_CONTRACT_DIR to check for drift",
)
def test_vendored_schemas_have_not_drifted_from_the_service_contract():
    upstream = {p.name: p.read_bytes() for p in (UPSTREAM / "schemas").glob("*.json")}
    vendored = {p.name: p.read_bytes() for p in CONTRACT_SCHEMAS.glob("*.json")}
    assert set(upstream) == set(vendored), "schema files added or removed upstream"
    changed = sorted(name for name in upstream if upstream[name] != vendored[name])
    assert not changed, (
        f"copy the changed contract schemas again and review the extension: {changed}"
    )
    upstream_examples = {p.name: p.read_bytes() for p in (UPSTREAM / "examples").glob("*")}
    examples = {p.name: p.read_bytes() for p in (ROOT / "tests" / "contract_examples").glob("*")}
    assert upstream_examples == examples, "contract examples changed upstream"


def test_webhook_signature_vector_matches_the_platform_verifiers():
    """B6 path: the platform's `hmac_sha256` mode (workflow_entrypoints_execution_auth.py
    141-160 on flow-steward/main) strips the `sha256=` prefix of the configured header
    and compares hex HMAC-SHA256(secret, raw body); the timestamped mode of the
    appendix signs timestamp + "." + raw body."""
    import hashlib
    import hmac
    import json

    vector = json.loads(
        (ROOT / "tests/contract_examples/webhook_body_signature.vector.json").read_text()
    )
    secret = vector["signing_secret"].encode("utf-8")
    body = vector["body"].encode("utf-8")
    header = vector["x_scraper_body_signature"]
    assert header.startswith("sha256=")
    assert header[len("sha256=") :] == hmac.new(secret, body, hashlib.sha256).hexdigest()
    signed = vector["timestamp"].encode() + b"." + body
    assert vector["x_scraper_signature"] == hmac.new(secret, signed, hashlib.sha256).hexdigest()


#: Providers the owner removed from the extension (0.1.6) that an older copy of the
#: service contract may still list; the service drops them in its own release.
REMOVED_PROVIDERS = ("storm_proxies",)


def offered_providers(profile_defs: dict) -> list[str]:
    return [p for p in profile_defs["proxy_provider"]["enum"] if p not in REMOVED_PROVIDERS]


def test_storm_proxies_is_gone():
    from handlers.profile import PROXY_PRESETS, PROXY_PROVIDERS

    text = "".join(
        (ROOT / path).read_text().lower()
        for path in (
            "ui/components/profile_connection_form.yaml",
            "contracts/connection_types.yaml",
        )
    )
    assert "storm" not in text
    assert "storm_proxies" not in PROXY_PROVIDERS and "storm_proxies" not in PROXY_PRESETS
