"""0.1.26: links and texts only. The support and website links point to the extension's
repository Flow-Steward/ext-data-web-scraper; the guide names no internal image tags.
(The version tests moved to test_0_1_28.py; the guide's service setup changed in 0.1.27.)
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = yaml.safe_load((ROOT / "extension.yaml").read_text())
DESCRIPTOR = yaml.safe_load((ROOT / "extension-descriptor.yaml").read_text())
REPOSITORY = "https://github.com/Flow-Steward/ext-data-web-scraper"


def test_vendor_links_point_to_the_repository():
    assert MANIFEST["vendor"]["support_url"] == REPOSITORY + "/issues"
    assert MANIFEST["vendor"]["website_url"] == REPOSITORY
    assert DESCRIPTOR["vendor"]["support_url"] == REPOSITORY + "/issues"
    for name in ("extension.yaml", "extension-descriptor.yaml"):
        assert "Flow-Steward/ext-web-scraper" not in (ROOT / name).read_text(), name


def test_the_guide_names_no_internal_tags():
    for name in ("docs/setup-guide.md", "ui/components/guide.yaml"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "rc-next" not in text, name
    guide = (ROOT / "docs/setup-guide.md").read_text(encoding="utf-8")
    assert "0.1.26 changes links\nand texts only" in guide
