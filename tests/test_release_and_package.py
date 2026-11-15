"""Release checks, packaging, licence files and the secret scan of shipped files."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

#: Values the service contract documents as fake examples or test vectors.
DOCUMENTED_FAKES = {
    "pX7-example-not-real",
    "CAP-EXAMPLE-0000000000000000",
    "fs_whsec_3b1f0e6c9a2d47f8b5c1d0e9",
    "fs_whsec_rotated_7c1d29e4b0a8f356",
}
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)\b(password|secret|api[_-]?key|token)\b\s*[:=]\s*[\"']([^\"'\s]{12,})[\"']"),
]


class _Evidence:
    def __init__(self):
        self.rows = []

    def add(self, name, passed, detail):
        self.rows.append((name, passed, detail))


class _Api:
    def __init__(self, routes):
        self.routes = routes

    def request(self, method, path, body=None):  # noqa: ARG002
        return self.routes.get((method, path.split("?")[0]), (404, {}))


def _checks():
    spec = importlib.util.spec_from_file_location(
        "web_scraper_release_checks", ROOT / "release" / "checks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_checks_declare_what_they_need():
    checks = _checks()
    assert checks.RELEASE_EXTENSIONS == ["flowsteward.web-scraper"]
    assert set(checks.RELEASE_CHECKS) == {
        "web_scraper_installed",
        "web_scraper_implementation_hidden",
        "web_scraper_service_reachable",
        "web_scraper_unexecuted",
    }
    assert all(reason for reason in checks.UNEXECUTED.values())


def test_release_checks_against_a_scripted_api(monkeypatch):
    checks = _checks()
    api = _Api(
        {
            ("GET", "/api/extensions"): (
                200,
                {"extensions": [{"extension_id": "flowsteward.web-scraper"}]},
            ),
            ("GET", "/api/workflows"): (200, [{"workflow_id": "scrape_a_site"}]),
        }
    )
    evidence = _Evidence()
    for name in ("FS_WEB_SCRAPER_SERVICE_URL", "FS_WEB_SCRAPER_SERVICE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for check in checks.RELEASE_CHECKS.values():
        check(api, evidence)
    results = {name: passed for name, passed, _ in evidence.rows}
    assert results["web_scraper_installed"] is True
    assert results["web_scraper_implementation_hidden"] is True
    assert results["web_scraper_service_reachable"] is False  # missing input is a failure
    assert any("NOT RUN" in detail for _n, _p, detail in evidence.rows)


IS_CHECKOUT = (ROOT / ".git").exists()
SKIP_PARTS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "dev-wheels",
}


def _tracked() -> list[str]:
    if IS_CHECKOUT:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
        return [p for p in out.split("\0") if p]
    # A staged copy without .git (the platform's extension gate): every file.
    return [
        str(p.relative_to(ROOT))
        for p in ROOT.rglob("*")
        if p.is_file() and not SKIP_PARTS & set(p.relative_to(ROOT).parts)
    ]


def test_no_secret_looking_value_in_any_file():
    findings = []
    for name in _tracked() + [str(p.relative_to(ROOT)) for p in ROOT.glob("templates/*.json")]:
        path = ROOT / name
        if path.suffix in {".png", ".whl", ".zip"} or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                value = match.group(match.lastindex or 0)
                if value in DOCUMENTED_FAKES or name.startswith("tests/"):
                    continue
                findings.append(f"{name}: {match.group(0)[:60]}")
        if not name.startswith("tests/contract_examples"):
            for fake in DOCUMENTED_FAKES:
                assert fake not in text or name.startswith("tests/"), (
                    f"{name} carries a contract example secret"
                )
    assert not findings, findings


def test_licence_notice_readme_and_docs_exist():
    assert "Apache License" in (ROOT / "LICENSE").read_text()
    notice = (ROOT / "NOTICE").read_text()
    assert "Apache License, Version 2.0" in notice and "LGPL-3.0" in notice
    for name in ("README.md", "docs/setup-guide.md", "docs/tutorial-outline.md", "docs/DEV-LOG.md"):
        assert (ROOT / name).is_file(), name


@pytest.mark.skipif(
    not IS_CHECKOUT, reason="packaging lists files with git; this copy is not a checkout"
)
def test_package_contains_the_bundle_and_no_authoring_files(tmp_path):
    script = ROOT / ".github" / "scripts" / "package_extension.py"
    spec = importlib.util.spec_from_file_location("package_extension_under_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    shipped = module.shipped_files()
    assert "extension.yaml" in shipped and "icon.png" in shipped and "main.py" in shipped
    assert (
        "templates/scrape_site.bundle.json" in shipped
        and "templates/scraper_events.bundle.json" in shipped
        and "templates/scrape_site.bundle.yaml" in shipped
        and "templates/scraper_events.bundle.yaml" in shipped
    )
    assert "artifacts/workflows/impl_build_mapping.yaml" in shipped
    assert not any(
        p.startswith(("dev-wheels/", ".github/", "tests/contract_examples/")) for p in shipped
    )
    module.ROOT = ROOT
    module.main()
    version = yaml.safe_load((ROOT / "extension.yaml").read_text())["version"]
    archive = ROOT / "dist" / f"flowsteward.web-scraper-{version}.zip"
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
    assert all(n.startswith("flowsteward_web_scraper/") for n in names)
    assert archive.stat().st_size < 50 * 1024 * 1024
