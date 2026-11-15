"""Webhook and API starts of both templates through the platform's own input code, offline.

Run in the platform's audit image with `core/` of flow-steward/main at /app:

    PYTHONPATH=/app python /ext/tests/tools/check_webhook_inputs.py

`WorkflowEntrypointInputService.build_inputs`
(core/application/workflows/workflow_entrypoint_input_service.py:69-161) refuses
every JSON body key that is not a declared input when the interface is enabled,
`input_mode: fields` and `strict_inputs: true` and no mapping takes the whole body.
The service's event bodies carry more fields than the four inputs, and the contract
may add fields at any time, so `Scraper events` declares `strict_inputs: false`.
Checked: every `event.*.json` and `webhook_event.request.json` contract example and
a body with an unknown extra field, with the guide's blank input mapping and with an
explicit `body.<name>` mapping; then the run-start validation of the same inputs.
`Scrape a site` (strict) is checked for its API/UI start with only declared fields.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

EXT = Path(__file__).resolve().parents[2]
sys.path[:0] = ["/app", str(EXT)]

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


def main() -> int:
    from core.application.registry.workflow_contract import interface_from_settings
    from core.application.registry.workflow_contract_service import WorkflowInterfaceService
    from core.application.workflows.workflow_entrypoint_input_service import (
        WorkflowEntrypointInputService,
        WorkflowEntrypointInputValidationError,
    )

    workflows = {
        name: json.loads((EXT / "templates" / name).read_text())["workflows"][0]
        for name in ("scraper_events.bundle.json", "scrape_site.bundle.json")
    }

    class Interfaces(WorkflowInterfaceService):
        def interface_for_workflow_id(self, workflow_id: str) -> Any:
            return interface_from_settings(workflows[workflow_id]["settings"])

    interfaces = Interfaces()
    service = WorkflowEntrypointInputService(interfaces)
    events = workflows["scraper_events.bundle.json"]
    interface = interfaces.interface_for_workflow_id("scraper_events.bundle.json")
    print(
        f"Scraper events: enabled={interface.enabled} input_mode={interface.input_mode} "
        f"strict_inputs={interface.strict_inputs}"
    )
    names = [row["name"] for row in events["settings"]["callable"]["expected_inputs"]]
    examples = EXT / "tests" / "contract_examples"
    bodies = {
        path.name: json.loads(path.read_text())
        for path in sorted(examples.glob("event.*.json"))
        + [examples / "webhook_event.request.json"]
    }
    extra = dict(bodies["event.run_completed.json"])
    extra["field_added_in_a_later_contract"] = {"any": ["value"]}
    bodies["event with an unknown extra field"] = extra
    mappings = {"blank (the guide)": {}, "explicit body.<name>": {n: f"body.{n}" for n in names}}
    for body_name, body in bodies.items():
        for mapping_name, mapping in mappings.items():
            label = f"{body_name}, mapping {mapping_name}"
            try:
                inputs = service.build_inputs(
                    workflow_id="scraper_events.bundle.json",
                    mapping=mapping,
                    sources={"body": body, "headers": {}, "query": {}, "form": {}, "files": []},
                    fallback_body=body,
                    file_refs=[],
                )
            except WorkflowEntrypointInputValidationError as exc:
                check(False, f"{label}: refused {exc.errors}")
                continue
            check(all(inputs.get(n) == body[n] for n in names), f"{label}: the four inputs filled")
            result = service.validate_inputs(
                "scraper_events.bundle.json", inputs, channel="webhook"
            )
            check(bool(result.ok), f"{label}: run start accepts them ({result.errors})")
            check(all(result.inputs.get(n) == body[n] for n in names), f"{label}: inputs kept")

    print("Scrape a site, API/UI start with declared fields only")
    form = {
        "site_url": "https://books.toscrape.com/",
        "extraction_instructions": "Books",
        "max_records": 10,
    }
    for channel in ("api", "ui"):
        result = service.validate_inputs("scrape_site.bundle.json", form, channel=channel)
        check(bool(result.ok), f"{channel}: accepted ({result.errors})")
    scrape = interfaces.interface_for_workflow_id("scrape_site.bundle.json")
    check(bool(scrape.strict_inputs), "Scrape a site keeps strict inputs (a typo is still refused)")
    typo = service.validate_inputs(
        "scrape_site.bundle.json", {**form, "max_record": 5}, channel="api"
    )
    check(not typo.ok, "an undeclared input is refused there")

    print("\nRESULT:", "OK" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
