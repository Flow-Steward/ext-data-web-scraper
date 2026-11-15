"""Write the extension's own read-only workflows from the project-copy templates.

    python3 .github/scripts/extension_workflows.py          # write artifacts/workflows/*.yaml
    python3 .github/scripts/extension_workflows.py --check  # fail when a file is stale

`templates/*.bundle.json` stay the source and the import path for project copies
(an imported copy is the account's own workflow; Flow Steward fills connection
slots only for a workflow an extension install provides, 0.9.18
`installed_connection_slots`). From each copy this script makes the extension
workflow listed in extension-descriptor.yaml:

- one connection slot `scraper` (ADR 0068); every connector step reads it and
  the build_mapping action Invoke passes it on (named by extension and action;
  the install and the connection come from the run's pinned slots);
- `settings.portability.allow_full_export: true`: the logic is visible, and
  export and clone are allowed;
- hooks (ADR 0038) where each project adds its own steps: `review_mapping` and
  `before_start` in Scrape a site, `on_error` in Scraper events. An empty hook
  passes its inputs through, so it changes nothing;
- canvas notes that point to the hooks instead of "place for your own steps".
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "artifacts" / "workflows"
EXTENSION_ID = "flowsteward.web-scraper"
SLOT = "scraper"
SLOT_DECLARATION = {
    "slot": SLOT,
    "extension_id": EXTENSION_ID,
    "connection_type_ids": ["scraper_profile"],
    "label": "Scraper profile",
}
#: template file -> extension workflow file (the local workflow id)
WORKFLOWS = {
    "scrape_site": "scrape_a_site",
    "scraper_events": "scraper_events",
    "service_maintenance": "service_maintenance",
}
HEADER = (
    "# Generated from templates/{source} by .github/scripts/extension_workflows.py; edit the\n"
    "# template JSON. The extension's read-only workflow (connection slot, hooks).\n"
)

REVIEW_MAPPING = {
    "hook_id": "review_mapping",
    "contract_version": 1,
    "display_name": "Review the mapping",
    "description": "Runs after the mapping was built or a stored one passed on the samples, "
    "before it is tested again and activated. Your steps may change the mappings; the "
    "changed mappings must pass the test, or nothing is activated.",
    "input_schema": {
        "type": "object",
        "properties": {
            "site": {"type": "string"},
            "mappings": {"type": "array", "items": {"type": "object"}},
            "field_results": {"type": "array"},
            "summary": {"type": ["string", "null"]},
        },
        "required": ["site", "mappings"],
    },
    "input_mapping": {
        "site": "state.site",
        "mappings": "state.mappings",
        "field_results": "state.field_results",
        "summary": "state.mapping_summary",
    },
    "mutable_fields": ["mappings"],
    "stage_contract": {
        "summary": "After the mapping passed on the samples; before it is tested again and "
        "activated. Nothing is crawled yet.",
        "effects_before": [],
        "effects_after": ["mapping.activate", "run.start"],
    },
}
BEFORE_START = {
    "hook_id": "before_start",
    "contract_version": 1,
    "display_name": "Before the crawl starts",
    "description": "Runs after the mapping is active, just before the crawl starts. Your "
    "steps may change the run configuration, or set skip_run to start no crawl.",
    "input_schema": {
        "type": "object",
        "properties": {
            "config": {"type": "object"},
            "site": {"type": "string"},
            "mapping_summary": {"type": ["string", "null"]},
        },
        "required": ["config", "site"],
    },
    "input_mapping": {
        "config": "state.config",
        "site": "state.site",
        "mapping_summary": "state.mapping_summary",
    },
    "mutable_fields": ["config"],
    "additional_outputs": {"skip_run": {"schema": {"type": "boolean"}, "default": False}},
    "stage_contract": {
        "summary": "After the mapping was activated; before the crawl starts.",
        "effects_before": ["mapping.activate"],
        "effects_after": ["run.start"],
    },
}
ON_ERROR = {
    "hook_id": "on_error",
    "contract_version": 1,
    "display_name": "On a problem or a failed run",
    "description": "Runs when the service reports a problem or a failed run, after the run "
    "summary was read, before the event is marked handled. For example notify someone.",
    "input_schema": {
        "type": "object",
        "properties": {
            "event_type": {"type": "string"},
            "run_id": {"type": "string"},
            "state": {"type": ["string", "null"]},
            "terminal": {"type": ["boolean", "null"]},
            "message": {"type": ["string", "null"]},
            "explanation": {"type": ["string", "null"]},
            "error": {"type": ["object", "null"]},
            "errors": {"type": ["array", "null"]},
        },
        "required": ["event_type", "run_id"],
    },
    "input_mapping": {
        "event_type": "state.inputs.event_type",
        "run_id": "state.inputs.run_id",
        "state": "state.problem.state",
        "terminal": "state.problem.terminal",
        "message": "state.problem.message",
        "explanation": "state.problem.explanation",
        "error": "state.problem.error",
        "errors": "state.problem.errors",
    },
    "mutable_fields": [],
    "stage_contract": {
        "summary": "After the run summary of a problem or failed run was read; before the "
        "event is marked handled.",
        "effects_before": [],
        "effects_after": ["event.complete"],
    },
}


def _hook_step(declaration: dict[str, Any], description: str) -> dict[str, Any]:
    return {
        "step_id": declaration["hook_id"],
        "display_name": declaration["display_name"],
        "step_kind": "invoke_workflow",
        "description": description,
        "override": {"invoke_workflow": {"hook": copy.deepcopy(declaration)}},
    }


def _edge(source: str, outcome: str, target: str) -> dict[str, Any]:
    return {
        "source": {"kind": "step", "id": source},
        "outcome": {"name": outcome},
        "target": {"kind": "step", "id": target},
        "rework": False,
    }


def _insert_before(workflow: dict[str, Any], hook: dict[str, Any], target: str) -> None:
    """Every edge into *target* now leads to the hook; the hook's success leads on."""
    steps = workflow["steps"]
    index = next(n for n, s in enumerate(steps) if s["step_id"] == target)
    steps.insert(index, hook)
    for edge in workflow["execution_graph"]["edges"]:
        if edge["target"]["id"] == target:
            edge["target"]["id"] = hook["step_id"]
    workflow["execution_graph"]["edges"].append(_edge(hook["step_id"], "success", target))


def _step(workflow: dict[str, Any], step_id: str) -> dict[str, Any]:
    return next(s for s in workflow["steps"] if s["step_id"] == step_id)


def _scrape_a_site(workflow: dict[str, Any]) -> None:
    _insert_before(
        workflow,
        _hook_step(
            REVIEW_MAPPING,
            "Hook: your steps may change the mapping before it is tested again and activated.",
        ),
        "retest_mapping",
    )
    reviewed = "state.hooks.review_mapping.output.mappings"
    _step(workflow, "retest_mapping")["input_mapping_jsonb"]["mappings"] = reviewed
    _step(workflow, "activate_mapping")["input_mapping_jsonb"]["mappings"] = reviewed
    # before_start, then 'Skip the crawl?', then Start run (start_crawl).
    hook = _hook_step(
        BEFORE_START,
        "Hook: your steps may change the run configuration or skip the crawl.",
    )
    steps = workflow["steps"]
    index = next(n for n, s in enumerate(steps) if s["step_id"] == "start_crawl")
    skip_if = {
        "step_id": "skip_run",
        "display_name": "Skip the crawl?",
        "step_kind": "if",
        "description": "Your before_start hook can set skip_run; then no crawl starts.",
        "override": {
            "if": {
                "condition": {
                    "selector": "state.hooks.before_start.output.skip_run",
                    "operator": "truthy",
                }
            }
        },
    }
    skipped = {
        "step_id": "run_skipped",
        "display_name": "Crawl skipped",
        "step_kind": "stop_success",
        "description": "Your before_start hook asked to skip the crawl.",
        "override": {
            "stop_success": {
                "message": "Your before_start hook set skip_run, so no crawl was started. "
                "The mapping is active."
            }
        },
    }
    steps[index:index] = [hook, skip_if]
    steps.append(skipped)
    edges = workflow["execution_graph"]["edges"]
    for edge in edges:
        if edge["target"]["id"] == "start_crawl":
            edge["target"]["id"] = "before_start"
    edges += [
        _edge("before_start", "success", "skip_run"),
        _edge("skip_run", "true", "run_skipped"),
        _edge("skip_run", "false", "start_crawl"),
    ]
    _step(workflow, "start_crawl")["input_mapping_jsonb"]["config"] = (
        "state.hooks.before_start.output.config"
    )


def _scraper_events(workflow: dict[str, Any]) -> None:
    _insert_before(
        workflow,
        _hook_step(
            ON_ERROR,
            "Hook: your steps for a problem or a failed run, e.g. a notification.",
        ),
        "complete_event",
    )
    # Only the problem branch leads through on_error: the run_completed and
    # mapping paths go to Complete event directly, as before.
    for edge in workflow["execution_graph"]["edges"]:
        if edge["target"]["id"] == "on_error" and edge["source"]["id"] != "problem_summary":
            edge["target"]["id"] = "complete_event"


NOTES = {
    "scrape_a_site": "## Your own steps\nThis workflow belongs to the Web Scraper extension "
    "and is read-only. Add your steps in its hooks: 'Review the mapping' (before the mapping "
    "is activated) and 'Before the crawl starts'. An empty hook changes nothing. To process "
    "the files, call this workflow from your own workflow with Invoke. Guide page: Read-only "
    "workflows and hooks; Use Scrape a site from your own workflow.",
    "scraper_events": "## Your own steps\nThis workflow belongs to the Web Scraper extension "
    "and is read-only. Add steps for problems and failed runs in its hook 'On a problem or a "
    "failed run'. Guide page: Read-only workflows and hooks.",
    "service_maintenance": "## Your own steps\nThis workflow belongs to the Web Scraper "
    "extension and is read-only. Give it a weekly schedule on its Schedules tab.",
}


def _notes(workflow_id: str, workflow: dict[str, Any]) -> None:
    kept = [
        note
        for note in workflow["settings"].get("canvas_annotations", [])
        if not note["text"].startswith("## Place for your own steps")
        and not note["text"].startswith("## Keep the clean-up ages")
    ]
    bottom = max((n["y"] + n["height"] for n in kept), default=-40) + 40
    kept.append(
        {
            "id": "note_hooks",
            "text": NOTES[workflow_id],
            "x": -640,
            "y": bottom,
            "width": 420,
            "height": 240,
        }
    )
    workflow["settings"]["canvas_annotations"] = kept


def extension_workflow(template: str, workflow: dict[str, Any]) -> dict[str, Any]:
    workflow_id = WORKFLOWS[template]
    out = copy.deepcopy(workflow)
    out["workflow_id"] = workflow_id
    out["description"] = (
        out["description"].rstrip(".")
        + ". Provided by the Web Scraper extension: read-only, with hooks for your steps."
    )
    if workflow_id == "scrape_a_site":
        _scrape_a_site(out)
    elif workflow_id == "scraper_events":
        _scraper_events(out)
    for step in out["steps"]:
        if step.get("step_kind") == "connector":
            step["connection_binding_jsonb"] = {"slot": SLOT}
        invoke = (step.get("override") or {}).get("invoke_workflow") or {}
        if "action" in invoke:
            # Named by extension and action; the install and the connection come from
            # the run's pinned slots (0.9.18 composite_action_runtime.plan_action_invoke).
            invoke["action"] = {
                "extension_id": EXTENSION_ID,
                "action_id": invoke["action"]["action_id"],
            }
            invoke.pop("workflow_id", None)
            invoke["connections"] = {name: {"slot": SLOT} for name in invoke["connections"]}
    _notes(workflow_id, out)
    settings = out["settings"]
    settings["execution_graph"] = out.pop("execution_graph")
    settings["connection_slots"] = [dict(SLOT_DECLARATION)]
    settings["portability"] = {"allow_full_export": True}
    out["enabled"] = True
    out.pop("participants", None)
    return out


def render(template: str) -> str:
    source = f"{template}.bundle.json"
    bundle = json.loads((ROOT / "templates" / source).read_text(encoding="utf-8"))
    body = yaml.safe_dump(
        extension_workflow(template, bundle["workflows"][0]),
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )
    return HEADER.format(source=source) + body


def main(argv: list[str]) -> int:
    check = "--check" in argv
    failed = False
    for template, workflow_id in WORKFLOWS.items():
        target = TARGET / f"{workflow_id}.yaml"
        text = render(template)
        if check:
            if not target.exists() or target.read_text(encoding="utf-8") != text:
                print(
                    f"{target.relative_to(ROOT)} is stale; run .github/scripts/extension_workflows.py"
                )
                failed = True
        else:
            target.write_text(text, encoding="utf-8")
            print(f"wrote {target.relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
