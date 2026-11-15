"""Offline platform checks of the Web Scraper extension against flow-steward/main code.

Run inside flow-steward-fs-audit with the platform export at /app (read-only)
and the extension at /ext. No database: DB lookups are replaced by the
extension's own release data where noted.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, "/app")
import os

EXT = Path(os.environ.get("CHECK_EXT", "/ext"))
SKIP_VALIDATE = os.environ.get("SKIP_VALIDATE") == "1"
INSTALL = "pi_0123456789abcdef"  # a mapped install id
CONNECTION = "7d6f6a8e-1111-4222-8333-444455556666"  # a mapped project connection
problems: list[str] = []


def report(title, issues):
    issues = list(issues)
    print(f"== {title}: {'none' if not issues else len(issues)}")
    for issue in issues:
        print("   ", json.dumps(issue, default=str)[:400])
        problems.append(f"{title}: {issue}")


# 1. extensions validate --health --marketplace
from core.application.tooling.extensions.models import ExtensionValidationRequest  # noqa: E402
from core.application.tooling.extensions.validate_extensions import validate_extensions  # noqa: E402

if not SKIP_VALIDATE:
    result = validate_extensions(
        ExtensionValidationRequest(
            extension_id="flowsteward.web-scraper", root="/exts", health=True, marketplace=True
        )
    )
    print("== extensions validate:", result.status, "exit", result.exit_code)
    print(result.output.strip()[:3000])
    if result.exit_code != 0:
        problems.append("extensions validate failed")

# 2. code review scanner (what the extension page lists)
from core.infrastructure.extension_runtime.extension_code_scan import scan_extension_code  # noqa: E402

manifest = yaml.safe_load((EXT / "extension.yaml").read_text())
entry = manifest.get("entrypoint") or manifest.get("runtime") or {}
commands = []
for label, block in (
    ("entrypoint", manifest.get("entrypoint")),
    ("health", manifest.get("health")),
):
    if isinstance(block, dict) and block.get("command"):
        commands.append((label, block["command"]))
errors, warnings = scan_extension_code(EXT, commands)
report("code scan errors", errors)
report("code scan review items (warnings)", warnings)

# 3. definition validator, selectors, step config keys, reserved write targets
from core.application.orchestration.workflow_state_selector_contract import (  # noqa: E402
    validate_workflow_state_selectors,
)
from core.application.registry.contract_validation_workflow import (  # noqa: E402
    _validate_step_state_write_targets,
)
from core.application.workflows.workflow_definition_validation import (  # noqa: E402
    validate_workflow_definition,
)
from core.application.workflows.workflow_step_config_contracts import (  # noqa: E402
    validate_step_config_keys,
)

impl = yaml.safe_load((EXT / "artifacts/workflows/impl_build_mapping.yaml").read_text())
impl_def = {
    "steps": impl["steps"],
    "groups": impl.get("groups", []),
    "for_each": impl.get("for_each", []),
    "execution_graph": impl["settings"]["execution_graph"],
}
report("validate_workflow_definition impl_build_mapping", validate_workflow_definition(impl_def))
report("state selectors impl_build_mapping", validate_workflow_state_selectors(impl))


def step_checks(name, workflow):
    issues = []
    for step in workflow["steps"]:
        path = f"steps.{step['step_id']}"
        issues += validate_step_config_keys(
            step_kind=step["step_kind"], override=step.get("override"), step_path=path
        )
        issues += _validate_step_state_write_targets(step, path)
    report(f"step config keys + reserved write targets {name}", issues)


step_checks("impl_build_mapping", impl)

# 4. bundle parser, topology, discovery, then the importer's rewrite
from core.application.extensions.projected_dataset_bridge import projection_bridge_id  # noqa: E402
from core.application.workflows.workflow_action_contract import (  # noqa: E402
    action_config_issues,
    action_connections,
    parse_action_ref,
)
from core.application.workflows.workflow_portability import WorkflowPortabilityService  # noqa: E402

service = WorkflowPortabilityService.__new__(WorkflowPortabilityService)


class _NoDB:
    """No database: every lookup the export mixin tries is absent."""


service.db = _NoDB()
imported = {}
for name in (
    "scrape_site.bundle.yaml",
    "scraper_events.bundle.yaml",
    "service_maintenance.bundle.yaml",
    "scrape_site.bundle.json",
):
    text = (EXT / "templates" / name).read_text()
    bundle = service.parse_bundle(text)
    print(
        f"== parse_bundle {name}: ok, workflows {[w['workflow_id'] for w in bundle['workflows']]}"
    )
    if name.endswith(".json"):
        continue
    workflow = bundle["workflows"][0]
    report(f"topology {name}", service._topology_issues(bundle["workflows"]))
    deps = service.discover_dependencies(workflow)
    rows = deps if isinstance(deps, list) else deps.get("dependencies", deps)
    print(f"== discover_dependencies {name}:")
    for row in rows if isinstance(rows, list) else []:
        print(
            "    ",
            row.get("dependency_type"),
            row.get("ref"),
            "sources",
            len(row.get("sources") or []),
        )
    resolution_index = {
        ("extension", "flowsteward.web-scraper"): {"action": "map_existing", "target_ref": INSTALL},
        ("project_connection", "scraper_profile"): {
            "action": "map_existing",
            "target_ref": CONNECTION,
        },
    }
    payload = service._import_payload(
        copy.deepcopy(workflow), workflow["workflow_id"], resolution_index
    )
    imported[name] = payload
    issues = []
    for step in payload["steps"]:
        sid = step["step_id"]
        if step["step_kind"] == "connector":
            if step.get("connector_install_id") != INSTALL:
                issues.append(f"{sid}: connector_install_id {step.get('connector_install_id')!r}")
            if step.get("connection_binding_jsonb") != {"connection_id": CONNECTION}:
                issues.append(f"{sid}: binding {step.get('connection_binding_jsonb')}")
        invoke = (step.get("override") or {}).get("invoke_workflow")
        if invoke:
            issues += [f"{sid}: {i}" for i in action_config_issues(invoke)]
            ref = parse_action_ref(invoke["action"])
            # workflow_action_validation.py:113: workflow_id must equal the
            # projected id of the action's implementation in the chosen install.
            expected = projection_bridge_id(ref.install_id, "impl_build_mapping")
            if ref.install_id != INSTALL or invoke.get("workflow_id") != expected:
                issues.append(f"{sid}: workflow_id {invoke.get('workflow_id')} != {expected}")
            if (action_connections(invoke).get("profile") or {}).get("connection_id") != CONNECTION:
                issues.append(f"{sid}: action connection {invoke.get('connections')}")
    report(f"importer rewrite {name} (install/connection/invoke id)", issues)
    defn = {k: payload.get(k) for k in ("steps", "groups", "for_each")}
    defn["execution_graph"] = (
        payload.get("execution_graph")
        or payload["settings"].get("execution_graph")
        or workflow["execution_graph"]
    )
    report(f"validate_workflow_definition after import {name}", validate_workflow_definition(defn))
    report(f"state selectors after import {name}", validate_workflow_state_selectors(payload))
    step_checks(f"after import {name}", payload)

# 5. readiness connector check with the release's own contract instead of the DB
import core.application.workflows.extension_connection_validation as ecv  # noqa: E402

operations = yaml.safe_load((EXT / "contracts/operation_manifest.yaml").read_text())["operations"]
contract = {"extension_id": "flowsteward.web-scraper", "operation_manifest": operations}
ecv._get_install_contract = lambda db, install_id, account_id: (contract, "install")
ecv._install_state_issue = lambda *a, **k: None
ecv._projected_extension_tool = lambda *a, **k: (None, False)
ecv._get_connection_metadata = lambda db, connection_id: {
    "connection_id": connection_id,
    "connection_type_id": "scraper_profile",
}
ecv._connection_matches = lambda connection, **k: bool(connection)
for name, payload in imported.items():
    issues = []
    for step in payload["steps"]:
        if step["step_kind"] == "connector":
            issues += ecv.validate_extension_connector_step(
                None, step=step, step_path=f"steps.{step['step_id']}", account_id="acc"
            )
    report(f"validate_extension_connector_step {name} (contract from release)", issues)
    # Control: the old binding key must be refused by the same code.
    old = copy.deepcopy(next(s for s in payload["steps"] if s["step_kind"] == "connector"))
    old["connection_binding_jsonb"] = {"connection_ref": CONNECTION}
    control = ecv.validate_extension_connector_step(
        None, step=old, step_path="control", account_id="acc"
    )
    print(f"   control with connection_ref: {[i['code'] for i in control]}")

print("\nRESULT:", "OK" if not problems else f"{len(problems)} problem(s)")

# 6. B4/M1 with the runtime's own resolver: connector inputs against a run that
# supplied only its required inputs (run start leaves the optional ones out).
from core.application.orchestration.connector_mapping import _render_input_payload  # noqa: E402

for name, workflow in [("impl_build_mapping", impl), *imported.items()]:
    rows = workflow["settings"]["callable"]["expected_inputs"]
    state = {"inputs": {r["name"]: "x" for r in rows if r.get("required")}}
    issues, defaults = [], {}
    for step in workflow["steps"]:
        if step["step_kind"] != "connector":
            continue
        payload, unresolved = _render_input_payload(step.get("input_mapping_jsonb") or {}, state)
        issues += [
            f"{step['step_id']}.{k} <- {v}"
            for k, v in unresolved.items()
            if v.startswith("state.inputs.")
        ]
        for key, selector in (step.get("input_mapping_jsonb") or {}).items():
            if isinstance(selector, str) and "|default" in selector:
                defaults[f"{step['step_id']}.{key}"] = payload.get(key)
    report(f"connector_mapping._render_input_payload, required inputs only, {name}", issues)
    print("    defaults resolved to:", json.dumps(defaults))
print("\nRESULT (with resolver):", "OK" if not problems else f"{len(problems)} problem(s)")

# 7. A40: what the job's Result panel gets, with the platform's own interface code.
# WorkflowInterface.from_settings derives the returned files from the steps
# (workflow_returned_files.derive_returned_files); captured_returned_files copies a
# step's file into the run's returned files; collect_runtime_outputs reads the
# declared outputs from run state (build_public_result shows outputs and files).
from core.application.registry.workflow_contract import WorkflowInterface  # noqa: E402
from core.application.registry.workflow_contract_service import (  # noqa: E402
    collect_runtime_outputs,
)
from core.application.registry.workflow_returned_files import (  # noqa: E402
    captured_returned_files,
    derive_returned_files,
    returned_files_for_step,
)

for name, payload in imported.items():
    interface = WorkflowInterface.from_settings(payload["settings"], steps=payload["steps"])
    files = [
        (r.name, r.source.step_id, r.source.result, r.source.target)
        for r in interface.returned_files
    ]
    print(
        f"== interface {name}: output_mode {interface.output_mode}, outputs "
        f"{[r.name for r in interface.expected_outputs]}, returned files {files}"
    )
    report(f"interface definition issues {name}", interface.definition_issues())
    if name == "scrape_site.bundle.yaml":
        # 0.1.23: Scrape a site returns the files and the summary itself.
        derived, _issues = derive_returned_files(
            payload["steps"],
            legacy_declarations=payload["settings"]["callable"].get("expected_artifacts") or [],
            taken_names=[r.name for r in interface.expected_outputs],
        )
        captured = {}
        for step_id, handle in (("get_result", "artifact:res"), ("get_result_csv", "artifact:csv")):
            rows = returned_files_for_step(derived, step_id)
            target = "result" if step_id == "get_result" else "result_csv"
            update = {"workflow_data": {target: {"artifact_handle": handle}}}
            captured.update(captured_returned_files(state={}, update=update, returned=rows))
        print("    captured after Get result / Get result CSV:", captured)
        if captured != {"result_file": "artifact:res", "result_csv": "artifact:csv"}:
            problems.append(f"Scrape a site: returned files not captured: {captured}")
        state = {
            "workflow_data": {
                "scraper_run_id": "run_1",
                "run_summary": "Run is completed.",
                "result": {"artifact_handle": "artifact:res", "record_count": 3},
                "result_preview": [{"title": "A"}],
            },
            "artifacts": captured,
        }
        outputs, returned = collect_runtime_outputs(state, interface=interface)
        print("    collect_runtime_outputs:", sorted(outputs), returned)
        if not {"run_summary", "result", "result_preview", "scraper_run_id"} <= set(outputs):
            problems.append(f"Scrape a site outputs not collected: {sorted(outputs)}")
    if name != "scraper_events.bundle.yaml":
        continue
    derived, _issues = derive_returned_files(
        payload["steps"],
        legacy_declarations=payload["settings"]["callable"].get("expected_artifacts") or [],
        taken_names=[r.name for r in interface.expected_outputs],
    )
    rows = returned_files_for_step(derived, "get_result")
    update = {"workflow_data": {"result": {"artifact_handle": "artifact:res_out"}}}
    artifacts = captured_returned_files(state={}, update=update, returned=rows)
    print("    captured after Get result:", artifacts)
    if artifacts != {"result_file": "artifact:res_out"}:
        problems.append(f"returned file not captured: {artifacts}")
    state = {
        "workflow_data": {
            "run_summary": "Run is completed.",
            "result": {"artifact_handle": "artifact:res_out", "record_count": 3},
            "result_preview": [{"title": "A"}],
        },
        "artifacts": artifacts,
    }
    outputs, returned = collect_runtime_outputs(state, interface=interface)
    print("    collect_runtime_outputs:", sorted(outputs), returned)
    if sorted(outputs) != ["result", "result_preview", "run_summary"] or not returned:
        problems.append(f"outputs not collected: {sorted(outputs)} {returned}")
print("\nRESULT (with A40 interface):", "OK" if not problems else f"{len(problems)} problem(s)")


# 8. ADR 0068 / ADR 0038 (0.9.18): the extension's own workflows with the slot
# `scraper` and hooks, as the descriptor publishes them.
try:
    from core.application.extensions.workflow_connection_slot_bundle import (
        connection_slot_descriptor_issues,
    )
    from core.application.workflows.workflow_connection_slots import (
        connection_slots_of,
        undeclared_slot_issues,
    )
    from core.application.workflows.workflow_hook_contract import invoke_config_issues
except ImportError:  # a platform before 0.9.18
    print("== connection slots: not in this platform (before 0.9.18)")
else:
    descriptor = yaml.safe_load((EXT / "extension-descriptor.yaml").read_text())
    published = {
        row["workflow_id"]: yaml.safe_load((EXT / row["ref"]).read_text())
        for row in descriptor["artifacts"]["workflows"]
        if row["workflow_id"] != "impl_build_mapping"
    }
    descriptor["artifacts"]["workflows"] = [
        {**row, "snapshot": yaml.safe_load((EXT / row["ref"]).read_text())}
        for row in descriptor["artifacts"]["workflows"]
    ]
    report(
        "connection slots of the published workflows (descriptor check)",
        connection_slot_descriptor_issues(descriptor),
    )
    for wid, wf in published.items():
        slots = [s.slot for s in connection_slots_of(wf)]
        hooks = [
            s["override"]["invoke_workflow"]["hook"]["hook_id"]
            for s in wf["steps"]
            if "hook" in ((s.get("override") or {}).get("invoke_workflow") or {})
        ]
        print(f"== {wid}: slots {slots}, hooks {hooks}")
        defn = {
            "steps": wf["steps"],
            "groups": wf.get("groups", []),
            "for_each": wf.get("for_each", []),
            "execution_graph": wf["settings"]["execution_graph"],
        }
        report(f"validate_workflow_definition {wid}", validate_workflow_definition(defn))
        report(f"state selectors {wid}", validate_workflow_state_selectors(wf))
        step_checks(wid, wf)
        report(
            f"invoke and hook declarations {wid}",
            [
                issue
                for step in wf["steps"]
                if step["step_kind"] == "invoke_workflow"
                for issue in invoke_config_issues(step["override"]["invoke_workflow"])
            ],
        )
        report(
            f"readiness undeclared slots {wid}",
            [
                issue
                for step in wf["steps"]
                if step.get("step_kind") == "connector"
                for issue in undeclared_slot_issues(
                    wf, step=step, step_path=f"steps.{step['step_id']}"
                )
            ],
        )
    print(
        "\nRESULT (with extension workflows):",
        "OK" if not problems else f"{len(problems)} problem(s)",
    )


# 9. ADR 0071 (0.9.18 "Wait for an event"): Scrape a site's Wait, and the resume_waits
# of a real claim_event response (the extension against the fake service) through the
# host's own normalisation, item checks and delivery (with an in-memory inbox).
try:
    from core.application.jobs.workflow_wait_event_delivery import (
        checked_wait_event,
        deliver_wait_events,
    )
    from core.application.workflows.workflow_wait_event_rules import wait_event_config_issues
except ImportError:  # a platform without ADR 0071
    print("== event waits: not in this platform")
else:
    from core.application.orchestration.wait_event_runtime import (
        event_pending_wait,
        event_wake_verdict,
        resolve_wait_event,
    )
    from core.application.workflows.workflow_definition_rules import WAIT_DURATION_MS_MAX
    from core.infrastructure.db.extension_response import normalize_extension_response

    variants = {
        "scrape_site.bundle.yaml (imported copy)": imported["scrape_site.bundle.yaml"],
        "scrape_a_site (published)": yaml.safe_load(
            (EXT / "artifacts/workflows/scrape_a_site.yaml").read_text()
        ),
    }
    for label, wf in variants.items():
        wait = next(st for st in wf["steps"] if st["step_id"] == "wait_for_run")
        report(
            f"wait_event_config_issues {label}",
            wait_event_config_issues(
                wait["override"]["wait"],
                "steps.wait_for_run.override.wait",
                timeout_max_ms=WAIT_DURATION_MS_MAX,
            ),
        )
        event, error = resolve_wait_event(wait, {"workflow_data": {"scraper_run_id": "run_01A"}})
        print(f"== resolve_wait_event {label}: {event} {error or ''}")
        if error or event["correlation_key"] != "run_01A":
            problems.append(f"{label}: key not resolved: {error}")

    sys.path[:0] = [str(EXT), str(EXT / "tests")]
    from conftest import ROOT_SCOPE, make_payload  # noqa: E402
    from fake_service import FakeService  # noqa: E402

    from handlers.router import handle_request  # noqa: E402

    os.environ["FS_ALLOW_PRIVATE_REMOTE_URLS"] = "1"
    fake = FakeService().start()
    try:
        scope = {**ROOT_SCOPE, "step_id": "claim_event"}
        raw = handle_request(
            make_payload(
                "claim_event",
                {"event_id": "evt_01CHECK0000000000000000001"},
                service_url=fake.url,
                scope=scope,
            )
        )
        raw_mapping = handle_request(
            make_payload(
                "get_run_summary",
                {"run_id": "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"},
                service_url=fake.url,
            )
        )
    finally:
        fake.stop()
    envelope = normalize_extension_response(raw).model_dump()
    items = envelope.get("resume_waits") or []
    print("== claim_event resume_waits after normalize_extension_response:", items)
    if len(items) != 1 or "_resume_waits" in json.dumps(raw["result"]):
        problems.append(f"resume_waits not carried: {items}")
    report(
        "checked_wait_event on the item",
        [
            rejection
            for _d, rejection in [checked_wait_event(0, item) for item in items]
            if rejection
        ],
    )
    stored_calls = []

    class _Inbox:
        def __init__(self, _db):
            pass

        def deliver(self, **kwargs):
            stored_calls.append(kwargs)
            return [True for _ in kwargs["items"]]

    invocation = {"status": "succeeded", "response": raw}
    outcome = deliver_wait_events(
        invocation,
        db=None,
        extension_id="flowsteward.web-scraper",
        account_id="acc",
        project_id="prj",
        repository_factory=_Inbox,
    )
    print(
        "== deliver_wait_events:",
        outcome,
        "source",
        stored_calls[0]["source"] if stored_calls else None,
    )
    if not outcome or outcome["stored"] != 1 or outcome["rejected"]:
        problems.append(f"resume_waits not stored: {outcome}")
    none = deliver_wait_events(
        {"status": "succeeded", "response": raw_mapping},
        db=None,
        extension_id="flowsteward.web-scraper",
        account_id="acc",
        project_id="prj",
        repository_factory=_Inbox,
    )
    print("== deliver_wait_events on get_run_summary (no resume_waits):", none)
    if none is not None:
        problems.append("an action without resume_waits delivered something")

    # The node accepts exactly this event for the pending wait the step records.
    class _Active:
        step_id = "wait_for_run"
        step_execution_id = "exec-1"
        attempt = 1
        group_id = None
        for_each_id = None

        def message_identity(self):
            return {}

    wait = next(
        st
        for st in variants["scrape_a_site (published)"]["steps"]
        if st["step_id"] == "wait_for_run"
    )
    event, _ = resolve_wait_event(
        wait, {"workflow_data": {"scraper_run_id": items[0]["correlation_key"]}}
    )
    try:
        pending = event_pending_wait(_Active(), event=event, revision=1)
    except Exception as exc:  # noqa: BLE001 - _base_wait may need more of ActiveStep
        pending = {**event, "wait_id": "w1", "due_epoch_ms": 0}
        print("    event_pending_wait needs a real ActiveStep:", type(exc).__name__)
    wake = {"reason": "event", "wait_id": pending["wait_id"], **items[0]}
    verdict = event_wake_verdict(pending, wake)
    other = event_wake_verdict(pending, {**wake, "correlation_key": "run_other"})
    print(f"== event_wake_verdict: own event {verdict!r}, another run's {other!r}")
    if verdict != "event" or other is not None:
        problems.append(f"wake verdict {verdict} / {other}")
    print("\nRESULT (with event waits):", "OK" if not problems else f"{len(problems)} problem(s)")


# 10. A68 (0.1.24): an own workflow's Invoke of Scrape a site passes Core's Invoke check
# (contract_validation_invoke.validate_invoke_workflow_step: the child must list
# parent_workflow among its trigger channels). Rows come from a resolver, not a database.
try:
    # The facade merges the split modules' globals (validate_contract_items ...).
    from core.application.registry.contract_validation import (
        validate_invoke_workflow_step,
    )
except ImportError:
    print("== invoke check: not importable on this platform")
else:
    child_id = f"extension.{INSTALL}.scrape_a_site"

    def _row(workflow: dict, workflow_id: str) -> dict:
        return {
            **workflow,
            "workflow_id": workflow_id,
            "enabled": True,
            "execution_graph": workflow.get("execution_graph")
            or workflow["settings"].get("execution_graph"),
        }

    def invoke_issues(child: dict) -> list:
        parent = {"workflow_id": "supplier_test", "enabled": True, "settings": {}, "steps": []}
        rows = {"supplier_test": parent, child_id: _row(child, child_id)}

        class _Resolver:
            def get_workflow_row(self, workflow_id):
                return rows.get(workflow_id)

            def get_available_type_map_for_step(self, _workflow_id, _step_id):
                # The parent's own run inputs (what the IOResolver reads from its form).
                return {
                    f"{prefix}inputs.{name}": "string"
                    for name in ("site_url", "what_to_extract")
                    for prefix in ("", "state.")
                }

        step = {
            "step_id": "scrape_supplier",
            "step_kind": "invoke_workflow",
            "override": {
                "invoke_workflow": {
                    "workflow_id": child_id,
                    "input_mapping": {
                        "site_url": "state.inputs.site_url",
                        "extraction_instructions": "state.inputs.what_to_extract",
                    },
                    "output_mapping": {
                        "run_summary": "state.scrape.summary",
                        "skipped": "state.scrape.skipped",
                    },
                    "artifact_mapping": {"result_file": "state.supplier_jsonl"},
                }
            },
        }
        try:
            answer = validate_invoke_workflow_step(
                None,
                workflow_id="supplier_test",
                step=step,
                path_prefix="steps.scrape_supplier",
                resolver=_Resolver(),
            )
        except Exception as exc:  # noqa: BLE001 - a lookup that needs the database
            return [{"code": "check_needs_database", "message": f"{type(exc).__name__}: {exc}"}]
        return answer["errors"]

    published_scrape = yaml.safe_load((EXT / "artifacts/workflows/scrape_a_site.yaml").read_text())
    report(
        "invoke of Scrape a site from an own workflow (published)",
        invoke_issues(published_scrape),
    )
    report(
        "invoke of Scrape a site from an own workflow (imported copy)",
        invoke_issues(imported["scrape_site.bundle.yaml"]),
    )
    # Control: without parent_workflow (0.1.23) Core refuses exactly as on the stand.
    old = copy.deepcopy(published_scrape)
    old["settings"]["callable"]["trigger_channels"].remove("parent_workflow")
    codes = [issue.get("code") for issue in invoke_issues(old)]
    print(f"   control without parent_workflow: {codes}")
    if "invoke.child_not_parent_workflow_triggerable" not in codes:
        problems.append(f"invoke control did not refuse: {codes}")
    print("\nRESULT (with invoke):", "OK" if not problems else f"{len(problems)} problem(s)")

if problems:
    sys.exit(1)
