"""Write templates/*.bundle.yaml from templates/*.bundle.json (the source).

The workflow importer's file picker accepts only .yaml/.yml, and the importer
reads the text with yaml.safe_load, so the YAML copy is what an operator
uploads. `--check` fails when a copy is missing or differs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
HEADER = (
    "# Generated from {source} by .github/scripts/bundle_yaml.py; edit the JSON.\n"
    "# Import: Workflows -> Import YAML -> Upload YAML.\n"
)


def render(source: Path) -> str:
    data = json.loads(source.read_text(encoding="utf-8"))
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    return HEADER.format(source=source.name) + body


def main(argv: list[str]) -> int:
    check = "--check" in argv
    failed = False
    for source in sorted((ROOT / "templates").glob("*.bundle.json")):
        target = source.with_name(source.name.replace(".json", ".yaml"))
        text = render(source)
        if check:
            if not target.exists() or target.read_text(encoding="utf-8") != text:
                print(f"{target.relative_to(ROOT)} is stale; run .github/scripts/bundle_yaml.py")
                failed = True
        else:
            target.write_text(text, encoding="utf-8")
            print(f"wrote {target.relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
