"""UI pages: supported components only, every binding declared and handled, texts for people."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"
COMPONENT_TYPES = {
    "section",
    "columns",
    "stack",
    "action_bar",
    "form",
    "fieldset",
    "repeater",
    "key_value",
    "json_editor",
    "secret_field",
    "grid",
    "stats_row",
    "key_value_card",
    "markdown",
    "health_panel",
    "log_list",
    "empty_state",
    "alert",
    "confirm_action",
    "connection_form",
    "state_editor",
    "drawer_detail",
    "status_badge",
    "filter_bar",
    "callback_setup",
    "external_resource_picker",
}  # ALLOWED_COMPONENT_TYPES on flow-steward/main
WIDGETS = {
    "text",
    "textarea",
    "url",
    "email",
    "number",
    "integer",
    "boolean",
    "select",
    "radio_group",
    "checkbox_group",
    "tag_picker",
    "secret",
    "json",
    "key_value",
    "repeater",
}


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _pages():
    manifest = _load(UI / "ui_manifest.yaml")
    for ref in manifest["pages"]:
        yield _load(UI / ref["ref"])


def _components():
    for page in _pages():
        for ref in page["components"]:
            yield page, _load((UI / "pages" / ref["ref"]).resolve())


ACTIONS = {a["action_id"]: a for a in _load(UI / "actions" / "actions.yaml")["actions"]}
QUERIES = {q["query_id"]: q for q in _load(UI / "queries" / "queries.yaml")["queries"]}


def test_pages_and_ids():
    pages = list(_pages())
    assert [p["page_id"] for p in pages] == [
        "profiles",
        "service",
        "guide",
        "runs",
        "run_detail",
        "overview",
        "support",
    ]
    ids = [c["component_id"] for _p, c in _components()]
    assert len(ids) == len(set(ids))
    for page in pages:
        assert page["title"] and page["description"]


def test_supported_components_and_bindings():
    from handlers.pages import PAGE_QUERY_HANDLERS
    from handlers.queries import QUERY_HANDLERS

    for _page, component in _components():
        assert component["type"] in COMPONENT_TYPES
        assert component.get("title")
        for key in ("save_action", "validate_action", "test_action", "load_query"):
            assert key not in component, f"{component['component_id']}: {key} belongs under data"
        if "query_id" in component:
            assert component["query_id"] in QUERIES
            assert component["query_id"] in {**QUERY_HANDLERS, **PAGE_QUERY_HANDLERS}
        data = component.get("data") or {}
        for key in ("test_action", "save_action"):
            if key in data:
                assert data[key] in ACTIONS
        for action in (component.get("ui_schema") or {}).get("actions", {}).values():
            if isinstance(action, dict):
                assert action["action_id"] in ACTIONS and action["label"]
        for row_action in component.get("row_actions") or []:
            assert row_action["action_id"] in ACTIONS
        for field in component.get("fields") or []:
            assert field["widget"] in WIDGETS, field
            assert field["label"]
            if field["widget"] == "select":
                assert field["options"]


def test_only_the_profiles_page_has_a_connection_form():
    """Owner, 0.1.18: profiles are created on Profiles; every other page uses the
    platform's profile selector (page `connection`, ADR 0067)."""
    forms = [
        (page["page_id"], c["component_id"])
        for page, c in _components()
        if c["type"] == "connection_form"
    ]
    assert forms == [("profiles", "scraper_profiles")]
    pages = {page["page_id"]: page for page in _pages()}
    for page_id in ("runs", "run_detail", "service"):
        assert pages[page_id]["connection"] == {"connection_type_ids": ["scraper_profile"]}
        assert 10 <= pages[page_id]["refresh_seconds"] <= 3600
    for page_id in ("profiles", "guide", "overview"):
        assert "connection" not in pages[page_id]


def test_connection_queries_are_declared_and_read_only():
    from handlers.pages import PAGE_QUERY_HANDLERS

    for query_id in PAGE_QUERY_HANDLERS:
        query = QUERIES[query_id]
        assert query["connection"] == {"connection_type_ids": ["scraper_profile"]}
        assert query["handler"] == {"mode": "extension"}
    assert "connection" not in QUERIES["extension_health"]


def test_page_buttons_and_inputs():
    """Every button a page shows and every input a person fills (report table in DEV-LOG)."""
    components = {c["component_id"]: c for _p, c in _components()}
    rows = {a["action_id"]: a.get("visible_if") for a in components["runs_table"]["row_actions"]}
    assert rows == {
        "stop_run": {"path": "can_stop", "eq": True},
        "resume_run": {"path": "can_resume", "eq": True},
        "recollect_run": {"path": "can_recollect", "eq": True},
    }
    assert [(a["action_id"], a["visible_if"]) for a in components["run_events"]["row_actions"]] == [
        ("resend_event", {"path": "can_send_again", "eq": True})
    ]
    assert "row_actions" not in components["run_missed"]
    cleanup = components["storage_cleanup"]
    assert cleanup["type"] == "form"
    assert [a["action_id"] for a in cleanup["ui_schema"]["actions"].values()] == [
        "clean_up_storage",
        "clean_up_storage_now",
    ]
    inputs: dict[str, list[str]] = {}
    for page, c in _components():
        if c["type"] == "connection_form":
            continue
        fields = [*(c.get("fields") or []), *((c.get("ui_schema") or {}).get("fields") or [])]
        inputs.setdefault(page["page_id"], []).extend(f.get("path") for f in fields)
    assert inputs.get("runs") in (None, [])
    assert inputs.get("run_detail") in (None, [])
    assert inputs["service"] == [
        "working_files_older_than_days",
        "result_files_older_than_days",
        "include_unfetched_results",
        "run_records_older_than_days",
        "samples_older_than_hours",
        "compact_database",
        "confirm_removal",
    ]
    technical = {"run_id", "event_id", "claim_id", "stage", "cursor", "note", "reasons"}
    assert not technical & {p for paths in inputs.values() for p in paths}
    # The row values a button sends are the action's inputs.
    for grid, action_ids in (
        ("runs_table", ("stop_run", "resume_run", "recollect_run")),
        ("run_events", ("resend_event",)),
    ):
        for action_id in action_ids:
            required = {p["name"] for p in ACTIONS[action_id]["parameters"] if p.get("required")}
            assert required <= {"run_id", "event_id"}, (grid, action_id)
    for action_id in ("clean_up_storage", "clean_up_storage_now"):
        names = {p["name"] for p in ACTIONS[action_id]["parameters"]} - {"dry_run"}
        assert names <= set(inputs["service"]), action_id


def test_texts_are_written_for_people():
    for _page, component in _components():
        text = " ".join(
            str(component.get(key) or "") for key in ("title", "description", "content")
        ) + " ".join(str(f.get("help") or "") for f in component.get("fields") or [])
        assert "TODO" not in text and "lorem" not in text.lower()
        for field in component.get("fields") or []:
            assert field["label"][0].isupper(), field["label"]
