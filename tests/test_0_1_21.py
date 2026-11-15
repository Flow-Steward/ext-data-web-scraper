"""0.1.21 (Flow Steward 0.9.18, host 1.8.0): grid links and filters on Runs and Run
detail; connection-slot variants of the three templates (ADR 0068)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from fake_service import example
from test_pages_0_1_18 import query  # noqa: F401 - the page query fixture

ROOT = Path(__file__).resolve().parents[1]
RUN = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"
COMPONENTS = ROOT / "ui" / "components"


def _component(name: str) -> dict:
    return yaml.safe_load((COMPONENTS / f"{name}.yaml").read_text())


def test_runs_rows_open_run_detail():
    grid = _component("runs_table")
    site = next(c for c in grid["columns"] if c["key"] == "site")
    link = {"page": "run_detail", "params": {"run_id": "{row.run_id}"}}
    assert site["type"] == "link" and site["link"] == link
    assert grid["row_link"] == link
    assert "run_id" in grid["row_fields"]
    pages = yaml.safe_load((ROOT / "ui" / "ui_manifest.yaml").read_text())["pages"]
    assert "pages/run_detail.yaml" in [p["ref"] for p in pages]


def test_runs_filters_are_page_parameters():
    filters = {f["param"]: f for f in _component("runs_table")["filters"]}
    assert set(filters) == {"state", "site"}
    states = [o["value"] for o in filters["state"]["options"]]
    schema = json.loads((ROOT / "service_contract/schemas/run_summary.schema.json").read_text())
    assert states == schema["$defs"]["run_state"]["enum"]
    # Site options cannot come from the service: an options query may not have a connection.
    assert filters["site"]["type"] == "text" and "options" not in filters["site"]


def test_the_link_list_is_gone():
    queries = yaml.safe_load((ROOT / "ui/queries/queries.yaml").read_text())["queries"]
    assert "runs_links" not in [q["query_id"] for q in queries]
    assert not (COMPONENTS / "runs_open.yaml").exists()
    page = yaml.safe_load((ROOT / "ui/pages/runs.yaml").read_text())
    assert [c["ref"] for c in page["components"]] == [
        "../components/runs_table.yaml",
        "../components/runs_help.yaml",
    ]


def test_filter_values_reach_the_service(query, service):  # noqa: F811
    query("runs_table", {"state": "completed", "site": "lumen-shop.example"})
    sent = service.calls("GET", "/v1/runs")[0]["query"]
    assert sent["state"] == "completed" and sent["site"] == "lumen-shop.example"


def test_events_link_the_job(query, service):  # noqa: F811
    grid = _component("run_events")
    job = next(c for c in grid["columns"] if c["key"] == "job")
    assert job["type"] == "link" and job["link"] == {"path_field": "job_path"}
    assert "job_path" in grid["row_fields"]
    rows = query("run_events", {"run_id": RUN})["result"]["items"]
    claimed = next(r for r in rows if r["event"] == "run completed")
    assert claimed["job_path"] == "/jobs/job_e2a8d4410b" and claimed["job"] == "Scraper events job"
    run = example("get_run.response.json")
    run["events"][0]["claim"] = None
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    unclaimed = query("run_events", {"run_id": RUN})["result"]["items"][0]
    assert unclaimed["job_path"] == "" and unclaimed["job"] == ""


def test_host_minimum_is_1_8_0():
    for name in ("extension.yaml", "extension-descriptor.yaml"):
        manifest = yaml.safe_load((ROOT / name).read_text())
        assert manifest["runtime"]["compatibility"]["platform_min"] == "1.8.0", name


def test_guide_and_version():
    guide = (ROOT / "docs/setup-guide.md").read_text(encoding="utf-8")
    assert "Click a row (or its site) to open Run detail." in guide
    assert "Flow Steward 0.9.18 or later" in guide
