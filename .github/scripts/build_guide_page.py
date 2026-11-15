"""Write ui/components/guide.yaml (the Guide page) from docs/setup-guide.md (the source).

The setup guide is written in the Markdown the platform's extension pages render
(headings, paragraphs, lists, bold, italic, code; no tables, no HTML), so the page
shows it unchanged. `--check` fails when the component is missing or differs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "docs" / "setup-guide.md"
TARGET = ROOT / "ui" / "components" / "guide.yaml"
HEADER = (
    "# Generated from docs/setup-guide.md by .github/scripts/build_guide_page.py; edit the guide.\n"
)


def render() -> str:
    text = SOURCE.read_text(encoding="utf-8")
    # The page has its own title; the guide's first heading and its note on this
    # file are for readers of the repository.
    body = text.split("\n## ", 1)[1]
    component = {
        "component_id": "guide",
        "type": "markdown",
        "title": "Web Scraper guide",
        "content": "## " + body,
    }
    return HEADER + yaml.safe_dump(component, sort_keys=False, allow_unicode=True, width=1000)


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != text:
            print("ui/components/guide.yaml is stale; run .github/scripts/build_guide_page.py")
            return 1
        return 0
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
