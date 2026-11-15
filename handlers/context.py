"""What one action invocation carries: input, connection, platform ids."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from flowsteward_extension_sdk import RuntimeContext

from handlers.common import Scrubber, as_dict, as_text
from handlers.profile import Profile, profile_from_connection


@dataclass
class ActionContext:
    payload: dict[str, Any]
    action_id: str
    input: dict[str, Any]
    target: dict[str, Any]
    runtime: RuntimeContext
    _profile: Profile | None = field(default=None, repr=False)
    #: When this invocation started; slow calls work to a deadline from here (budget.py).
    started: float = field(default_factory=time.monotonic)

    @classmethod
    def from_payload(cls, payload: dict[str, Any], runtime: RuntimeContext) -> ActionContext:
        action = as_dict(payload.get("action"))
        raw_input = as_dict(action.get("input"))
        # Grid row actions and some UI paths send values under `input.payload`.
        nested = as_dict(raw_input.get("payload"))
        values = {**as_dict(payload.get("values")), **nested, **raw_input}
        values.pop("payload", None)
        return cls(
            payload=payload,
            action_id=as_text(action.get("action_id")),
            input=values,
            target=as_dict(action.get("target")),
            runtime=runtime,
        )

    @property
    def profile(self) -> Profile:
        if self._profile is None:
            connection = as_dict(self.target.get("connection"))
            if connection and not connection.get("connection_id"):
                connection["connection_id"] = as_text(
                    self.target.get("connection_id") or self.input.get("connection_ref")
                )
            self._profile = profile_from_connection(connection)
        return self._profile

    @property
    def scrubber(self) -> Scrubber:
        try:
            return self.profile.scrubber
        except Exception:  # an unusable profile has nothing to scrub yet
            connection = as_dict(self.target.get("connection"))
            return Scrubber(as_text(value) for value in as_dict(connection.get("secrets")).values())

    @property
    def connection_id(self) -> str:
        return as_text(
            self.target.get("connection_id")
            or as_dict(self.target.get("connection")).get("connection_id")
            or self.input.get("connection_ref")
        )

    def text(self, name: str, default: str = "") -> str:
        return as_text(self.input.get(name), default)

    def require_text(self, name: str, label: str) -> str:
        from handlers.errors import ScraperError

        value = self.text(name)
        if not value:
            raise ScraperError("invalid_payload", f"{label} is required.", no_external_effect=True)
        return value
