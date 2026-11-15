#!/usr/bin/env python3
"""Web Scraper extension entrypoint (subprocess, extension_host_v2).

Reads one JSON object from stdin and writes one canonical JSON object to stdout:
exit 0 with `ok: true`, exit 2 with `ok: false` and a non-empty `error_code`.
"""

from __future__ import annotations

import json
import sys

from handlers.common import fail
from handlers.router import handle_request


def main() -> int:
    raw = sys.stdin.read().strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        print(json.dumps(fail("invalid_payload", "The request is not valid JSON.")))
        return 2
    if not isinstance(payload, dict):
        print(json.dumps(fail("invalid_payload", "The request must be a JSON object.")))
        return 2
    response = handle_request(payload)
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0 if response.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
