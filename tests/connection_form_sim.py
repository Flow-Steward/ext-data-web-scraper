"""The platform's connection-form save, ported line by line for offline tests.

Source (flow-steward/main, abaac4b):
- `assets/frontend/src/pages/extensions/connection-save.ts`
  `configuredConnectionFields` (80-97), `applyConnectionConfigValues` (99-124),
  `collectConnectionSecrets` (126-144), `prepareConnectionSave` (188-253).
- `assets/frontend/src/pages/extensions/connection-form-support.ts`
  `manifestFieldPath` (13-15), `manifestFieldWidget` (17-19),
  `manifestFieldIsSecret` (21-24), `connectionDefaultsForValues` (97-105),
  `manifestFieldVisibleForValues` (118-124), `manifestFieldValue` (126-141).
- `assets/frontend/src/pages/extensions/extension-manifest-utils.ts`
  `stringFrom` (215-217), `valueAtPath` (335-346), `normalizedCompareValue`
  (366-374), `matchesVisibleIf` (384-407).

Only what decides `connection_config` and the typed secrets is ported (no
variants, pre-save actions or required-field checks). `UNDEFINED` stands for
JavaScript's `undefined`, which differs from `None` (`null`) and `""`.
"""

from __future__ import annotations

import copy
import math
from typing import Any

UNDEFINED = object()


def _string_from(value: Any, fallback: str = "") -> str:
    if value is UNDEFINED or value is None:
        value = fallback
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _number_text(value)
    return str(value).strip() if isinstance(value, str) else ""


def _number_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def field_path(field: dict) -> str:
    return _string_from(
        field.get("path") or field.get("name") or field.get("field_id") or field.get("id")
    )


def field_widget(field: dict) -> str:
    return _string_from(field.get("widget") or field.get("type") or "text").lower()


def field_is_secret(field: dict, secret_fields: set[str]) -> bool:
    return (
        field_path(field) in secret_fields
        or field_widget(field) == "secret"
        or field.get("secret") is True
    )


def _normalized(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _number_text(value)
    return _string_from(value).lower()


def _matches_visible_if(row: dict, condition: dict) -> bool:
    path = _string_from(condition.get("path") or condition.get("field") or condition.get("key"))
    if not path:
        return True
    actual = _normalized(row.get(path, UNDEFINED))
    if "eq" in condition:
        return actual == _normalized(condition["eq"])
    if "neq" in condition:
        return actual != _normalized(condition["neq"])
    if isinstance(condition.get("in"), list):
        return actual in [_normalized(item) for item in condition["in"]]
    if isinstance(condition.get("not_in"), list):
        return actual not in [_normalized(item) for item in condition["not_in"]]
    return True


def field_visible(field: dict, values: dict) -> bool:
    conditions = field.get("visible_if") or field.get("visibleIf") or []
    return not conditions or all(_matches_visible_if(values, c) for c in conditions)


def _js_number(raw: Any) -> float:
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if text == "":
        return 0.0
    try:
        return float(text)
    except ValueError:
        return math.nan


def field_value(field: dict, values: dict) -> Any:
    raw = values.get(field_path(field), UNDEFINED)
    if raw is UNDEFINED or raw is None or raw == "":
        return UNDEFINED
    if field_widget(field) in {"integer", "number"}:
        parsed = _js_number(raw)
        if not math.isfinite(parsed):
            return UNDEFINED
        return int(parsed) if parsed.is_integer() else parsed
    return raw


def prepare_connection_save(
    component: dict, values: dict, existing: dict | None = None
) -> tuple[dict, dict]:
    """(connection_config, typed secrets) the browser sends for `values`."""
    data = dict(component.get("data") or {})
    field_values = {**(component.get("defaults") or {}), **values}
    fields = [f for f in component.get("fields") or [] if field_visible(f, field_values)]
    config_fields = [_string_from(p) for p in data.get("config_fields") or [] if _string_from(p)]
    required_secret = set(data.get("required_secret_fields") or [])
    secret_fields = set(data.get("secret_fields") or []) | required_secret
    config = copy.deepcopy((existing or {}).get("connection_config") or {})
    by_path = {field_path(f): f for f in fields}
    if config_fields:
        configured = config_fields
    else:
        configured = [
            path
            for path in (field_path(f) for f in fields)
            if path
            and (path not in by_path or not field_is_secret(by_path[path], secret_fields))
            and path not in {"display_name", "environment"}
        ]
    for path in configured:
        raw = field_values.get(path, UNDEFINED)
        if raw is UNDEFINED and path == "provider":
            raw = data.get("provider", UNDEFINED)
        field = next((f for f in fields if field_path(f) == path), None)
        value = field_value(field, {path: raw}) if field is not None else raw
        if value is not UNDEFINED:
            config[path] = value
        elif field is not None and raw == "" and not field_is_secret(field, secret_fields):
            config.pop(path, None)
    secrets = {}
    for field in fields:
        path = field_path(field)
        if field_is_secret(field, secret_fields):
            text = _string_from(field_values.get(path, UNDEFINED))
            if text:
                secrets[path] = text
    return config, secrets


def edit_values(component: dict, existing_config: dict, connection_id: str) -> dict:
    """The form state when an existing connection is opened for editing.

    Every input is controlled: a field with no stored value holds "" (what the
    stand showed for the hidden integer fields); secrets are never sent back.
    """
    values: dict[str, Any] = {"connection_id": connection_id}
    secret_fields = set((component.get("data") or {}).get("secret_fields") or [])
    for field in component.get("fields") or []:
        path = field_path(field)
        if field_is_secret(field, secret_fields):
            values[path] = ""
        else:
            values[path] = existing_config.get(path, "")
    return values
