"""Protection traces in plain words (finding A23).

The service reports what its protection route did for every URL that met a
challenge (`analysis.schema.json#/$defs/protection_trace`: in `POST /v1/analyze`,
`POST /v1/samples` and, first 5 per job, the run summary's `protection.traces`).
The raw data stays in the outputs; this module writes the paragraph an operator
reads: where, which challenge, each step and its outcome, the proxy tier, the
exit country with its timezone and locale, the browser, and the screenshot id.

Nothing here is secret: the trace carries a tier number, never a proxy URL, and
cookie names, never values. The exit IP is left out of the text (it stays in the
raw trace). URLs are written without their scheme, so a text that travels into
a platform error is not mistaken for a link with credentials.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from handlers.common import as_dict, as_list, as_text, bounded, count

#: Paragraphs written in one summary; the rest is counted.
MAX_PARAGRAPHS = 10
#: Paragraphs that go into a step error (the summary output has them all).
MAX_ERROR_PARAGRAPHS = 3
PROTECTION_NOT_PASSED = "protection_not_passed"
SCREENSHOT_KEPT = "kept by the service while samples are kept"
SCREENSHOT_HOW = (
    "To look at a screenshot, request GET /v1/screenshots/<id> from the scraper service "
    "with the profile's service token in the Authorization header; Flow Steward cannot "
    "show it yet."
)

_CHALLENGE = {
    "turnstile": "Turnstile challenge",
    "interstitial": "interstitial challenge",
    "other_captcha": "CAPTCHA",
}
_REASON = {
    "not_requested": "not switched on for this run",
    "not_allowed_by_service": "not allowed by the service",
    "not_configured": "not set up in the profile",
    "limit_reached": "the run's limit was reached",
    "no_sitekey": "no site key found on the page",
}
#: The run form field that switches a step on, named when it was not requested.
_SWITCH = {"click": "challenge_click", "solver": "use_solver"}
_STEP_NAME = {
    "wait": "wait",
    "click": "click",
    "solver": "solver",
    "next_proxy_tier": "next proxy tier",
}
_STILL = {"timed_out", "still_challenged"}


def display_url(url: Any, limit: int = 120) -> str:
    """`www.example.com/category?page=2` — host, path and query, no scheme."""
    text = as_text(url)
    parsed = urlparse(text)
    if not parsed.netloc:
        return bounded(text, limit)
    shown = parsed.netloc.rsplit("@", 1)[-1] + (parsed.path if parsed.path != "/" else "")
    if parsed.query:
        shown += "?" + parsed.query
    return bounded(shown, limit)


def _seconds(value: Any) -> str:
    try:
        number = round(float(value), 1)
    except (TypeError, ValueError):
        return ""
    return f"{number:g} s"


def _reason(step: str, reason: str) -> str:
    phrase = _REASON.get(reason, reason.replace("_", " ") if reason else "skipped")
    if reason == "not_requested" and step in _SWITCH:
        phrase += f" ({_SWITCH[step]})"
    return phrase


def _step_phrase(row: dict[str, Any], clicks: int) -> str:
    step = as_text(row.get("step"))
    outcome = as_text(row.get("outcome"))
    reason = as_text(row.get("reason"))
    took = _seconds(row.get("seconds"))
    code = as_text(row.get("code"))
    if outcome == "skipped":
        name = _STEP_NAME.get(step, step.replace("_", " "))
        phrase = _reason(step, reason)
        return f"{name} {phrase}" if phrase.startswith("not ") else f"{name} skipped: {phrase}"
    if outcome == "hard_block":
        return f"{_STEP_NAME.get(step, step)}: the site then refused the visitor (hard block)"
    if outcome == "error":
        return f"{_STEP_NAME.get(step, step)} failed" + (f" ({code})" if code else "")
    if step == "wait":
        waited = f"waited {took}" if took else "waited"
        if outcome == "passed":
            return f"{waited} — passed"
        if outcome in _STILL:
            return f"{waited} — still challenged"
    if step == "click":
        once = " once" if clicks == 1 else ""
        if outcome == "frame_not_found":
            return "click: the checkbox frame was not found"
        if outcome == "passed":
            return f"clicked the checkbox{once} — passed"
        if outcome in _STILL:
            return f"clicked the checkbox{once} — still challenged"
    if step == "solver":
        if outcome == "passed":
            return "the solver's answer passed"
        if outcome in _STILL:
            return "the solver's answer did not pass"
    if step == "next_proxy_tier" and outcome == "moved":
        to_tier = row.get("to_tier")
        return f"moved to proxy tier {to_tier}" if to_tier else "moved to the next proxy tier"
    return f"{_STEP_NAME.get(step, step.replace('_', ' '))}: {outcome.replace('_', ' ')}"


_UA_BROWSERS = (
    ("Edge", re.compile(r"Edg(?:e|A|iOS)?/(\d+)")),
    ("Firefox", re.compile(r"Firefox/(\d+)")),
    ("Chrome", re.compile(r"Chrome/(\d+)")),
    ("Safari", re.compile(r"Version/(\d+).*Safari/")),
)
_UA_SYSTEMS = (
    ("Android", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iOS"),
    ("Windows", "Windows"),
    ("Mac OS X", "macOS"),
    ("Macintosh", "macOS"),
    ("Linux", "Linux"),
)
_PLATFORM_SYSTEMS = (("Win", "Windows"), ("Mac", "macOS"), ("Linux", "Linux"))


def browser_text(trace: dict[str, Any]) -> str:
    """`Firefox 158 on Windows` from the page's own user agent, else the service's browser name."""
    view = as_dict(as_dict(trace.get("environment")).get("browser_view"))
    agent = as_text(view.get("user_agent"))
    name = ""
    for label, pattern in _UA_BROWSERS:
        found = pattern.search(agent)
        if found:
            name = f"{label} {found.group(1)}"
            break
    system = next((label for token, label in _UA_SYSTEMS if token in agent), "")
    if not system:
        platform = as_text(view.get("platform"))
        system = next((label for token, label in _PLATFORM_SYSTEMS if token in platform), "")
    if not name:
        engine = as_text(trace.get("browser"))
        name = {"camoufox": "Camoufox (Firefox)", "chromium": "Chromium", "http": "plain HTTP"}.get(
            engine, engine
        )
    if name and system:
        return f"{name} on {system}"
    return name or system


def _exit_text(trace: dict[str, Any]) -> str:
    environment = as_dict(trace.get("environment"))
    place = as_dict(environment.get("exit"))
    view = as_dict(environment.get("browser_view"))
    country = as_text(place.get("country"))
    details = [as_text(place.get(key)) for key in ("timezone", "locale") if place.get(key)]
    if not country and not details:
        return ""
    text = f"exit {country or 'country unknown'}"
    if details:
        text += f" ({', '.join(details)})"
    shown_zone = as_text(view.get("timezone"))
    if shown_zone and place.get("timezone") and shown_zone != as_text(place.get("timezone")):
        text += f", but the browser reported {shown_zone}"
    return text


_READBACK = {"exact": "exact", "noise": "noise", "unavailable": "unreadable"}


def _readback_text(environment: dict[str, Any]) -> str:
    """`canvas exact` / `canvas noise` (and WebGL when it differs): whether a page could
    read the canvas back as drawn or got random data (service A32)."""
    canvas = as_text(environment.get("canvas_readback"))
    webgl = as_text(environment.get("webgl_readback"))
    parts = []
    if canvas:
        parts.append(f"canvas {_READBACK.get(canvas, canvas)}")
    if webgl and webgl != canvas:
        parts.append(f"WebGL {_READBACK.get(webgl, webgl)}")
    return ", ".join(parts)


def _challenge_text(trace: dict[str, Any]) -> str:
    challenge = as_text(trace.get("challenge"))
    label = _CHALLENGE.get(challenge, challenge.replace("_", " ") or "protection check")
    cookies = [as_text(name).lower() for name in as_list(trace.get("cookies"))]
    if any(name.startswith(("cf_", "__cf")) for name in cookies):
        label = f"Cloudflare {label}"
    return label


def trace_passed(trace: dict[str, Any]) -> bool:
    return bool(as_text(trace.get("passed_at_step")))


def describe_trace(trace: Any) -> str:
    """One paragraph for one challenged URL."""
    row = as_dict(trace)
    head = f"{display_url(row.get('url')) or 'A page'}: {_challenge_text(row)}"
    if row.get("http_status"):
        head += f" (HTTP {row.get('http_status')})"
    steps = [as_dict(step) for step in as_list(row.get("steps"))]
    clicks = sum(1 for step in steps if as_text(step.get("step")) == "click")
    phrases = [_step_phrase(step, clicks) for step in steps]
    sentence = "; ".join(phrases) if phrases else "no step of the protection route ran"
    sentence = sentence[:1].upper() + sentence[1:]
    passed_at = as_text(row.get("passed_at_step"))
    verdict = f"Passed ({_STEP_NAME.get(passed_at, passed_at)})." if passed_at else "Not passed."
    where = []
    tier = row.get("proxy_tier")
    where.append(f"Proxy tier {tier}" if tier else "No proxy")
    exit_text = _exit_text(row)
    if exit_text:
        where.append(exit_text)
    browser = browser_text(row)
    if browser:
        where.append(f"browser {browser}")
    preset = as_text(as_dict(row.get("environment")).get("preset_id"))
    if preset:
        where.append(f"fingerprint {preset}")
    readback = _readback_text(as_dict(row.get("environment")))
    if readback:
        where.append(readback)
    parts = [f"{head}."]
    earlier = as_dict(row.get("passed_earlier"))
    if earlier:
        step = as_text(earlier.get("step"))
        page = display_url(earlier.get("url"))
        parts.append(
            f"The host had passed earlier ({_STEP_NAME.get(step, step) or 'a step'}"
            + (f", on {page}" if page else "")
            + ") and was challenged again."
        )
    parts += [f"{sentence}.", verdict, ", ".join(where) + "."]
    title = as_text(row.get("final_title"))
    if title and not passed_at:
        parts.append(f'Page title "{bounded(title, 80)}".')
    shot = as_text(row.get("screenshot_id"))
    if shot:
        parts.append(f"Screenshot: {shot} ({SCREENSHOT_KEPT}).")
    return " ".join(parts)


def stopped_text(stopped: Any) -> str:
    info = as_dict(stopped)
    if as_text(info.get("reason")) != PROTECTION_NOT_PASSED:
        return ""
    host = as_text(info.get("host")) or display_url(info.get("url")) or "the site"
    skipped = info.get("skipped_urls")
    text = f"The service stopped early: the protection of {host} was not passed"
    if isinstance(skipped, int) and skipped > 0:
        text += f", so {count(skipped, 'more URL')} {'was' if skipped == 1 else 'were'} not tried"
    return text + "."


def protection_warnings(warnings: Any) -> list[dict[str, Any]]:
    return [
        as_dict(row)
        for row in as_list(warnings)
        if as_text(as_dict(row).get("code")) == PROTECTION_NOT_PASSED
    ]


def not_passed(
    traces: Any = None,
    *,
    warnings: Any = None,
    stopped: Any = None,
    protection: Any = None,
) -> bool:
    """True when the service says, or the traces show, that a challenge was not passed."""
    if protection_warnings(warnings) or stopped_text(stopped):
        return True
    rows = [as_dict(row) for row in as_list(traces)]
    if any(not trace_passed(row) for row in rows):
        return True
    overall = as_dict(protection)
    return (
        not rows
        and as_text(overall.get("kind")) in {"challenge", "hard_block"}
        and not as_text(overall.get("passed_at_step"))
    )


def _overall_paragraph(protection: Any, site: str) -> str:
    """For a service without traces: the analysis' one-line protection verdict."""
    overall = as_dict(protection)
    kind = as_text(overall.get("kind"))
    if kind in {"", "none"}:
        return ""
    label = {
        "challenge": _CHALLENGE.get(as_text(overall.get("challenge")), "challenge"),
        "hard_block": "hard block (the site refuses the visitor)",
        "rate_limited": "rate limit",
    }.get(kind, kind)
    passed_at = as_text(overall.get("passed_at_step"))
    verdict = f"passed ({_STEP_NAME.get(passed_at, passed_at)})" if passed_at else "not passed"
    return f"{site or 'The site'}: {label}, {verdict}."


def paragraphs(
    traces: Any = None,
    *,
    warnings: Any = None,
    stopped: Any = None,
    protection: Any = None,
    site: str = "",
    limit: int = MAX_PARAGRAPHS,
) -> list[str]:
    """The summary as a list of paragraphs; empty when nothing was challenged."""
    rows = [as_dict(row) for row in as_list(traces)]
    out = [describe_trace(row) for row in rows[:limit]]
    if len(rows) > limit:
        more = len(rows) - limit
        out.append(f"…and {count(more, 'more challenged page')}.")
    if not rows:
        overall = _overall_paragraph(protection, site)
        if overall:
            out.append(overall)
    stop = stopped_text(stopped)
    if stop:
        out.append(stop)
    for row in protection_warnings(warnings):
        message = bounded(as_text(row.get("message")), 400)
        if message and message not in out:
            out.append(f"The service says: {message}")
    if any(row.get("screenshot_id") for row in rows[:limit]):
        out.append(SCREENSHOT_HOW)
    return out


def summary(
    traces: Any = None,
    *,
    warnings: Any = None,
    stopped: Any = None,
    protection: Any = None,
    site: str = "",
) -> str:
    return "\n\n".join(
        paragraphs(traces, warnings=warnings, stopped=stopped, protection=protection, site=site)
    )


def advice(traces: Any = None) -> str:
    """What to try, with the switches the traces show were off."""
    rows = [as_dict(row) for row in as_list(traces)]
    skipped = {
        (as_text(as_dict(step).get("step")), as_text(as_dict(step).get("reason")))
        for row in rows
        for step in as_list(row.get("steps"))
        if as_text(as_dict(step).get("outcome")) == "skipped"
    }
    tries = [
        "another proxy tier, best a residential proxy with sticky sessions (the profile's "
        "proxy tiers, and Proxy tiers on the run form)"
    ]
    if ("click", "not_requested") in skipped:
        tries.append("allow the checkbox click (Allow the checkbox click on the run form)")
    if ("solver", "not_configured") in skipped:
        tries.append("a challenge solver in the profile, if the service allows solvers")
    else:
        tries.append(
            "the challenge solver, if the service allows it (Allow the challenge solver on the "
            "run form; the solver is set in the profile)"
        )
    own_os = (
        "the machine's own OS for the browser (leave Browser OS empty or host on the run "
        "form): a Windows or macOS claim on a Linux service is usually refused by protection "
        "checks"
    )
    if any(_claims_foreign_os(row) for row in rows):
        tries.insert(0, own_os)
    else:
        tries.append(own_os)
    tries.append("running again later")
    return "What to try: " + "; ".join(tries) + "."


def _claims_foreign_os(trace: dict[str, Any]) -> bool:
    """The browser claimed Windows or macOS (owner's matrix, row 64: on the Linux service
    every such claim failed Cloudflare and the host OS passed)."""
    environment = as_dict(trace.get("environment"))
    claimed = as_text(environment.get("browser_os")).lower()
    if claimed:
        return claimed in {"windows", "macos"}
    system = browser_text(trace)
    return system.endswith((" on Windows", " on macOS"))


def error_paragraphs(text: str) -> str:
    """The first paragraphs of a summary, for a step error; the rest is counted.

    The screenshot sentence, when present, is kept as the last line.
    """
    parts = [part for part in as_text(text).split("\n\n") if part]
    how = [part for part in parts if part == SCREENSHOT_HOW]
    parts = [part for part in parts if part != SCREENSHOT_HOW]
    shown = parts[:MAX_ERROR_PARAGRAPHS]
    rest = len(parts) - len(shown)
    if rest > 0:
        shown.append(
            f"(+{count(rest, 'more paragraph')} in the output protection_summary of 'Analyze site'.)"
        )
    return "\n".join(shown + how)
