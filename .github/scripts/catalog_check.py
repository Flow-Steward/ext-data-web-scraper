#!/usr/bin/env python3
"""Check packaged archives with the extension catalog's own checker.

    python3 .github/scripts/catalog_check.py dist/*.zip

Fetches scripts/build_index.py from github.com/Flow-Steward/extensions and runs
its inspect_archive on every archive, so a release the catalog would refuse
fails here, before it is published. A failed catalog download blocks publication.

The checker runs as code in this job, so it is fetched at a pinned commit and
must match a recorded SHA-256: a change to the catalog's main branch cannot run
here until this file is updated to the new revision and digests.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

CATALOG_REVISION = "f21511cba4bba6e283d74494e87f9a69e0c342ab"
CATALOG = f"https://raw.githubusercontent.com/Flow-Steward/extensions/{CATALOG_REVISION}"
#: SHA-256 of each file at CATALOG_REVISION; update together with the revision.
CATALOG_DIGESTS = {
    "scripts/build_index.py": "aef2c2d9b134b35387503e085bd61afb48244393418229e63aef27b7762a6e6d",
    "categories.json": "c80cfd8078c4a24613261a2eefe6bb1ff5c98a3503d17436eac339f548dfe9b1",
    "verified_publishers.json": "9b9d8903a45d34f6e81739b609e406d39cdfd250b2f47e506f25952591ff98b8",
}
_NAME = re.compile(
    r"^(?P<id>[a-z0-9.-]+?)-(?P<version>\d+\.\d+\.\d+)(?:-(?P<target>linux-amd64|linux-arm64))?\.zip$"
)


def _repository() -> str:
    """https://github.com/<owner>/<repo> of this repository (GITHUB_REPOSITORY in Actions)."""
    import os
    import subprocess

    slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not slug:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=False
        ).stdout.strip()
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", remote)
        slug = match.group(1) if match else "owner/repo"
    return f"https://github.com/{slug}"


class CatalogDigestMismatch(Exception):
    """A fetched catalog file is not the one this repository pinned."""


def _fetch(path: str) -> bytes:
    with urllib.request.urlopen(f"{CATALOG}/{path}", timeout=60) as response:  # noqa: S310 - fixed https host
        data = response.read()
    if hashlib.sha256(data).hexdigest() != CATALOG_DIGESTS[path]:
        raise CatalogDigestMismatch(
            f"{path} at {CATALOG_REVISION} does not match its pinned SHA-256"
        )
    return data


def _error(message: str) -> int:
    print(f"::error::{message}", file=sys.stderr)
    return 1


def _load_catalog() -> tuple[object, set[str], dict[str, str]]:
    """The pinned checker module, the category ids and the verified publishers.

    Raises on anything short of all three, verified: the caller fails the job."""
    source = _fetch("scripts/build_index.py")
    categories = set(json.loads(_fetch("categories.json")))
    publishers = json.loads(_fetch("verified_publishers.json"))
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "build_index.py"
        script.write_bytes(source)
        spec = importlib.util.spec_from_file_location("catalog_build_index", script)
        if spec is None or spec.loader is None:
            raise ImportError("the catalog checker cannot be loaded")
        catalog = importlib.util.module_from_spec(spec)
        # Registered first: its dataclasses resolve their own module by name.
        sys.modules[spec.name] = catalog
        spec.loader.exec_module(catalog)
    missing = [
        name
        for name in ("parse_entry", "inspect_archive", "CatalogError")
        if not hasattr(catalog, name)
    ]
    if missing:
        raise ImportError(f"the catalog checker lacks {', '.join(missing)}")
    return catalog, categories, publishers


def main(paths: list[str]) -> int:
    """0 only when every given archive passed the pinned checker; never a silent skip.

    A failed or tampered download, a checker that does not load, no archive at all, an
    archive that cannot be read and every refusal or crash of the inspection exit 1."""
    try:
        catalog, categories, publishers = _load_catalog()
    except CatalogDigestMismatch as exc:
        return _error(str(exc))
    except OSError as exc:
        return _error(f"catalog checker unavailable ({exc}); archives were not checked against it")
    except Exception as exc:  # noqa: BLE001 - any other failure must fail the job too
        return _error(f"catalog checker could not be loaded ({type(exc).__name__}: {exc})")
    if not paths:
        return _error("no archive to check (expected dist/<extension_id>-<version>.zip)")
    failures = 0
    for path in paths:
        match = _NAME.match(Path(path).name)
        if not match:
            _error(f"{path}: not named <extension_id>-<version>-<target>.zip")
            failures += 1
            continue
        try:
            entry = catalog.parse_entry(match["id"], f"repository: {_repository()}\n")  # type: ignore[attr-defined]
            catalog.inspect_archive(  # type: ignore[attr-defined]
                Path(path).read_bytes(),
                entry=entry,
                version=match["version"],
                categories=categories,
                publishers=publishers,
                target=match["target"] or "",
            )
        except catalog.CatalogError as exc:  # type: ignore[attr-defined]
            _error(f"{path}: {exc}")
            failures += 1
            continue
        except Exception as exc:  # noqa: BLE001 - an inspection that crashes is no pass
            _error(f"{path}: the catalog checker failed ({type(exc).__name__}: {exc})")
            failures += 1
            continue
        print(f"{Path(path).name}: the catalog would accept it")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
