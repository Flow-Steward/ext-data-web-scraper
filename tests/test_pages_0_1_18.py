"""0.1.18: Runs, Run detail and Service read the service through the page's profile
(ADR 0067). The envelope is the host's (`extension_runtime_entrypoints.py`, 0.9.17):
`mode: query`, `query: {query_id, page_id, component_id, context, params}` and the
resolved connection at the root `target.connection`."""

from __future__ import annotations

import copy
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import ROOT_SCOPE, SECRETS, assert_no_secrets, profile_config
from fake_service import TOKEN, example

RUN = "run_01JCXA7Q3M5VZ8K2N4P6R8T0W1"
LINK = re.compile(r"\]\((/extensions/[^)]+)\)")


@pytest.fixture
def query(service):
    from handlers.router import handle_request

    def _query(query_id: str, params: dict | None = None, *, connection: bool = True) -> dict:
        target = {}
        if connection:
            target = {
                "connection": {
                    "connection_id": "conn_scraper_1",
                    "connection_type": "scraper_profile",
                    "config": profile_config(service.url),
                    "secrets": dict(SECRETS),
                }
            }
        payload = {
            "contract_version": "extension_host_v2",
            "mode": "query",
            "runtime_context": {
                **ROOT_SCOPE,
                "job_id": None,
                "step_id": None,
                "step_execution_id": None,
            },
            "query": {
                "query_id": query_id,
                "page_id": "x",
                "component_id": "y",
                "context": {"project_id": "prj_1"},
                "params": dict(params or {}),
            },
            "target": target,
        }
        response = handle_request(payload)
        assert_no_secrets(response)
        # A connection query only reads (ADR 0067: query_connection_effects_forbidden).
        assert not response.get("commands") and not response.get("events")
        return response

    return _query


def _links(content: str) -> list[dict]:
    out = []
    for href in LINK.findall(content):
        parsed = urlparse(href)
        assert parsed.path == "/extensions/flowsteward.web-scraper"
        out.append({k: v[0] for k, v in parse_qs(parsed.query).items()})
    return out


# ----------------------------------------------------------------------- Runs


def test_runs_table_rows_and_buttons(query, service):
    response = query("runs_table")
    assert response["ok"], response
    rows = {row["run_id"]: row for row in response["result"]["items"]}
    running = rows["run_01JCZB2D9F4GH6JK8LM0NP2QR4"]
    done = rows[RUN]
    assert running["state"] == "running" and running["progress"] == "37 pages so far"
    assert running["can_stop"] is True and running["can_resume"] is False
    assert running["can_recollect"] is False and running["why_stopped"] == ""
    assert done["can_stop"] is False and done["can_resume"] is False
    assert done["why_stopped"] == "finished, complete" and done["records"] == 1318
    # A complete run is not read again for missed pages.
    assert not service.calls("GET", f"/v1/runs/{RUN}")
    call = service.calls("GET", "/v1/runs")[0]
    assert call["query"] == {"limit": "100", "connection_id": "conn_scraper_1"}


@pytest.mark.parametrize(
    ("state", "outcome", "unextracted", "stop", "resume", "recollect"),
    [
        ("queued", None, 0, True, False, False),
        ("waiting_for_credentials", None, 0, False, True, False),
        ("stopping", None, 0, False, False, False),
        ("completed", "complete", 0, False, False, False),
        ("completed", "partial", 7, False, False, True),
        ("completed", "partial", 0, False, False, False),
        ("failed", None, 3, False, False, True),
    ],
)
def test_a_button_shows_only_where_it_applies(
    query, service, state, outcome, unextracted, stop, resume, recollect
):
    listing = example("get_runs.response.json")
    listing["items"] = [listing["items"][1]]
    listing["items"][0].update({"state": state, "outcome": outcome})
    listing["items"][0]["completeness"] = {
        "target_pages_found": 20,
        "pages_collected": 20 - unextracted,
        "records": 20 - unextracted,
        "not_collected": unextracted,
        "not_collected_by_reason": {"fetch_failed": unextracted} if unextracted else {},
    }
    service.override("GET", r"^/v1/runs$", 200, listing)
    row = query("runs_table")["result"]["items"][0]
    # 0.1.20: one call; the list carries completeness (README 4.9).
    assert not service.calls("GET", f"/v1/runs/{RUN}")
    assert (row["can_stop"], row["can_resume"], row["can_recollect"]) == (stop, resume, recollect)


def test_filters_reach_the_service_and_bad_values_are_dropped(query, service):
    query("runs_table", {"state": "failed", "site": "lumen-shop.example"})
    assert service.calls("GET", "/v1/runs")[0]["query"]["state"] == "failed"
    assert service.calls("GET", "/v1/runs")[0]["query"]["site"] == "lumen-shop.example"
    query("runs_table", {"state": "drop table", "site": "x/../y"})
    assert "state" not in service.calls("GET", "/v1/runs")[1]["query"]
    assert "site" not in service.calls("GET", "/v1/runs")[1]["query"]


def test_no_profile_chosen(query):
    response = query("runs_table", connection=False)
    assert response["ok"] is False and response["error_code"] == "invalid_connection"
    assert "selector at the top of the page" in response["error"]


def test_a_service_outage_is_a_domain_failure_or_a_sentence(query, service):
    service.override(
        "GET",
        r"^/v1/runs$",
        503,
        {"error": {"code": "service_unavailable", "message": "x", "request_id": "r"}},
    )
    grid = query("runs_table")
    assert grid["ok"] is False and grid["error_code"] == "service_unavailable"
    service.override(
        "GET",
        r"^/v1/runs$",
        503,
        {"error": {"code": "service_unavailable", "message": "x", "request_id": "r"}},
    )
    service.override(
        "GET",
        r"^/v1/admin/storage$",
        503,
        {"error": {"code": "service_unavailable", "message": "x", "request_id": "r"}},
    )
    text = query("storage_report")
    assert text["ok"] is True and text["result"]["content"].startswith("This could not be loaded.")


# ----------------------------------------------------------------- Run detail


def test_run_detail_without_a_run_id_points_to_runs(query):
    content = query("run_report")["result"]["content"]
    assert content.startswith("Open a run from the [Runs](")
    assert _links(content)[0]["extension_page"] == "runs"
    assert query("run_events")["result"]["items"] == []
    assert query("run_missed")["result"]["items"] == []


def test_run_detail_refuses_a_forged_run_id(query, service):
    content = query("run_report", {"run_id": "../admin"})["result"]["content"]
    assert content.startswith("Open a run from the")
    assert not service.calls("GET", "/v1/runs/")


def test_run_detail_report_sections(query, service):
    content = query("run_report", {"run_id": RUN})["result"]["content"]
    headings = [line for line in content.splitlines() if line.startswith("### ")]
    assert headings == [
        "### Status",
        "### Progress",
        "### Why it stopped / what to do",
        "### Results",
        "### Missed pages",
        *(["### Timeline"] if "### Timeline" in content else []),
    ]
    assert "lumen-shop.example** — completed, complete." in content
    assert f"**{RUN}.jsonl** (JSON Lines" in content
    assert "[the Scraper events job](/jobs/job_e2a8d4410b)" in content
    assert "[Back to Runs](" in content
    assert "token" not in content.lower()


def test_run_detail_links_parent_and_child(query, service):
    run = example("get_run.response.json")
    run["recollected_by"] = [
        {
            "run_id": "run_01CHILD0000000000000000001",
            "state": "running",
            "created_at": "2026-11-14T11:00:00Z",
        }
    ]
    run["recollect_of"] = {
        "run_id": "run_01PARENT000000000000000001",
        "reasons": ["blocked"],
        "url_count": 2,
        "by_reason": {"blocked": 2},
    }
    run["platform"]["job_id"] = "page-0123456789abcdef01234567"
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    content = query("run_report", {"run_id": RUN})["result"]["content"]
    targets = {link.get("p.run_id") for link in _links(content)}
    assert {"run_01CHILD0000000000000000001", "run_01PARENT000000000000000001"} <= targets
    assert "Started from the Runs page (re-collect)." in content
    assert "/jobs/page-" not in content


def test_run_detail_csv_file_is_named(query, service):
    run = example("get_run.response.json")
    run["result"]["csv"] = example("get_run_result.response.json")["csv"]
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    content = query("run_report", {"run_id": RUN})["result"]["content"]
    assert f"**{RUN}.csv** (CSV for Excel" in content


def test_a_removed_run_says_so(query, service):
    service.override(
        "GET",
        rf"^/v1/runs/{RUN}$",
        404,
        {"error": {"code": "run_not_found", "message": "gone", "request_id": "r"}},
    )
    content = query("run_report", {"run_id": RUN})["result"]["content"]
    assert content.startswith("The service no longer has this run")


def test_events_read_only_and_send_again_where_stuck(query, service):
    run = example("get_run.response.json")
    events = run["events"]
    stale = copy.deepcopy(events[1])
    stale["event_id"] = "evt_01STALE000000000000000001"
    stale["state"] = "stale"
    run["events"] = [events[0], events[1], stale]
    service.override("GET", rf"^/v1/runs/{RUN}$", 200, run)
    rows = query("run_events", {"run_id": RUN})["result"]["items"]
    handled, claimed, failed = rows
    assert handled["state"] == "handled" and handled["can_send_again"] is False
    assert handled["event"] == "mapping repair"
    # Claimed long ago (2026-11-12): offered again, taking over the claim.
    assert claimed["can_send_again"] is True and claimed["revoke_live_claim"] is True
    assert "if that job failed, send it again" in claimed["state"]
    assert failed["state"].startswith("failed") and failed["can_send_again"] is True
    assert failed["revoke_live_claim"] is False and failed["resend_mode"] == "resume"
    assert failed["event_id"] == "evt_01STALE000000000000000001"


def test_send_again_from_a_row_resends(invoke, service):
    """What the grid sends: the row's values as the action's inputs (no typed ids)."""
    row = {
        "event_id": "evt_01JCXF5T6V7W8X9Y0Z1A2B3C4D",
        "resend_mode": "resume",
        "revoke_live_claim": True,
        "expected_generation": 1,
    }
    response = invoke("resend_event", row)
    assert response["ok"], response
    body = service.calls("POST", "/v1/events/evt_01JCXF5T6V7W8X9Y0Z1A2B3C4D/resend")[0]["body"]
    assert body["mode"] == "resume" and body["revoke_live_claim"] is True


def test_missed_pages_table(query, service):
    rows = query("run_missed", {"run_id": RUN})["result"]["items"]
    assert rows and set(rows[0]) == {"url", "reason", "missing", "gaps", "key"}
    assert service.calls("GET", f"/v1/runs/{RUN}/unextracted")[0]["query"] == {"limit": "100"}


def test_row_buttons_work_from_the_runs_page(invoke, service):
    """Stop / Resume / Re-collect get only the row's run_id; the page has no job."""
    page = {**ROOT_SCOPE, "job_id": None, "step_id": None, "step_execution_id": None}
    assert invoke("stop_run", {"run_id": RUN})["ok"]
    assert invoke("resume_run", {"run_id": RUN})["ok"]
    response = invoke("recollect_run", {"run_id": RUN}, scope=page)
    assert response["ok"] and response["result"]["reasons"]


# -------------------------------------------------------------------- Service


def test_storage_report(query, service):
    content = query("storage_report")["result"]["content"]
    assert content.startswith("The service keeps 20.2 MB")
    assert "- **Disk:** 38.4 GB free of 58.4 GB" in content
    assert "- **Last manual clean-up:** Never." in content


def test_storage_report_on_an_older_service(query, service):
    service.override(
        "GET",
        r"^/v1/admin/storage$",
        404,
        {"error": {"code": "not_found", "message": "x", "request_id": "r"}},
    )
    content = query("storage_report")["result"]["content"]
    assert "older than the storage routes" in content


def test_the_token_never_reaches_a_page(query, service):
    for query_id in (
        "runs_table",
        "runs_links",
        "run_report",
        "run_events",
        "run_missed",
        "storage_report",
    ):
        response = query(query_id, {"run_id": RUN})
        assert TOKEN not in json.dumps(response)
