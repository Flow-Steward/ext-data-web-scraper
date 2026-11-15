"""A small walker for the extension's workflow definitions, for offline tests.

It is not the platform's engine, but where the platform is strict it is strict
too (rules read from `flow-steward/main`, paths below are platform paths):

- Run start keeps only the inputs that were given; an optional input that is
  absent or blank is left out of `state.inputs`, a missing required one refuses
  the run (`registry/workflow_contract_service.py:314-323, 606-611`).
- A connector step's input mapping: a `state.` path that is not found and has
  no `|default:<json>` filter is unresolved, and the step fails before the
  connector is called (`orchestration/connector_mapping.py:110-181`,
  `connector_step_runtime.py:281-300`). `|default:` also replaces a found
  `null` or `""`. `literal:<json>` is a JSON value.
- An Invoke's input mapping: an empty value for an optional child input is
  skipped, for a required one the step fails (`invoke_workflow_step_node.py:
  426-455`). The child sees only what was passed.
- Agent steps read leniently (`graph/nodes_contracts.py:513-519`).
- No step may write a reserved state root (`state_path_guards.py:12-48`).
- A failed step follows its `error` edge only under `error_policy.on_error:
  route_to_step` (`step_hub_results.py:405-452`); otherwise the run fails.

- A tabular_feed step writes its whole result to `result_target` and applies a
  step-level `output_mapping_jsonb` for the keys its result has
  (`tabular_feed_step_runtime._apply_output_mapping`).
- A step result ticked "Return from workflow" (`override.returned_files`) is
  copied into the run's returned files under its key when the step writes it
  (`workflow_returned_files.captured_returned_files`); the walker keeps them in
  `Walk.artifacts`, because `state.artifacts` is reserved.
- At a success stop the declared outputs are read like
  `workflow_contract_service.collect_runtime_outputs`: `state.outputs.<name>`,
  else `state.<name>`; the walker keeps them in `Walk.outputs`.

- A Wait with `override.wait.event` (Flow Steward 0.9.18, ADR 0071,
  `wait_event_runtime.py`): the correlation key is resolved once when the step
  is entered (a `state.` path that must resolve to a non-empty text or integer
  of at most 256 characters); the `wait` callback hands the wakes the control
  process would deliver, in order. A wake for another type or key is ignored
  (the node pauses again); a matching event is the step's result
  (`event_type`, `correlation_key`, `payload`, `received_at`, `source`) and
  `output_mapping` copies it to state; `{"timed_out": true}` fails the step
  with `workflow.wait.event_timed_out` under its error policy. Without a `wait`
  callback the first listed event of this key arrives at once.

It follows `execution_graph` edges, evaluates `if` / `switch` the way
`control_flow_step_helpers.py` does, runs `set_variable`, and hands connector,
agent, invoke, tabular_feed and event-wait steps to callbacks.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

StepRunner = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]

#: `core/application/orchestration/state_path_guards.py` RESERVED_STATE_ROOTS on
#: flow-steward/main (abaac4b).
RESERVED_STATE_ROOTS = frozenset(
    {
        "_artifact_plane",
        "_test_mode_suppressed_outputs",
        "_trusted_runtime_context",
        "_workflow_secret_ref_fingerprints",
        "trusted_runtime_context",
        "external_effect_resolution_ref",
        "account_id",
        "actor_id",
        "artifact_plane",
        "artifacts",
        "agent_messages",
        "messages",
        "hooks",
        "job_id",
        "project_config",
        "project_id",
        "run_id",
        "step_test_run_context",
        "step_test_run_terminal",
        "tenant_id",
        "test_mode",
        "user_id",
        "workflow_id",
        "workflow_run_trace",
        "workflow_test_run_kept_outputs",
        "workflow_container_progress",
        "workspace_id",
    }
)

_MISSING = object()


class StepFailed(Exception):
    """A step's callback reports a failure (the connector answered ok: false)."""


def _parse_literal(raw: str) -> Any:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except ValueError:
        return text


def split_default(selector: str) -> tuple[str, Any, bool]:
    """`connector_mapping._split_default_filter`: (selector, default, has_default)."""
    text = str(selector or "").strip()
    if text.startswith("literal:"):
        try:
            json.loads(text[len("literal:") :])
        except ValueError:
            pass
        else:
            return text, None, False
    if "|" not in text:
        return text, None, False
    path, _, filter_text = text.partition("|")
    filter_text = filter_text.strip()
    if not filter_text.startswith("default"):
        return text, None, False
    _, sep, raw_default = filter_text.partition(":")
    if not sep:
        start, end = filter_text.find("("), filter_text.rfind(")")
        raw_default = filter_text[start + 1 : end] if start >= 0 and end > start else ""
    return path.strip(), _parse_literal(raw_default), True


def lookup(state: dict[str, Any], path: str) -> Any:
    """The value at `state.<path>`, or `_MISSING` when a key on the way is absent."""
    node: Any = state
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def resolve(state: dict[str, Any], selector: Any) -> tuple[bool, Any]:
    """(found, value) of one mapping value, platform semantics."""
    if not isinstance(selector, str):
        return True, selector
    token, default, has_default = split_default(selector)
    if token.startswith("literal:"):
        return True, _parse_literal(token[len("literal:") :])
    if not token.startswith("state."):
        return True, selector
    value = lookup(state, token[len("state.") :])
    if has_default and (value is _MISSING or value is None or value == ""):
        return True, default
    if value is _MISSING:
        return False, None
    return True, value


def get_path(state: dict[str, Any], selector: str) -> Any:
    found, value = resolve(state, selector)
    return value if found else None


def is_reserved(target: str) -> bool:
    root = target[len("state.") :].split(".", 1)[0] if target.startswith("state.") else target
    return root in RESERVED_STATE_ROOTS


def set_path(state: dict[str, Any], target: str, value: Any) -> None:
    assert target.startswith("state."), target
    assert not is_reserved(target), f"writes the reserved state root: {target}"
    parts = target[len("state.") :].split(".")
    node = state
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = copy.deepcopy(value)


def has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def start_inputs(workflow: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
    """Run start: required inputs must be there, absent or blank optional ones are left out."""
    rows = workflow["settings"]["callable"]["expected_inputs"]
    inputs: dict[str, Any] = {}
    for row in rows:
        name = row["name"]
        value = given.get(name)
        missing = value is None or (
            row.get("type") not in {"object", "array"}
            and isinstance(value, str)
            and not value.strip()
        )
        if missing:
            assert not row.get("required"), f"run refused: required input '{name}' missing"
            continue
        inputs[name] = copy.deepcopy(value)
    unknown = set(given) - {row["name"] for row in rows}
    assert not unknown, f"run refused: unknown inputs {sorted(unknown)}"
    return inputs


def condition_true(condition: dict[str, Any], state: dict[str, Any]) -> bool:
    left = get_path(state, str(condition.get("selector") or ""))
    operator = str(condition.get("operator") or "truthy").lower()
    right = condition.get("value")
    if isinstance(right, str) and right.startswith("state."):
        right = get_path(state, right)
    if operator in {"eq", "equals", "=="}:
        return left == right
    if operator in {"ne", "not_equals", "!="}:
        return left != right
    if operator == "exists":
        return left is not None
    if operator == "truthy":
        return bool(left)
    if operator == "in":
        return left in (right or [])
    if operator == "not_in":
        return left not in (right or [])
    raise AssertionError(f"operator {operator} not simulated")


class Walk:
    def __init__(
        self,
        workflow: dict[str, Any],
        *,
        connector: StepRunner,
        agent: StepRunner | None = None,
        invoke: StepRunner | None = None,
        tabular: StepRunner | None = None,
        child: dict[str, Any] | None = None,
        max_steps: int = 200,
        hook_steps: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
        wait: Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.workflow = workflow
        self.steps = {step["step_id"]: step for step in workflow["steps"]}
        graph = workflow.get("execution_graph") or workflow["settings"]["execution_graph"]
        self.entry = graph["entry"]["id"]
        self.edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        for edge in graph["edges"]:
            outcome = edge["outcome"]
            key = (edge["source"]["id"], outcome["name"], outcome.get("case_id", ""))
            assert key not in self.edges, f"two edges for {key}"
            self.edges[key] = edge
        self.connector = connector
        self.agent = agent
        self.invoke = invoke
        self.tabular = tabular
        self.child = child
        self.max_steps = max_steps
        self.trace: list[str] = []
        self.reads_missing: list[str] = []
        self.failures: list[str] = []
        #: Returned files captured so far (key -> value), and the outputs at the end.
        self.artifacts: dict[str, Any] = {}
        #: A project's hook steps, simulated: hook id -> function of the hook output.
        self.hook_steps = hook_steps or {}
        self.hooks_run: list[str] = []
        self.outputs: dict[str, Any] = {}
        #: The event waits entered: {step_id, source, event_types, correlation_key, timeout_ms}.
        self.wait = wait
        self.waits: list[dict[str, Any]] = []

    def _capture_returned_files(self, step: dict[str, Any], state: dict[str, Any]) -> None:
        mapping = step.get("output_mapping_jsonb") or {}
        for entry in (step.get("override") or {}).get("returned_files") or []:
            target = mapping.get(entry["result"])
            assert target, f"{step['step_id']}: returns '{entry['result']}', which it does not save"
            value = get_path(state, target)
            if has_value(value):
                self.artifacts[entry.get("key") or entry["result"]] = copy.deepcopy(value)

    def _collect_outputs(self, state: dict[str, Any]) -> None:
        names = [
            row["name"]
            for row in self.workflow.get("settings", {})
            .get("callable", {})
            .get("expected_outputs", [])
        ]
        written = state.get("outputs") if isinstance(state.get("outputs"), dict) else {}
        for name in names:
            if name in written:
                self.outputs[name] = copy.deepcopy(written[name])
            elif name in state:
                self.outputs[name] = copy.deepcopy(state[name])

    def _empty_hook(self, hook: dict[str, Any], state: dict[str, Any]) -> None:
        """An empty hook (0.9.18 invoke_workflow_hook_runtime): its inputs by presence,
        plus the output-only defaults, published at `state.hooks.<hook_id>.output`;
        a required input that is absent fails the step."""
        inputs: dict[str, Any] = {}
        for name, selector in hook["input_mapping"].items():
            found, value = resolve(state, selector)
            if found:
                inputs[name] = copy.deepcopy(value)
        missing = [n for n in hook["input_schema"].get("required", []) if n not in inputs]
        if missing:
            raise StepFailed(f"workflow.hook.input_invalid: {missing} missing")
        output = dict(inputs)
        for name, spec in (hook.get("additional_outputs") or {}).items():
            if "default" in spec:
                output[name] = copy.deepcopy(spec["default"])
        if self.hook_steps and hook["hook_id"] in self.hook_steps:
            output = self.hook_steps[hook["hook_id"]](copy.deepcopy(output))
        state.setdefault("hooks", {})[hook["hook_id"]] = {"output": output}
        self.hooks_run.append(hook["hook_id"])

    def _event_wait(self, step: dict[str, Any], state: dict[str, Any]) -> None:
        """A Wait for an event: resolve the key, take the first matching wake."""
        config = step["override"]["wait"]
        event = config["event"]
        selector = event["correlation_key"]
        found, key = (
            resolve(state, selector)
            if str(selector).startswith("state.")
            else (
                True,
                selector,
            )
        )
        if isinstance(key, int) and not isinstance(key, bool):
            key = str(key)
        if not found or not isinstance(key, str) or not key.strip() or len(key) > 256:
            raise StepFailed(f"workflow.wait.correlation_key_invalid: {selector} -> {key!r}")
        pending = {
            "step_id": step["step_id"],
            "source": event["source"],
            "event_types": list(event["event_types"]),
            "correlation_key": key,
            "timeout_ms": config["timeout_ms"],
        }
        self.waits.append(pending)
        # Without a `wait` callback the awaited event arrives at once: the first listed
        # type for this key with an empty payload (tests about the steps before the wait).
        wakes = (
            self.wait(step, copy.deepcopy(pending))
            if self.wait is not None
            else [{"event_type": pending["event_types"][0], "correlation_key": key}]
        )
        for wake in wakes:
            if wake.get("timed_out") is True:
                raise StepFailed(
                    f"workflow.wait.event_timed_out: {step['step_id']} waited "
                    f"{config['timeout_ms']} ms for {pending['event_types']} (key {key})"
                )
            if wake.get("event_type") not in pending["event_types"]:
                continue
            if str(wake.get("correlation_key")) != key:
                continue
            payload = wake.get("payload", {})
            assert isinstance(payload, dict), "an event payload is an object"
            result = {
                "event_type": wake["event_type"],
                "correlation_key": key,
                "payload": copy.deepcopy(payload),
                "received_at": wake.get("received_at", "2026-11-14T12:00:00Z"),
                "source": event["source"],
            }
            for name, target in (config.get("output_mapping") or {}).items():
                value: Any = result
                for part in name.split("."):
                    value = value.get(part) if isinstance(value, dict) else None
                if value is not None:
                    set_path(state, target, value)
            return
        raise AssertionError(f"{step['step_id']}: still waiting for {pending}")

    def _connector_inputs(self, step: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, selector in (step.get("input_mapping_jsonb") or {}).items():
            found, value = resolve(state, selector)
            if not found:
                self.reads_missing.append(f"{step['step_id']}.{name} <- {selector}")
                raise StepFailed(
                    f"unresolved_required_input: step '{step['step_id']}' could not resolve "
                    f"{name} from {selector}"
                )
            values[name] = value
        return values

    def _agent_inputs(self, step: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, selector in (step.get("input_mapping_jsonb") or {}).items():
            found, value = resolve(state, selector)
            if found:
                values[name] = value
        return values

    def _invoke_inputs(self, spec: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        rows = (self.child or {}).get("settings", {}).get("callable", {}).get("expected_inputs", [])
        required = {row["name"] for row in rows if row.get("required")}
        values: dict[str, Any] = {}
        for name, selector in (spec.get("input_mapping") or {}).items():
            value = get_path(state, selector)
            if not has_value(value):
                if self.child is not None and name not in required:
                    continue
                raise StepFailed(
                    f"invoke.input_mapping.{name} resolved empty value from '{selector}'"
                )
            values[name] = value
        return values

    def _outputs(self, step: dict[str, Any], result: dict[str, Any], state: dict[str, Any]) -> None:
        # A Test Run suppresses an external effect: the host answers {test_mode_status:
        # suppressed, ...} and leaves the mapped outputs unset instead of failing the step
        # (connector_mapping._apply_output_mapping, safe_suppression).
        suppressed = result.get("test_mode_status") == "suppressed"
        for key, target in (step.get("output_mapping_jsonb") or {}).items():
            if suppressed and key not in result:
                continue
            assert key in result, f"{step['step_id']}: result has no '{key}' ({sorted(result)})"
            set_path(state, target, result[key])

    def _next(self, step_id: str, outcome: str, case_id: str = "") -> str | None:
        edge = self.edges.get((step_id, outcome, case_id))
        if edge is None:
            return None
        return edge["target"]["id"]

    def _run_step(self, step: dict[str, Any], state: dict[str, Any]) -> None:
        kind = step["step_kind"]
        override = step.get("override") or {}
        if kind == "connector":
            result = self.connector(step, self._connector_inputs(step, state))
            self._outputs(step, result, state)
        elif kind == "agent":
            assert self.agent is not None
            result = self.agent(step, self._agent_inputs(step, state))
            self._outputs(step, result, state)
        elif kind == "invoke_workflow" and "hook" in override["invoke_workflow"]:
            self._empty_hook(override["invoke_workflow"]["hook"], state)
        elif kind == "invoke_workflow":
            assert self.invoke is not None
            spec = override["invoke_workflow"]
            result = self.invoke(step, self._invoke_inputs(spec, state))
            for key, target in (spec.get("output_mapping") or {}).items():
                assert key in result, f"{step['step_id']}: action result has no '{key}'"
                set_path(state, target, result[key])
        elif kind == "wait" and "event" in override.get("wait", {}):
            self._event_wait(step, state)
        elif kind == "tabular_feed":
            if self.tabular is None:
                # Tests about other steps: the read gives an empty dataset.
                self.tabular = lambda _step, _inputs: {
                    "dataset_handle": "dataset:x",
                    "row_count": 0,
                }
            spec = override["tabular_feed"]
            handle = get_path(state, spec["input"]["artifact_handle"])
            result = self.tabular(step, {"artifact_handle": handle})
            set_path(state, spec["result_target"], result)
            for key, target in (step.get("output_mapping_jsonb") or {}).items():
                if key in result:
                    set_path(state, target, result[key])
        else:
            raise AssertionError(f"step kind {kind} not simulated")
        self._capture_returned_files(step, state)

    def run(
        self, inputs: dict[str, Any], *, raw_inputs: bool = False
    ) -> tuple[str, dict[str, Any]]:
        """Walk from the entry; `raw_inputs`: a child run gets exactly what was passed."""
        given = copy.deepcopy(inputs) if raw_inputs else start_inputs(self.workflow, inputs)
        state: dict[str, Any] = {"inputs": given}
        current: str | None = self.entry
        for _ in range(self.max_steps):
            assert current is not None
            step = self.steps[current]
            kind = step["step_kind"]
            self.trace.append(current)
            override = step.get("override") or {}
            if kind == "set_variable":
                for target, value in override["set_variable"]["assignments"].items():
                    if isinstance(value, dict) and set(value) == {"source"}:
                        value = get_path(state, value["source"])
                    set_path(state, target, value)
                current = self._next(current, "success")
            elif kind == "if":
                outcome = "true" if condition_true(override["if"]["condition"], state) else "false"
                current = self._next(current, outcome)
                assert current is not None, f"{step['step_id']} has no {outcome} edge"
            elif kind == "switch":
                spec = override["switch"]
                value = get_path(state, spec["selector"])
                case = next((c for c in spec["cases"] if str(c["when"]) == str(value)), None)
                current = (
                    self._next(current, "case", case["case_id"])
                    if case
                    else self._next(current, "default")
                )
            elif kind in {"stop_success", "stop_fail"}:
                if kind == "stop_success":
                    # validation_node: the output contract is checked against state at the end.
                    outputs = self.workflow.get("settings", {}).get("callable", {})
                    missing = [
                        row["name"]
                        for row in outputs.get("expected_outputs", [])
                        if row.get("required") and state.get(row["name"]) is None
                    ]
                    if missing:
                        self.failures.append(f"{current}: required output missing: {missing}")
                        return "failed", state
                    self._collect_outputs(state)
                return kind, state
            else:
                try:
                    self._run_step(step, state)
                except StepFailed as exc:
                    self.failures.append(f"{current}: {exc}")
                    policy = (step.get("error_policy") or {}).get("on_error")
                    target = self._next(current, "error") if policy == "route_to_step" else None
                    if target is None:
                        return "failed", state
                    current = target
                    continue
                current = self._next(current, "success")
            assert current is not None, f"{step['step_id']} has no continuation"
        raise AssertionError("too many steps: " + " > ".join(self.trace[-20:]))
