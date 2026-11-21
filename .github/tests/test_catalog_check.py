"""The catalog checker runs only as the pinned, digest-verified file."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
from pathlib import Path
from urllib.error import URLError

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "catalog_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("catalog_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_checker_is_fetched_at_a_pinned_commit():
    module = _load()
    assert len(module.CATALOG_REVISION) == 40
    assert module.CATALOG.endswith(module.CATALOG_REVISION)
    assert "/main" not in module.CATALOG


def test_a_changed_checker_is_refused_and_fails_the_job(monkeypatch, tmp_path):
    module = _load()

    def poisoned(url, timeout):  # noqa: ARG001
        return io.BytesIO(b"import os; os.system('replace the archives')")

    monkeypatch.setattr(module.urllib.request, "urlopen", poisoned)
    assert module.main([str(tmp_path / "x-1.0.0.zip")]) == 1


@pytest.mark.parametrize(
    "missing", ["scripts/build_index.py", "categories.json", "verified_publishers.json"]
)
@pytest.mark.parametrize(
    "failure", [URLError("catalog unavailable"), TimeoutError("catalog timed out")]
)
def test_unavailable_checker_or_policy_fails_the_job(monkeypatch, capsys, missing, failure):
    module = _load()
    files = {
        "scripts/build_index.py": b"raise AssertionError('fetch all policies first')",
        "categories.json": b"[]",
        "verified_publishers.json": b"{}",
    }
    if hasattr(module, "CATALOG_DIGESTS"):
        monkeypatch.setattr(
            module,
            "CATALOG_DIGESTS",
            {path: hashlib.sha256(data).hexdigest() for path, data in files.items()},
        )

    def unavailable(url, timeout):  # noqa: ARG001
        path = next(path for path in files if url.endswith("/" + path))
        if path == missing:
            raise failure
        return io.BytesIO(files[path])

    monkeypatch.setattr(module.urllib.request, "urlopen", unavailable)
    assert module.main([]) == 1
    output = capsys.readouterr()
    assert "::error::" in output.out + output.err


# Fail closed on every path (0.1.25): only a clean pass of every given archive exits 0.

FAKE_CHECKER = b"""
class CatalogError(Exception):
    pass


def parse_entry(extension_id, text):
    return {"extension_id": extension_id, "text": text}


def inspect_archive(data, *, entry, version, categories, publishers, target):
    if data == b"refuse":
        raise CatalogError("the catalog refuses it")
    if data == b"crash":
        raise KeyError("the checker crashed")
    return {"extension_id": entry["extension_id"], "version": version}
"""


def _serve(monkeypatch, module, files):
    monkeypatch.setattr(
        module,
        "CATALOG_DIGESTS",
        {path: hashlib.sha256(data).hexdigest() for path, data in files.items()},
    )

    def serve(url, timeout):  # noqa: ARG001
        return io.BytesIO(next(data for path, data in files.items() if url.endswith("/" + path)))

    monkeypatch.setattr(module.urllib.request, "urlopen", serve)


def _checker(monkeypatch, checker=FAKE_CHECKER, categories=b'["data"]'):
    module = _load()
    _serve(
        monkeypatch,
        module,
        {
            "scripts/build_index.py": checker,
            "categories.json": categories,
            "verified_publishers.json": b"{}",
        },
    )
    return module


def _archive(tmp_path, content: bytes, name="acme.thing-1.0.0.zip"):
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


def test_an_accepted_archive_passes(monkeypatch, tmp_path, capsys):
    module = _checker(monkeypatch)
    assert module.main([_archive(tmp_path, b"ok")]) == 0
    assert "the catalog would accept it" in capsys.readouterr().out


@pytest.mark.parametrize(
    "name,payload",
    [
        ("acme.thing-1.2.3.zip", b"1.2.3|"),
        ("acme.thing-1.2.3-dev.6.zip", b"1.2.3-dev.6|"),
        ("acme.thing-1.2.3-linux-amd64.zip", b"1.2.3|linux-amd64"),
        ("acme.thing-1.2.3-linux-arm64.zip", b"1.2.3|linux-arm64"),
        ("acme.thing-1.2.3-dev.6-linux-amd64.zip", b"1.2.3-dev.6|linux-amd64"),
        ("acme.thing-1.2.3-dev.6-linux-arm64.zip", b"1.2.3-dev.6|linux-arm64"),
    ],
)
def test_archive_identity_reaches_the_pinned_inspector(monkeypatch, tmp_path, name, payload):
    checker = FAKE_CHECKER + b'''
def inspect_archive(data, *, entry, version, categories, publishers, target):
    if data.decode() != version + "|" + target:
        raise CatalogError("manifest version or package target disagrees with the filename")
    return {"version": version, "target": target}
'''
    module = _checker(monkeypatch, checker=checker)
    assert module.main([_archive(tmp_path, payload, name)]) == 0


@pytest.mark.parametrize(
    "name",
    ["acme.thing-1.2.zip", "acme.thing-1.2.3-.zip", "acme.thing-1.2.3-dev!.zip"],
)
def test_malformed_prerelease_filenames_fail_closed(monkeypatch, tmp_path, name):
    module = _checker(monkeypatch)
    assert module.main([_archive(tmp_path, b"ok", name)]) == 1


def test_runtime_suffix_cannot_bypass_manifest_identity(monkeypatch, tmp_path):
    checker = FAKE_CHECKER + b'''
def inspect_archive(data, *, entry, version, categories, publishers, target):
    if version != "1.2.3-dev-linux-arm64" or target:
        raise CatalogError("manifest version or package target disagrees with the filename")
'''
    module = _checker(monkeypatch, checker=checker)
    assert module.main([_archive(tmp_path, b"ok", "acme.thing-1.2.3-dev-linux-arm64.zip")]) == 1


def test_prerelease_inspection_refusal_still_fails(monkeypatch, tmp_path):
    module = _checker(monkeypatch)
    assert module.main([_archive(tmp_path, b"refuse", "acme.thing-1.2.3-dev.6.zip")]) == 1


@pytest.mark.parametrize("content", [b"refuse", b"crash"])
def test_a_refused_or_crashed_inspection_fails_the_job(monkeypatch, tmp_path, capsys, content):
    module = _checker(monkeypatch)
    assert (
        module.main([_archive(tmp_path, b"ok", "acme.a-1.0.0.zip"), _archive(tmp_path, content)])
        == 1
    )
    assert "::error::" in capsys.readouterr().err


def test_an_unreadable_or_misnamed_archive_fails_the_job(monkeypatch, tmp_path):
    module = _checker(monkeypatch)
    assert module.main([str(tmp_path / "acme.thing-1.0.0.zip")]) == 1  # not there
    assert module.main([_archive(tmp_path, b"ok", "thing.zip")]) == 1


def test_no_archive_fails_the_job(monkeypatch, capsys):
    module = _checker(monkeypatch)
    assert module.main([]) == 1
    assert "no archive to check" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("checker", "categories"),
    [
        (b"raise ImportError('broken checker')", b'["data"]'),
        (b"x = 1", b'["data"]'),  # loads, but has no inspect_archive
        (FAKE_CHECKER, b"not json"),
    ],
)
def test_a_checker_that_does_not_load_fails_the_job(monkeypatch, tmp_path, checker, categories):
    module = _checker(monkeypatch, checker, categories)
    assert module.main([_archive(tmp_path, b"ok")]) == 1


def test_the_job_runs_the_checker_unguarded():
    """CI runs the checker as its own step, without `|| true` or continue-on-error."""
    ci = (SCRIPT.parents[1] / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "run: python .github/scripts/catalog_check.py dist/*.zip\n" in ci
    assert "continue-on-error" not in ci and "|| true" not in ci
