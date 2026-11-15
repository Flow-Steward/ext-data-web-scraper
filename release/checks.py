"""What this bundle must still do on a release candidate.

These assertions are about the Web Scraper extension in particular, so they
live with the bundle rather than in the platform's smoke. They run over the
public API only (`api.request(method, path, body=None) -> (status, payload)`)
and record results with `evidence.add(name, passed, detail)`.

Two kinds of "did not run" are kept apart, as in the reference extensions:

* a precondition the stand can meet (the scraper service address and token,
  given through RELEASE_INPUTS) is a failure naming the cause when missing;
* a limitation of the public surface (no API reads an artifact's bytes, and a
  composite action cannot be called outside a workflow) is recorded as
  NOT RUN with the reason.
"""

from __future__ import annotations

import os
from typing import Any

RELEASE_SCOPES = [
    "extension:invoke",
    "jobs:*",
    "projects:*",
    "read:*",
    "registry:*",
    "workflows:*",
]

RELEASE_EXTENSIONS = ["flowsteward.web-scraper"]

#: The scraper service the candidate reaches. Without it only the static
#: checks run, and the service checks are failures that name the missing input.
RELEASE_INPUTS = [
    "FS_WEB_SCRAPER_SERVICE_URL",
    "FS_WEB_SCRAPER_SERVICE_TOKEN",
]

EXTENSION_ID = "flowsteward.web-scraper"
IMPLEMENTATION = "impl_build_mapping"

#: Declared but not executed by this suite, and why.
UNEXECUTED = {
    "build_mapping": "a composite action runs only as a workflow step with a model route; "
    "the template walk in tests/test_templates.py covers its data flow offline",
    "get_result_artifact_bytes": "no public API reads an artifact's bytes",
    "signed_webhook_delivery": "needs the service to deliver to the candidate's public URL",
}


def _rows(payload: Any, key: str) -> list[dict[str, Any]]:
    rows = payload if isinstance(payload, list) else None
    if rows is None:
        rows = payload.get(key) if isinstance(payload, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def web_scraper_installed(api: Any, evidence: Any) -> None:
    status, payload = api.request("GET", "/api/extensions")
    rows = _rows(payload, "extensions")
    found = [row for row in rows if str(row.get("extension_id") or "") == EXTENSION_ID]
    evidence.add(
        "web_scraper_installed",
        status == 200 and bool(found),
        f"status={status}; found={len(found)}",
    )


def web_scraper_implementation_hidden(api: Any, evidence: Any) -> None:
    """ADR 0065: the implementation workflow is projected hidden."""
    status, payload = api.request("GET", "/api/workflows?per_page=500")
    listed = [
        str(row.get("workflow_id") or "")
        for row in _rows(payload, "workflows")
        if str(row.get("workflow_id") or "").endswith(f".{IMPLEMENTATION}")
    ]
    evidence.add(
        "web_scraper_implementation_hidden",
        status == 200 and not listed,
        f"status={status}; listed={listed}",
    )


def web_scraper_service_reachable(api: Any, evidence: Any) -> None:
    missing = [name for name in RELEASE_INPUTS if not os.environ.get(name)]
    if missing:
        evidence.add(
            "web_scraper_service_reachable",
            False,
            f"precondition missing: {', '.join(missing)} (the stand needs a scraper service)",
        )
        return
    # The connection test runs through the extension's own action with a
    # temporary profile the harness provides as FS_RELEASE_SCRAPER_CONNECTION_ID.
    connection_id = os.environ.get("FS_RELEASE_SCRAPER_CONNECTION_ID", "")
    project_id = os.environ.get("FS_RELEASE_PROJECT_ID", "")
    if not connection_id or not project_id:
        evidence.add(
            "web_scraper_service_reachable",
            False,
            "precondition missing: FS_RELEASE_SCRAPER_CONNECTION_ID / FS_RELEASE_PROJECT_ID",
        )
        return
    status, payload = api.request(
        "POST",
        f"/api/extensions/{EXTENSION_ID}/actions/test_connection",
        {
            "target": {"connection_id": connection_id, "project_id": project_id},
            "context": {"project_id": project_id},
            "values": {},
        },
    )
    ok = status == 200 and isinstance(payload, dict) and "error" not in payload
    evidence.add("web_scraper_service_reachable", ok, f"status={status}")


def web_scraper_unexecuted(_api: Any, evidence: Any) -> None:
    for name, reason in sorted(UNEXECUTED.items()):
        evidence.add(f"web_scraper_{name}", True, f"NOT RUN: {reason}")


RELEASE_CHECKS = {
    "web_scraper_installed": web_scraper_installed,
    "web_scraper_implementation_hidden": web_scraper_implementation_hidden,
    "web_scraper_service_reachable": web_scraper_service_reachable,
    "web_scraper_unexecuted": web_scraper_unexecuted,
}
