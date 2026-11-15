"""What the mapping agent receives, built by the platform's own code, offline.

Run in the platform's audit image with `core/` of flow-steward/main at /app and
this repository at /ext (see docs/DEV-LOG.md, 0.1.3):

    PYTHONPATH=/app python /ext/tests/tools/render_agent_prompt.py

It takes four realistic samples (tests/page_fixtures.py), resolves the
`impl_build_mapping` step mappings with the platform's connector resolver,
writes samples.md with this extension's handler code, binds it to the agent
step through the platform's AgentFileRuntime (a fake artifact plane serves the
bytes), lets `prepare_agent_files` build the text that is appended to the task
message, renders the step inputs with `PromptBuilder`, and builds the
structured-output schema with `resolve_step_output_contract` and
`build_phase_result_json_schema` from the skill and agent documents exactly as
an installed release projects them. It exits non-zero when any check fails.
Not covered offline: the provider call itself, the database rows of a live
install, and the security scanner's redactions when it needs the database.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

EXT = Path(__file__).resolve().parents[2]
sys.path[:0] = ["/app", str(EXT), str(EXT / "tests")]

import yaml  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402

INSTALL = "829a829e-1ba9-4538-87b9-caec56d32382"
ACCOUNT, PROJECT, JOB = "acc_1", "prj_1", "job_child_1"
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("  ok    " if condition else "  FAIL  ") + message)
    if not condition:
        failures.append(message)


def load(path: str) -> dict[str, Any]:
    return yaml.safe_load((EXT / path).read_text(encoding="utf-8"))


def main() -> int:  # noqa: PLR0915 - one linear report
    from core.application.orchestration.agent_file_runtime import AgentFileRuntime
    from core.application.orchestration.agents.agent_files import prepare_agent_files
    from core.application.orchestration.agents.prompt_builder import PromptBuilder
    from core.application.orchestration.connector_mapping import _render_input_payload
    from core.application.orchestration.models import build_phase_result_json_schema
    from core.application.registry.agent_manifest import AgentManifest
    from core.application.registry.skill_manifest import SkillManifest
    from core.application.registry.step_output_contract import resolve_step_output_contract
    from page_fixtures import samples

    from handlers.samples_file import html_budget, render_samples

    impl = load("artifacts/workflows/impl_build_mapping.yaml")
    steps = {step["step_id"]: step for step in impl["steps"]}
    rows = samples()
    child_inputs = {
        "site": "books.toscrape.com",
        "sample_ids": [row["sample_id"] for row in rows],
        "samples": rows,
        "extraction_instructions": (
            "Book pages: title, price in GBP, UPC code, availability, description, cover image."
        ),
        "fields": ["title", "price", "currency", "upc", "availability", "description", "image"],
        "required_fields": ["title", "price"],
        "required_fill_threshold": 0.8,
        "extraction_mode": "auto",
        "max_refinements": 2,
    }
    # The state after 'prepare' (first generation).
    state: dict[str, Any] = {
        "inputs": child_inputs,
        "attempt": 0,
        "mappings": None,
        "failures": [],
        "service_failures": None,
    }

    print("1. write_samples: inputs through the platform's connector resolver")
    write_inputs, unresolved = _render_input_payload(
        steps["write_samples"]["input_mapping_jsonb"], state
    )
    check(not unresolved, f"no unresolved input ({unresolved})")
    text, report = render_samples(write_inputs["site"], write_inputs["samples"], write_inputs)
    body = text.encode("utf-8")
    print(
        f"   samples.md: {len(text)} characters, {len(body)} bytes; HTML budget per sample "
        f"{html_budget(len(rows))}; per sample {[(r['html_chars'], r['html_shortened']) for r in report]}"
    )
    state["samples_file"] = "artifact:art_samples01"

    print("2. agent file binding through AgentFileRuntime and prepare_agent_files")

    class Plane:
        def get_artifact(self, artifact_id: str) -> dict[str, Any]:
            return {
                "artifact_id": artifact_id,
                "owner_scope": {"account_id": ACCOUNT, "project_id": PROJECT, "job_id": JOB},
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "filename": "samples.md",
                "content_type": "text/markdown",
                "object_key": f"artifacts/{artifact_id}",
                "persistence_status": "available",
            }

        def read_artifact_bytes(self, artifact_id: str, *, max_size_bytes: int) -> bytes:
            return body

    runtime = AgentFileRuntime(plane=Plane(), account_id=ACCOUNT, project_id=PROJECT, job_id=JOB)
    bindings = steps["write_mapping"]["override"]["agent"]["file_bindings"]
    context = runtime.bind_files(bindings, state)
    scanned = "not run"

    def scan(value: str) -> str:
        nonlocal scanned
        try:
            from core.infrastructure.security.integration import scan_tool_output

            decision = scan_tool_output(
                value, tool_name="read_agent_file", project_id=PROJECT, job_id=JOB
            )
        except Exception as exc:  # the live worker swallows scanner errors the same way
            scanned = f"skipped ({type(exc).__name__})"
            return value
        scanned = "redacted" if decision.redacted_text is not None else "clean"
        return value if decision.redacted_text is None else decision.redacted_text

    instructions, tools, blocks = prepare_agent_files(context, None, "", scan)
    print(
        f"   appended to the task message: {len(instructions)} characters; extra tools "
        f"{[getattr(t, 'name', t) for t in tools]}; media blocks {len(blocks)}; scanner: {scanned}"
    )
    check(not tools, "the file is attached whole (no read_agent_file paging needed)")
    check("... truncated ..." not in instructions, "no truncation marker in the attached text")
    for row in rows:
        check(row["distilled_html"] in instructions, f"{row['sample_id']}: whole distilled HTML")
        check(
            row["url"] in instructions and row["title"] in instructions,
            f"{row['sample_id']}: URL and title",
        )
        check(
            json.dumps(row["template_hint"], ensure_ascii=False) in instructions,
            f"{row['sample_id']}: template hint",
        )
        for value in row["meta"].values():
            check(str(value) in instructions, f"{row['sample_id']}: meta {str(value)[:30]!r}")
        if row["jsonld"]:
            check(
                json.dumps(row["jsonld"], ensure_ascii=False, indent=1) in instructions,
                f"{row['sample_id']}: JSON-LD, all {len(row['jsonld'])} nodes whole",
            )
    check(
        child_inputs["extraction_instructions"] in instructions, "the instructions are in the file"
    )

    print("3. the step inputs section (PromptBuilder) and the task envelope excerpt")
    agent_inputs, _unresolved = _render_input_payload(
        steps["write_mapping"]["input_mapping_jsonb"], state
    )
    section = PromptBuilder._build_materialized_inputs_context(
        {"materialized_inputs": agent_inputs}
    )
    print(f"   '## Step Inputs': {len(section)} characters, keys {sorted(agent_inputs)}")
    check("samples" not in agent_inputs, "the agent step no longer maps samples into the prompt")
    check("... truncated ..." not in section, "the step inputs fit the prompt budget whole")
    envelope = PromptBuilder._bounded_task_envelope_value("inputs", child_inputs)
    print(
        f"   task envelope 'inputs' (platform-owned excerpt of the child's inputs): "
        f"{len(json.dumps(envelope, ensure_ascii=False))} characters"
    )

    print("4. structured-output schema from the projected skill and agent documents")
    skill_doc = load("artifacts/skills/build_extraction_mapping.yaml")
    agent_doc = load("artifacts/agents/scraper_mapping_author.yaml")
    skill_id = f"extension.{INSTALL}.build_extraction_mapping"
    agent_id = f"extension.{INSTALL}.scraper_mapping_author"

    class NoDB:
        """No published blocks, no anchors: what a projected skill resolves through."""

    def schema_for(document: dict[str, Any]) -> dict[str, Any]:
        snapshot = {**copy.deepcopy(document), "skill_id": skill_id}
        skill = SkillManifest.from_db_row(snapshot)
        agent = AgentManifest.from_db_row(
            {
                **copy.deepcopy(agent_doc),
                "agent_id": agent_id,
                "skills": [skill_id],
                "base_skill": skill_id,
            }
        )
        contract = resolve_step_output_contract(
            NoDB(),
            agent_id=agent_id,
            skill_id=skill_id,
            account_id=ACCOUNT,
            skill_manifest=skill,
            agent_manifest=agent,
        )
        return {"contract": contract, "schema": build_phase_result_json_schema(contract.rows)}

    from core.application.orchestration.agents.common_runtime_structured_utils import (
        _prepare_openai_json_schema,
        _structured_output_kwargs,
    )

    def strict_problems(node: Any, path: str = "$") -> list[str]:
        """OpenAI strict json_schema rules for every object of the schema."""
        found: list[str] = []
        if isinstance(node, dict):
            if node.get("type") == "object":
                props = node.get("properties")
                if not isinstance(props, dict):
                    found.append(f"{path}: object without properties")
                elif node.get("additionalProperties") is not False:
                    found.append(f"{path}: additionalProperties is not false")
                elif sorted(node.get("required") or []) != sorted(props):
                    found.append(f"{path}: required differs from properties")
            for key, value in node.items():
                found += strict_problems(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                found += strict_problems(value, f"{path}[{index}]")
        return found

    examples_dir = EXT / "tests" / "contract_examples"
    real = [
        json.loads((examples_dir / name).read_text())
        for name in ("mapping.product.json", "mapping.spare_part.json")
    ]

    def answer(artifacts: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "phase_result_v2",
            "status": "pass",
            "summary": "Mapping written.",
            "artifacts": artifacts,
            "evidence": [],
            "errors": [],
            "files_to_touch": [],
            "next_action": "next_phase",
            "confidence": 0.8,
        }

    print(f"   provider openai: with_structured_output{_structured_output_kwargs('openai')}")

    old_doc = copy.deepcopy(skill_doc)
    old_doc["output_schema"] = {
        "type": "object",
        "properties": {
            "mappings": {"type": "array", "minItems": 1, "items": {"type": "object"}},
            "notes": {"type": "string"},
        },
        "required": ["mappings"],
    }
    old_openai = _prepare_openai_json_schema(schema_for(old_doc)["schema"])
    old_items = old_openai["properties"]["artifacts"]["properties"]["mappings"]["items"]
    print(
        "   control, 0.1.3 (mappings: array of objects) as sent to openai: items =",
        json.dumps(old_items),
    )
    old_validator = Draft202012Validator(old_openai)
    check(
        bool(list(old_validator.iter_errors(answer({"mappings": real, "notes": "x"})))),
        "control: a real mapping does NOT fit the 0.1.3 schema the provider enforces",
    )
    check(
        not list(old_validator.iter_errors(answer({"mappings": [{}], "notes": "x"}))),
        "control: only empty objects fit it (what the stand received)",
    )

    built = schema_for(skill_doc)
    for provider in ("openai", "anthropic", "google"):
        sent = (
            _prepare_openai_json_schema(built["schema"])
            if provider == "openai"
            else built["schema"]
        )
        artifacts = sent["properties"]["artifacts"]
        print(f"   0.1.5 as sent to {provider}: artifacts =", json.dumps(artifacts)[:300])
        check(
            artifacts["properties"]["mappings_json"]["type"] == "string"
            and "mappings_json" in artifacts["required"],
            f"{provider}: artifacts.mappings_json is a required string",
        )
        if provider == "openai":
            check(
                not strict_problems(sent),
                f"openai: strict-mode rules hold ({strict_problems(sent)[:3]})",
            )
        validator = Draft202012Validator(sent)
        good = answer({"mappings_json": json.dumps(real), "notes": "JSON-LD first."})
        errors = [e.message for e in validator.iter_errors(good)]
        check(not errors, f"{provider}: the real mappings as JSON text pass ({errors[:2]})")
        check(
            bool(list(validator.iter_errors(answer({"notes": "x"})))),
            f"{provider}: an answer without mappings_json is refused",
        )
    check(not built["contract"].validate(good["artifacts"]), "the output contract accepts it")

    from handlers.mappings import candidate_mappings, structural_problems

    parsed, problems = candidate_mappings(good["artifacts"]["mappings_json"])
    check(
        parsed == real and not problems and not structural_problems(parsed),
        "test_mapping reads the text back into the two mappings",
    )

    try:
        from langchain_core.messages import HumanMessage
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(model="gpt-5.6-luna", api_key="sk-offline-not-used")
        bound = model.with_structured_output(
            _prepare_openai_json_schema(built["schema"]),
            include_raw=False,
            **_structured_output_kwargs("openai"),
        )
        first = getattr(bound, "first", None)
        kwargs = dict(getattr(first, "kwargs", {}) or {})
        payload = model._get_request_payload([HumanMessage(content="x")], **kwargs)
        response_format = payload.get("response_format") or payload.get("text") or {}
        body = json.dumps(response_format)
        print(f"   openai request body response_format: {body[:240]}")
        check(
            '"strict": true' in body and '"mappings_json"' in body,
            "the request body the platform's openai route sends carries the schema strictly",
        )
    except Exception as exc:  # langchain_openai may be absent from the image
        print(f"   openai request body: not built offline ({type(exc).__name__}: {exc})")

    print("\nRESULT:", "OK" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
