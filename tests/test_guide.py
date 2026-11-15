"""The Guide page: generated from docs/setup-guide.md, renderable, complete."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
GUIDE = (ROOT / "docs" / "setup-guide.md").read_text(encoding="utf-8")
SCRAPE = json.loads((ROOT / "templates" / "scrape_site.bundle.json").read_text())["workflows"][0]
EVENTS = json.loads((ROOT / "templates" / "scraper_events.bundle.json").read_text())["workflows"][0]
MAINT = json.loads((ROOT / "templates" / "service_maintenance.bundle.json").read_text())[
    "workflows"
][0]


def _builder():
    path = ROOT / ".github" / "scripts" / "build_guide_page.py"
    spec = importlib.util.spec_from_file_location("build_guide_page", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_guide_page_is_the_setup_guide():
    builder = _builder()
    assert (ROOT / "ui" / "components" / "guide.yaml").read_text(encoding="utf-8") == (
        builder.render()
    ), "run .github/scripts/build_guide_page.py"
    component = yaml.safe_load((ROOT / "ui" / "components" / "guide.yaml").read_text())
    assert component["type"] == "markdown" and component["content"].startswith("## How it works")


def test_the_guide_uses_only_what_the_page_renders():
    """extension-markdown.tsx (0.9.16): headings, paragraphs, lists, links, bold,
    italic, inline code, code blocks. No tables, no HTML, no rules; https links only."""
    for line in GUIDE.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("|"), line
        assert not stripped.startswith("<") and "<!--" not in stripped, line
        assert stripped not in {"---", "***", "___"}, line
    for target in re.findall(r"\]\(([^)]+)\)", GUIDE):
        assert target.startswith("https://") or target.startswith("docs/"), target


def test_every_run_form_field_is_explained():
    for row in SCRAPE["settings"]["callable"]["expected_inputs"]:
        assert f"(`{row['name']}`" in GUIDE or f"`{row['name']}`)" in GUIDE, row["name"]
        label = row["label"]
        assert f"**{label}**" in GUIDE, label


def test_every_working_step_of_every_workflow_is_explained():
    for workflow in (SCRAPE, EVENTS, MAINT):
        for step in workflow["steps"]:
            if step["step_kind"] in {"connector", "invoke_workflow", "tabular_feed"}:
                assert f"**{step['display_name']}**" in GUIDE, step["display_name"]
            assert step.get("description"), step["step_id"]
            assert len(step["description"]) <= 200, step["step_id"]


def test_the_guide_covers_the_owner_s_checklist():
    for words in (
        "WSS_API_TOKEN",
        "WSS_SECRETS_KEY",
        "Approve use of secrets",
        "X-Scraper-Body-Signature",
        "Require `Idempotency-Key`",
        "Only accept listed inputs",
        "structured output",
        "Start test run",
        "Run workflow",
        "result.jsonl",
        "no limit: the whole site",
        "present **only on product pages**",
        "Take a top-level one",
        "usually refuse a Windows or macOS claim",
        "Set `browser` when",
        "Use it when the stored mapping is wrong",
        "## Troubleshooting",
    ):
        assert words in GUIDE, words
