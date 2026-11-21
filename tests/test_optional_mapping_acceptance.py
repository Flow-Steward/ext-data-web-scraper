"""Offline cross-repository acceptance using the real service mapping engine.

Set SCRAPER_SERVICE_SOURCE to a service checkout. No browser, supplier, proxy or
model is used. The ordinary extension suite skips this optional integration.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_payload, samples_descriptor
from fake_service import FakeService
from test_build_mapping import WORKFLOW
from workflow_sim import StepFailed, Walk

OPTIONAL = ("description", "tracklist", "label", "packaging")
REQUIRED = ("title", "sku", "format")


FIXTURES = Path(__file__).parent / "fixtures/album-editions"


def album_fixture():
    return (FIXTURES / "sample.html").read_text()


def initial_mapping():
    return json.loads((FIXTURES / "initial.mapping.json").read_text())


class FixtureAuthor:
    """Deterministic substitute at the paid author boundary, using real feedback.

    Start with sparse optional coverage. Repair only fields named by the workflow's
    feedback, using selectors from the supplied sample attachment. The terminal
    mode exercises the failure/regression path after a better pass was tested.
    """

    def __init__(self, attachment, terminal="stable"):
        self.attachment = attachment
        self.terminal = terminal
        self.calls = []

    def __call__(self, step, inputs):
        self.calls.append(copy.deepcopy(inputs))
        text = self.attachment().read_text()
        assert 'data-edition="cd"' in text and 'data-edition="dvd"' in text
        if inputs["attempt"] == 0:
            mapping = initial_mapping()
        else:
            assert inputs["previous_mappings"] and inputs["failures"]
            assert "fields_never_filled" in text or "6/7" in text
            mapping = copy.deepcopy(inputs["previous_mappings"][0])
            if inputs["attempt"] >= 2 and self.terminal == "provider_failure":
                raise StepFailed("fixture author unavailable after a tested improvement")
            if inputs["attempt"] >= 2 and self.terminal == "regression":
                mapping["fields"][0]["sources"][0]["selector"] = ".missing-title"
            else:
                warned = {row.get("field") for row in inputs["failures"]}
                for row in inputs["failures"]:
                    if row.get("kind") == "fields_never_filled":
                        warned.update(field for field in OPTIONAL if field in row["message"])
                for field in OPTIONAL:
                    if field in warned and f'class="{field}"' in text:
                        specs = mapping["variants"]["fields"]
                        specs[:] = [spec for spec in specs if spec["name"] != field]
                        specs.append(
                            {
                                "name": field,
                                "selector_template": f'[data-edition="{{id}}"] .{field}',
                                "extract": "text",
                                "fallback_to_page": False,
                            }
                        )
        return {"mappings_json": json.dumps([mapping]), "notes": "offline fixture author"}


@pytest.fixture
def engine_service(tmp_path):
    source = os.environ.get("SCRAPER_SERVICE_SOURCE")
    if not source:
        pytest.skip("set SCRAPER_SERVICE_SOURCE for the offline service integration")
    source_path = Path(source).resolve()
    assert (source_path / "app/setup_ops.py").is_file(), source
    sys.path.insert(0, str(source_path))
    from app.core.db import Database
    from app.extraction.samples_store import SampleStore
    from app.settings import LIMIT_DEFAULTS
    from app.setup_ops import mapping_test

    database = Database(tmp_path / "service.sqlite3")
    engine = SimpleNamespace(
        samples=SampleStore(database, tmp_path),
        settings=SimpleNamespace(limits={key: value[0] for key, value in LIMIT_DEFAULTS.items()}),
    )
    sample = {
        "sample_id": "smp_album",
        "url": "https://catalogue.example.test/album",
        "expires_at": "2099-01-01T00:00:00Z",
        "variant_sources": [],
        "distilled_html": album_fixture(),
        "title": "Imaginary album",
        "jsonld": [],
        "meta": {},
    }
    engine.samples.save(sample, sample["distilled_html"], sample["url"])

    class EngineService(FakeService):
        def _answer(self, method, pattern, groups, body, query, record, handler):
            if pattern == r"^/v1/mappings/test$":
                answer = asyncio.run(mapping_test(engine, body))
                self.reports.append(copy.deepcopy(answer))
                return 200, answer, None
            return super()._answer(method, pattern, groups, body, query, record, handler)

    service = EngineService()
    service.reports = []
    service.start()
    try:
        yield service, sample
    finally:
        service.stop()
        database.close()
        sys.path.remove(str(source_path))
        assert not service.schema_violations, service.schema_violations
        assert not service.response_violations, service.response_violations


def run_fixture(engine_service, tmp_path, terminal="stable", budget=2):
    from handlers.router import handle_request

    service, sample = engine_service
    attachments = []

    def connector(step, inputs):
        operation = step["override"]["connector"]["operation_id"]
        artifacts = None
        if operation == "write_samples_file":
            directory = tmp_path / f"attachment-{len(attachments)}"
            directory.mkdir()
            descriptor = samples_descriptor(directory)
            attachments.append(Path(descriptor["_path"]))
            artifacts = {"inputs": [], "outputs": [descriptor]}
        response = handle_request(
            make_payload(operation, inputs, service_url=service.url, artifacts=artifacts)
        )
        assert response["ok"], response
        return response["result"]

    author = FixtureAuthor(lambda: attachments[-1], terminal)
    walk = Walk(WORKFLOW, connector=connector, agent=author)
    outcome, state = walk.run(
        {
            "site": "catalogue.example.test",
            "sample_ids": [sample["sample_id"]],
            "samples": [sample],
            "fields": [*REQUIRED, *OPTIONAL],
            "required_fields": list(REQUIRED),
            "max_refinements": budget,
        }
    )
    return walk, outcome, state, service.reports


def assert_edition_rows(report):
    rows = [row["data"] for row in report["samples"][0]["variants"]["items"]]
    assert rows == json.loads((FIXTURES / "expected.rows.json").read_text())


@pytest.mark.parametrize("budget", [0, 1, 2])
def test_real_optional_coverage_drives_bounded_refinement(engine_service, tmp_path, budget):
    walk, outcome, state, reports = run_fixture(engine_service, tmp_path, budget=budget)
    assert outcome == "stop_success" and state["passed"] is True
    assert state["attempts"] == budget + 1 and len(reports) == budget + 1
    assert all(report["passed"] for report in reports)
    initial_counts = {
        field["name"]: field["filled_count"] for field in reports[0]["templates"][0]["fields"]
    }
    assert initial_counts == {
        "title": 7,
        "sku": 7,
        "format": 7,
        "description": 0,
        "tracklist": 2,
        "label": 0,
        "packaging": 0,
    }
    if budget:
        assert_edition_rows(reports[-1])
        assert state["mappings"] == [json.loads((FIXTURES / "reviewed.mapping.json").read_text())]
        assert all(
            field["filled_count"] == 6
            for field in reports[-1]["templates"][0]["fields"]
            if field["name"] in OPTIONAL
        )
        assert "6/7" in state["summary"]
    else:
        assert "0/7" in state["summary"]
        assert "2/7" in state["summary"]
    assert state["warnings"] and state["failures"] == []
    assert walk.outputs["mappings"] == state["mappings"]
    assert "Warning:" in walk.outputs["summary"]


@pytest.mark.parametrize("terminal", ["provider_failure", "regression"])
def test_real_refinement_retains_latest_tested_pass(engine_service, tmp_path, terminal):
    walk, outcome, state, reports = run_fixture(engine_service, tmp_path, terminal)
    assert outcome == "stop_success" and state["passed"] is True
    assert_edition_rows(reports[1])
    assert state["mappings"] == [json.loads((FIXTURES / "reviewed.mapping.json").read_text())]
    assert state["failures"] == [] and "6/7" in walk.outputs["summary"]
    assert state["warnings"] and all(row["passed"] for row in state["field_results"])
    assert len(reports) == (2 if terminal == "provider_failure" else 3)
    if terminal == "regression":
        assert reports[-1]["passed"] is False
