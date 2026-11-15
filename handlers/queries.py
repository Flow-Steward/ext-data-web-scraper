"""Page queries. A query gets no project connection from the host, so these never call the service."""

from __future__ import annotations

from typing import Any

from handlers.common import ok

SETUP_STEPS = [
    {
        "message": "1. Start the scraper service (compose file of the web-scraper-service repository).",
        "severity": "info",
    },
    {
        "message": "2. Create a scraper profile on the Profiles page and press 'Test service'.",
        "severity": "info",
    },
    {
        "message": "3. Import 'Scraper events', enable it, add a signed incoming webhook and copy its "
        "URL and secret into the profile.",
        "severity": "info",
    },
    {
        "message": "4. Import 'Scrape a site', enable it and run it with a site, a description and "
        "max_records 10.",
        "severity": "info",
    },
]


def extension_health(_query: dict[str, Any]) -> dict[str, Any]:
    checks = [
        {"name": "extension_runtime", "status": "ok", "message": "The extension runtime answers."},
        {
            "name": "scraper_service",
            "status": "unknown",
            "message": "Checked per profile: open the Profiles page, choose a profile and press "
            "'Test service'.",
        },
    ]
    return ok(
        {"status": "healthy", "configured": True, "checks": checks},
        items=SETUP_STEPS,
        summary={"setup_steps": len(SETUP_STEPS)},
    )


QUERY_HANDLERS = {"extension_health": extension_health}
