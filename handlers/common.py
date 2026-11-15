"""Envelope helpers, safe coercion and secret scrubbing shared by every handler."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

EXTENSION_ID = "flowsteward.web-scraper"
CONNECTION_TYPE_ID = "scraper_profile"
REDACTED = "[redacted]"


def as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def is_unset(value: Any) -> bool:
    """The contract's "not set in this layer": null, "", [] and {}."""
    return value is None or value == "" or value == [] or value == {}


def ok(result: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "result": result or {},
        "commands": [],
        "events": [],
        "reload": [],
        "errors": [],
    }
    payload.update(extra)
    return payload


def fail(
    code: str,
    message: str,
    *,
    result: dict[str, Any] | None = None,
    retryable: bool | None = None,
    no_external_effect: bool | None = None,
) -> dict[str, Any]:
    code = as_text(code, "internal_error")
    message = as_text(message, code)
    payload: dict[str, Any] = {
        "ok": False,
        "error_code": code,
        "error": message,
        "result": result or {},
        "commands": [],
        "events": [],
        "reload": [],
        "errors": [{"code": code, "message": message}],
    }
    if retryable is not None:
        payload["retryable"] = retryable
    if no_external_effect is not None:
        payload["definitely_no_external_effect"] = no_external_effect
    return payload


class Scrubber:
    """Replaces every known secret value in text and nested data with a marker.

    The service promises never to echo secrets; this is the extension's own
    second line, so a secret can never reach a result, a log line or an error
    even if a message quotes one.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        values = {str(item) for item in secrets if item is not None and len(str(item)) >= 4}
        # Longest first, so a secret that contains another is replaced whole.
        self._secrets = sorted(values, key=len, reverse=True)

    def add(self, *secrets: str) -> None:
        merged = set(self._secrets) | {str(s) for s in secrets if s and len(str(s)) >= 4}
        self._secrets = sorted(merged, key=len, reverse=True)

    def text(self, value: Any) -> str:
        text = "" if value is None else str(value)
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text

    def data(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {str(key): self.data(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.data(item) for item in value]
        return value

    def response(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.data(payload)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def count(number: Any, singular: str, plural: str = "") -> str:
    """`1 page`, `2 pages`, `0 pages`: a count with its noun in the right number."""
    try:
        one = float(number) == 1
    except (TypeError, ValueError):
        one = False
    return f"{number} {singular if one else (plural or singular + 's')}"


def bounded(text: str, limit: int = 600) -> str:
    text = as_text(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def human_bytes(value: Any) -> str:
    """`0 B`, `512 B`, `40.0 KB`, `20.2 MB`, `38.4 GB`: a byte count for people (1 KB = 1024 B)."""
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} TB"  # pragma: no cover - the loop always returns
