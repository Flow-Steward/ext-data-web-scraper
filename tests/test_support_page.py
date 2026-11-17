"""The installed extension and its release archive expose actionable Support."""

from __future__ import annotations

import importlib.util
import posixpath
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/Flow-Steward/ext-data-web-scraper"


def _assert_support(load):
    manifest = load("extension.yaml")
    descriptor = load("extension-descriptor.yaml")
    assert manifest["vendor"]["support_url"] == f"{REPOSITORY}/issues"
    assert descriptor["vendor"]["support_url"] == f"{REPOSITORY}/issues"
    ui_path = Path(manifest["ui_manifest"])
    ui = load(ui_path.as_posix())
    pages = {
        page["page_id"]: (path, page)
        for item in ui["pages"]
        for path in [ui_path.parent / item["ref"]]
        for page in [load(path.as_posix())]
    }
    assert "support" in pages, "Support must be registered in the actual UI manifest"
    page_path, support = pages["support"]
    assert support["title"] == "Support"
    assert support["project_scoped"] is False
    assert "connection" not in support
    components = [
        load(posixpath.normpath((page_path.parent / item["ref"]).as_posix()))
        if "ref" in item
        else item
        for item in support["components"]
    ]
    assert components and all(component["type"] == "markdown" for component in components)
    text = "\n".join(c.get("body", c.get("content", "")) for c in components)
    for url in (f"{REPOSITORY}/issues/new/choose", f"{REPOSITORY}/issues", REPOSITORY):
        assert f"]({url})" in text
    for detail in (
        "GitHub account",
        "extension version",
        "Flow Steward version",
        "expected",
        "happened",
        "exact error message",
        "workflow",
        "step or action",
        "when it happened",
        "Monitoring → Download diagnostics",
        "Issues are public",
        "passwords",
        "API keys",
        "tokens",
        "connection strings",
        "customer data",
    ):
        assert detail in text, detail


def test_support_is_registered_and_useful():
    _assert_support(lambda name: yaml.safe_load((ROOT / name).read_text(encoding="utf-8")))


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="packaging requires a Git checkout")
def test_published_bundle_contains_registered_support():
    spec = importlib.util.spec_from_file_location(
        "support_package", ROOT / ".github/scripts/package_extension.py"
    )
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    assert package.main() == 0
    manifest = yaml.safe_load((ROOT / "extension.yaml").read_text(encoding="utf-8"))
    archives = list((ROOT / "dist").glob(f"{manifest['extension_id']}-{manifest['version']}*.zip"))
    assert archives
    prefix = manifest["extension_id"].replace(".", "_").replace("-", "_")
    for archive in archives:
        with zipfile.ZipFile(archive) as bundle:
            _assert_support(lambda name: yaml.safe_load(bundle.read(f"{prefix}/{name}")))
