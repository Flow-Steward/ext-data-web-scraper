"""0.1.26: links and texts only. The support and website links point to the extension's
repository Flow-Steward/ext-data-web-scraper; the guide names the published service image
ghcr.io/flow-steward/web-scraper-service:0.2.0 instead of internal image tags.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from test_0_1_23 import SCRAPES

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = yaml.safe_load((ROOT / "extension.yaml").read_text())
DESCRIPTOR = yaml.safe_load((ROOT / "extension-descriptor.yaml").read_text())
REPOSITORY = "https://github.com/Flow-Steward/ext-data-web-scraper"
IMAGE = "ghcr.io/flow-steward/web-scraper-service:0.2.0"


def test_version_and_host_minimum():
    assert MANIFEST["version"] == "0.1.26"
    assert DESCRIPTOR["release_version"] == "0.1.26"
    for variant in SCRAPES.values():
        assert variant["version"] == "0.1.26"
    for name in ("extension.yaml", "extension-descriptor.yaml"):
        manifest = yaml.safe_load((ROOT / name).read_text())
        assert manifest["runtime"]["compatibility"]["platform_min"] == "1.8.0", name


def test_the_templates_and_workflows_carry_the_version():
    for name in ("scrape_site", "scraper_events", "service_maintenance"):
        for suffix in ("json", "yaml"):
            bundle = yaml.safe_load((ROOT / f"templates/{name}.bundle.{suffix}").read_text())
            assert bundle["metadata"]["source_version"] == "0.1.26", (name, suffix)
            assert [w["version"] for w in bundle["workflows"]] == ["0.1.26"], (name, suffix)
    for name in ("scrape_a_site", "scraper_events", "service_maintenance"):
        workflow = yaml.safe_load((ROOT / f"artifacts/workflows/{name}.yaml").read_text())
        assert workflow["version"] == "0.1.26", name


def test_vendor_links_point_to_the_repository():
    assert MANIFEST["vendor"]["support_url"] == REPOSITORY + "/issues"
    assert MANIFEST["vendor"]["website_url"] == REPOSITORY
    assert DESCRIPTOR["vendor"]["support_url"] == REPOSITORY + "/issues"
    for name in ("extension.yaml", "extension-descriptor.yaml"):
        assert "Flow-Steward/ext-web-scraper" not in (ROOT / name).read_text(), name


def test_the_guide_names_the_published_image_not_internal_tags():
    for name in ("docs/setup-guide.md", "ui/components/guide.yaml"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "rc-next" not in text, name
        assert IMAGE in text, name
    guide = (ROOT / "docs/setup-guide.md").read_text(encoding="utf-8")
    assert "0.1.26 changes links\nand texts only" in guide
    assert f"**Run the scraper service** (the image `{IMAGE}`" in guide
