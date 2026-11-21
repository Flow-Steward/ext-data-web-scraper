"""0.1.28: refresh the development JSON Schema validator.

Retain the 0.1.27 setup regressions: the setup guide and catalog setup line build
the scraper service
with Docker Compose from its public repository; no prebuilt service image is named. The
contract copy follows web-scraper-service 0.2.1 (descriptions only).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from test_0_1_23 import SCRAPES

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = yaml.safe_load((ROOT / "extension.yaml").read_text())
DESCRIPTOR = yaml.safe_load((ROOT / "extension-descriptor.yaml").read_text())
SERVICE_REPOSITORY = "https://github.com/Flow-Steward/web-scraper-service"


def test_version_and_host_minimum():
    assert MANIFEST["version"] == "0.1.30-dev.6"
    assert DESCRIPTOR["release_version"] == "0.1.30-dev.6"
    for variant in SCRAPES.values():
        assert variant["version"] == "0.1.30-dev.6"
    for name in ("extension.yaml", "extension-descriptor.yaml"):
        manifest = yaml.safe_load((ROOT / name).read_text())
        assert manifest["runtime"]["compatibility"]["platform_min"] == "1.8.0", name


def test_the_templates_and_workflows_carry_the_version():
    for name in ("scrape_site", "scraper_events", "service_maintenance"):
        for suffix in ("json", "yaml"):
            bundle = yaml.safe_load(
                (ROOT / f"templates/{name}.bundle.{suffix}").read_text()
            )
            assert bundle["metadata"]["source_version"] == "0.1.30-dev.6", (
                name,
                suffix,
            )
            assert [w["version"] for w in bundle["workflows"]] == ["0.1.30-dev.6"], (
                name,
                suffix,
            )
    for name in ("scrape_a_site", "scraper_events", "service_maintenance"):
        workflow = yaml.safe_load(
            (ROOT / f"artifacts/workflows/{name}.yaml").read_text()
        )
        assert workflow["version"] == "0.1.30-dev.6", name


def test_setup_builds_the_service_with_compose_and_names_no_image():
    setup = MANIFEST["listing"]["setup_summary"]
    assert setup.startswith("Run the Web Scraper service in Docker")
    for words in ("docker-compose.yml", SERVICE_REPOSITORY, ".env", "docker compose up -d"):
        assert words in setup, words
    for name in ("docs/setup-guide.md", "ui/components/guide.yaml", "extension.yaml"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "ghcr.io" not in text, name
        assert "web-scraper-service:server" not in text, name
    guide = (ROOT / "docs/setup-guide.md").read_text(encoding="utf-8")
    step = guide.split("1. **Run the scraper service**", 1)[1].split("\n2. ", 1)[0]
    for words in (
        "Docker Compose",
        "`docker-compose.yml`",
        SERVICE_REPOSITORY,
        "`.env`",
        "`docker compose up -d`",
        "`WSS_API_TOKEN`",
        "`WSS_SECRETS_KEY`",
        "`FS_DOCKER_NETWORK`",
        "`flow-steward_default`",
        "`http://web-scraper:8080`",
        "`FS_ALLOW_PRIVATE_REMOTE_URLS=1`",
    ):
        assert words in step, words
    assert "0.1.27 changes texts\nonly" in guide
