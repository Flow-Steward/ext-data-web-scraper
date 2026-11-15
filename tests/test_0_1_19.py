"""0.1.19: every action and query asks only for scopes the extension itself requests.

The platform refuses an operation whose `required_scopes` are not in the extension's own
`required_scopes` ("requires scopes not granted to the extension"); 0.1.18's page queries
asked for `read:projects` and every page query was refused on the stand.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _granted() -> set[str]:
    manifest = yaml.safe_load((ROOT / "extension.yaml").read_text("utf-8"))
    return set(manifest["required_scopes"])


def test_every_query_asks_only_for_granted_scopes():
    queries = yaml.safe_load((ROOT / "ui" / "queries" / "queries.yaml").read_text("utf-8"))
    granted = _granted()
    for query in queries["queries"]:
        missing = set(query.get("required_scopes") or []) - granted
        assert not missing, (query["query_id"], missing)


def test_every_action_asks_only_for_granted_scopes():
    actions = yaml.safe_load((ROOT / "ui" / "actions" / "actions.yaml").read_text("utf-8"))
    rows = actions["actions"] if isinstance(actions, dict) else actions
    granted = _granted()
    for action in rows:
        missing = set(action.get("required_scopes") or []) - granted
        assert not missing, (action.get("action_id"), missing)
