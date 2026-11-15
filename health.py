#!/usr/bin/env python3
"""Health command: the runtime starts and its handlers import."""

from __future__ import annotations

import json


def check_health() -> dict:
    try:
        from handlers.router import ACTION_HANDLERS
    except Exception as exc:  # noqa: BLE001 - health reports, never raises
        return {"ok": False, "status": "unhealthy", "error": type(exc).__name__}
    return {"ok": True, "status": "healthy", "actions": len(ACTION_HANDLERS)}


if __name__ == "__main__":
    print(json.dumps(check_health(), separators=(",", ":")))
