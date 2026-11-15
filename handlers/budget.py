"""The time one action may spend: the platform's operation timeout, not a constant.

The host gives every action of this extension one timeout (the canonical
timeout_policy of extension.yaml): T = FS_EXTENSION_OPERATION_TIMEOUT_SECONDS
clamped to 30..3600, 300 when unset, and kills the process at T + 30 s headroom
(flow-steward/main core/infrastructure/extension_runtime/extension_runtime_invocation.py
_resolved_entrypoint_timeout; the variable is forwarded to the subprocess,
extension_runtime_common._ENV_ALLOWLIST). An action works to a deadline of
T - 20 s after it started (default 280 s, killed at 330 s).

Slow service calls (`POST /v1/analyze`, `POST /v1/samples`) run the site's
protection route in a browser and can take minutes on a protected site. They get
everything left of that deadline except RETURN_RESERVE_SECONDS for the answer to
travel back: about 270 s by default. When less than MIN_CALL_SECONDS is left, or
the call runs out, the step fails with the retryable `service_timeout` naming
FS_EXTENSION_OPERATION_TIMEOUT_SECONDS.
"""

from __future__ import annotations

import os
import time

from handlers.errors import ScraperError

OPERATION_TIMEOUT_ENV = "FS_EXTENSION_OPERATION_TIMEOUT_SECONDS"
OPERATION_TIMEOUT_DEFAULT = 300
OPERATION_TIMEOUT_MIN = 30
OPERATION_TIMEOUT_MAX = 3600
BUDGET_MARGIN_SECONDS = 20
RETURN_RESERVE_SECONDS = 10
MIN_CALL_SECONDS = 15


def budget_seconds() -> float:
    """Seconds an action may spend from its start (see the module docstring)."""
    try:
        configured = int(str(os.environ.get(OPERATION_TIMEOUT_ENV) or "").strip())
    except ValueError:
        configured = OPERATION_TIMEOUT_DEFAULT
    timeout = max(OPERATION_TIMEOUT_MIN, min(configured, OPERATION_TIMEOUT_MAX))
    return float(timeout - BUDGET_MARGIN_SECONDS)


GENERIC_CAUSE = (
    "On a protected site the service runs the protection route (wait, click, solver) in a "
    "browser for every page, which can take minutes."
)


def slow_call_error(what: str, seconds: int, cause: str = "", after: str = "") -> ScraperError:
    """`cause` replaces the generic sentence on why it is slow; `after` follows the advice."""
    message = (
        f"The scraper service did not finish {what} within the {seconds} s left of this "
        f"step's time limit. {cause or GENERIC_CAUSE} Run the step again (Retry), or ask the "
        f"operator to raise {OPERATION_TIMEOUT_ENV} (now "
        f"{int(budget_seconds()) + BUDGET_MARGIN_SECONDS} s)."
    )
    if after:
        message += f"\n{after}"
    return ScraperError("service_timeout", message, retryable=True)


def slow_call_timeout(started: float, what: str, cause: str = "", after: str = "") -> int:
    """Seconds a slow call may wait, from the action's own deadline."""
    left = started + budget_seconds() - time.monotonic() - RETURN_RESERVE_SECONDS
    if left < MIN_CALL_SECONDS:
        raise slow_call_error(what, max(int(left), 0), cause, after)
    return int(left)
