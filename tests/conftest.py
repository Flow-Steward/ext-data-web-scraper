from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for path in (str(ROOT), str(TESTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from fake_service import TOKEN, FakeService  # noqa: E402

#: Distinct secret values: none of them may ever appear in a response.
SECRETS = {
    "service_token": TOKEN,
    "webhook_signing_secret": "whsec-test-signing-secret-000001",
    "proxy_tier2_username": "proxy-user-secret-42",
    "proxy_tier2_password": "proxy-pass-secret-42",
    "proxy_tier3_proxy_urls": "http://listuser:list-pass-secret-77@10.1.2.3:3128, socks5://u2:list-pass-secret-88@10.1.2.4:1080",
    "solver_api_key": "solver-key-secret-42",
}

ROOT_SCOPE = {
    "account_id": "acc_1",
    "project_id": "prj_1",
    "workflow_id": "wf_scrape",
    "job_id": "3f1c2a90-1111-4222-8333-444455556666",
    "group_id": None,
    "for_each_id": None,
    "step_id": "start_run",
    "step_execution_id": "2b0f6c1e-8d3a-4c55-9a51-0d6b1f1f0001",
}


def profile_config(service_url: str, **overrides: Any) -> dict[str, Any]:
    config = {
        "profile_name": "Test profile",
        "service_url": service_url,
        "request_timeout_seconds": 10,
        "webhook_url": "https://flow.example.com/webhook/workflows/wh_events_abc",
        "default_discovery_mode": "auto",
        "default_max_concurrency": "4",
        "default_block_heavy_resources": True,
        "proxy_tier1_provider": "generic",
        "proxy_tier1_protocol": "http",
        "proxy_tier1_host": "dc-proxy.example.net",
        "proxy_tier1_port": 3128,
        "proxy_tier1_auth_mode": "ip_allowlist",
        "proxy_tier2_provider": "oxylabs",
        "proxy_tier2_auth_mode": "user_password",
        "proxy_tier2_country": "de",
        "proxy_tier2_session_mode": "sticky",
        "proxy_tier2_sticky_minutes": 30,
        "proxy_tier3_provider": "custom_list",
        "proxy_tier3_session_mode": "sticky",
        "solver_provider": "capsolver",
        "solver_max_solves_per_job": 25,
    }
    config.update(overrides)
    return config


def make_payload(
    action_id: str,
    inputs: dict[str, Any] | None = None,
    *,
    service_url: str,
    config: dict[str, Any] | None = None,
    secrets: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    connection: bool = True,
) -> dict[str, Any]:
    target: dict[str, Any] = {}
    if connection:
        target = {
            "connection_id": "conn_scraper_1",
            "connection": {
                "connection_id": "conn_scraper_1",
                "connection_type_id": "scraper_profile",
                "config": config if config is not None else profile_config(service_url),
                "secrets": dict(SECRETS if secrets is None else secrets),
            },
        }
    payload: dict[str, Any] = {
        "contract_version": "extension_host_v2",
        "runtime_context": dict(ROOT_SCOPE if scope is None else scope),
        "action": {"action_id": action_id, "target": target, "input": dict(inputs or {})},
    }
    if artifacts is not None:
        payload["artifacts"] = artifacts
    return payload


def assert_no_secrets(response: Any) -> None:
    text = json.dumps(response)
    for name, value in SECRETS.items():
        for part in [value, *[p.strip() for p in str(value).replace(",", " ").split()]]:
            if len(part) >= 8:
                assert part not in text, f"secret {name} leaked: {text[:400]}"


@pytest.fixture(autouse=True)
def _private_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fake service listens on 127.0.0.1; a real installation sets this for a
    # scraper service on its private network.
    monkeypatch.setenv("FS_ALLOW_PRIVATE_REMOTE_URLS", "1")


@pytest.fixture
def service():
    fake = FakeService().start()
    try:
        yield fake
    finally:
        fake.stop()
        assert not fake.response_violations, fake.response_violations


def _declared_outputs() -> dict[str, list[str]]:
    import yaml

    operations = yaml.safe_load((ROOT / "contracts" / "operation_manifest.yaml").read_text())[
        "operations"
    ]
    return {op["operation_id"]: [row["name"] for row in op["outputs"]] for op in operations}


DECLARED_OUTPUTS = _declared_outputs()


@pytest.fixture
def invoke(service):
    from handlers.router import handle_request

    def _invoke(
        action_id: str, inputs: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        payload = make_payload(action_id, inputs, service_url=service.url, **kwargs)
        response = handle_request(payload)
        assert_no_secrets(response)
        assert isinstance(response.get("ok"), bool)
        if response["ok"] and action_id in DECLARED_OUTPUTS:
            # A workflow step's output mapping fails on a missing key, so every
            # declared output is always present in a successful result.
            missing = [
                name for name in DECLARED_OUTPUTS[action_id] if name not in response["result"]
            ]
            assert not missing, f"{action_id} result misses declared outputs {missing}"
        if not response["ok"]:
            assert response["error_code"] and response["error"]
            assert response["errors"][0] == {
                "code": response["error_code"],
                "message": response["error"],
            }
        return response

    return _invoke


def output_descriptor(tmp_path: Path, *, max_size: int = 200 * 1024 * 1024) -> dict[str, Any]:
    path = tmp_path / "result-artifact.jsonl"
    return {
        "kind": "artifact",
        "artifact_id": "res_out",
        "artifact_handle": "artifact:res_out",
        "binding_key": "result_artifact_handle",
        "filename": "result.jsonl",
        "content_type": "application/x-ndjson",
        "access": {
            "transport": "presigned_url",
            "mode": "write",
            "upload_url": path.as_uri(),
            "max_size_bytes": max_size,
        },
        "_path": str(path),
    }


def csv_output_descriptor(tmp_path: Path, *, max_size: int = 200 * 1024 * 1024) -> dict[str, Any]:
    """The output grant of get_result_csv (contracts/artifact_policies.yaml)."""
    path = tmp_path / "result-artifact.csv"
    return {
        "kind": "artifact",
        "artifact_id": "res_csv",
        "artifact_handle": "artifact:res_csv",
        "binding_key": "result_csv_handle",
        "filename": "result.csv",
        "content_type": "text/csv",
        "access": {
            "transport": "presigned_url",
            "mode": "write",
            "upload_url": path.as_uri(),
            "max_size_bytes": max_size,
        },
        "_path": str(path),
    }


def input_descriptor(path: Path, *, artifact_id: str = "urls_in") -> dict[str, Any]:
    return {
        "kind": "artifact",
        "artifact_id": artifact_id,
        "artifact_handle": f"artifact:{artifact_id}",
        "binding_key": "urls_file",
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "access": {
            "transport": "presigned_url",
            "mode": "read",
            "download_url": path.as_uri(),
            "max_size_bytes": 20 * 1024 * 1024,
        },
    }


def samples_descriptor(tmp_path: Path) -> dict[str, Any]:
    """The output grant of write_samples_file (contracts/artifact_policies.yaml)."""
    path = tmp_path / "samples-artifact.md"
    return {
        "kind": "artifact",
        "artifact_id": "art_samples",
        "artifact_handle": "artifact:art_samples",
        "binding_key": "samples_file_handle",
        "filename": "samples.md",
        "content_type": "text/markdown",
        "access": {
            "transport": "presigned_url",
            "mode": "write",
            "upload_url": path.as_uri(),
            "max_size_bytes": 4 * 1024 * 1024,
        },
        "_path": str(path),
    }
