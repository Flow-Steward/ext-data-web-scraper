"""A fake scraper service built from the contract, for the extension's tests.

It serves the contract's examples, keeps just enough state for idempotent
starts, mapping versions and event claims, and validates every request body it
receives (and every response it sends) against `service_contract/schemas`. Any
request that does not match the contract is answered `422 validation_failed`
and recorded in `schema_violations`, so a test can assert there were none.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "service_contract" / "schemas"
EXAMPLES = Path(__file__).resolve().parent / "contract_examples"
BASE_ID = "https://contracts.flowsteward.org/web-scraper/v1/"

TOKEN = "fake-service-token-for-tests-0001"  # noqa: S105 - a test value


def _load_registry() -> Registry:
    resources = []
    for path in SCHEMAS.glob("*.schema.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        resources.append((document["$id"], Resource.from_contents(document)))
    return Registry().with_resources(resources)


REGISTRY = _load_registry()


def validator(ref: str) -> Draft202012Validator:
    """`ref` is `<file>#<pointer>` relative to the contract's schema base."""
    return Draft202012Validator({"$ref": BASE_ID + ref}, registry=REGISTRY)


def schema_errors(ref: str, instance: Any) -> list[str]:
    return [
        f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in validator(ref).iter_errors(instance)
    ]


def example(name: str) -> Any:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


#: (method, route pattern) -> (request schema ref or None, response schema ref)
ROUTES: list[tuple[str, str, str | None, str | None]] = [
    ("GET", r"^/health$", None, "job_config.schema.json#/$defs/service_health"),
    (
        "POST",
        r"^/v1/config/validate$",
        "job_config.schema.json#/$defs/validate_request",
        "job_config.schema.json#/$defs/validate_response",
    ),
    (
        "POST",
        r"^/v1/analyze$",
        "analysis.schema.json#/$defs/analyze_request",
        "analysis.schema.json",
    ),
    (
        "POST",
        r"^/v1/proxies/test$",
        "connection_profile.schema.json#/$defs/proxy_test_request",
        "connection_profile.schema.json#/$defs/proxy_test_response",
    ),
    (
        "POST",
        r"^/v1/solvers/test$",
        "connection_profile.schema.json#/$defs/solver_test_request",
        "connection_profile.schema.json#/$defs/solver_test_response",
    ),
    ("POST", r"^/v1/samples$", "samples.schema.json#/$defs/samples_request", "samples.schema.json"),
    (
        "POST",
        r"^/v1/mappings/test$",
        "mapping_test.schema.json#/$defs/request",
        "mapping_test.schema.json#/$defs/response",
    ),
    ("GET", r"^/v1/sites$", None, "mapping.schema.json#/$defs/site_list"),
    (
        "GET",
        r"^/v1/sites/(?P<site>[^/]+)/mappings$",
        None,
        "mapping.schema.json#/$defs/site_mappings",
    ),
    (
        "PUT",
        r"^/v1/sites/(?P<site>[^/]+)/mappings$",
        "mapping.schema.json#/$defs/activate_request",
        "mapping.schema.json#/$defs/activate_response",
    ),
    (
        "POST",
        r"^/v1/runs$",
        "run_summary.schema.json#/$defs/run_create_request",
        "run_summary.schema.json",
    ),
    ("GET", r"^/v1/runs$", None, "run_summary.schema.json#/$defs/run_list"),
    ("GET", r"^/v1/runs/(?P<run>run_[A-Za-z0-9]+)$", None, "run_summary.schema.json"),
    (
        "GET",
        r"^/v1/runs/(?P<run>run_[A-Za-z0-9]+)/unextracted$",
        None,
        "run_summary.schema.json#/$defs/unextracted_list",
    ),
    (
        "POST",
        r"^/v1/runs/(?P<run>run_[A-Za-z0-9]+)/stop$",
        "run_summary.schema.json#/$defs/run_stop_request",
        "run_summary.schema.json",
    ),
    (
        "POST",
        r"^/v1/runs/(?P<run>run_[A-Za-z0-9]+)/resume$",
        "run_summary.schema.json#/$defs/run_resume_request",
        "run_summary.schema.json",
    ),
    (
        "GET",
        r"^/v1/runs/(?P<run>run_[A-Za-z0-9]+)/result$",
        None,
        "result_descriptor.schema.json#/$defs/service_result",
    ),
    ("GET", r"^/v1/runs/(?P<run>run_[A-Za-z0-9]+)/result/download$", None, None),
    (
        "GET",
        r"^/v1/mapping-requests/(?P<mrq>mrq_[A-Za-z0-9]+)$",
        None,
        "mapping_request.schema.json",
    ),
    (
        "POST",
        r"^/v1/mapping-requests/(?P<mrq>mrq_[A-Za-z0-9]+)/fail$",
        "mapping_request.schema.json#/$defs/fail_request",
        "mapping_request.schema.json",
    ),
    (
        "POST",
        r"^/v1/events/(?P<evt>evt_[A-Za-z0-9]+)/claim$",
        "event.schema.json#/$defs/claim_request",
        "event.schema.json#/$defs/claim_response",
    ),
    (
        "POST",
        r"^/v1/events/(?P<evt>evt_[A-Za-z0-9]+)/stage$",
        "event.schema.json#/$defs/stage_request",
        "event.schema.json#/$defs/event_record",
    ),
    (
        "POST",
        r"^/v1/events/(?P<evt>evt_[A-Za-z0-9]+)/complete$",
        "event.schema.json#/$defs/complete_request",
        "event.schema.json#/$defs/event_record",
    ),
    (
        "POST",
        r"^/v1/events/(?P<evt>evt_[A-Za-z0-9]+)/resend$",
        "event.schema.json#/$defs/resend_request",
        "event.schema.json#/$defs/event_record",
    ),
    (
        "POST",
        r"^/v1/runs/(?P<run>run_[A-Za-z0-9]+)/recollect$",
        "run_summary.schema.json#/$defs/run_recollect_request",
        "run_summary.schema.json",
    ),
    ("GET", r"^/v1/admin/storage$", None, "storage.schema.json"),
    (
        "POST",
        r"^/v1/admin/cleanup$",
        "cleanup.schema.json#/$defs/cleanup_request",
        "cleanup.schema.json",
    ),
]

#: Parts of POST /v1/admin/cleanup that remove files (README 16.2).
CLEANUP_PARTS = (
    "run_working_files",
    "result_files",
    "run_records",
    "samples",
    "protection_screenshots",
)


def _csv_from(jsonl: bytes) -> bytes:
    """A small CSV of the records as contract 7.5 writes it: BOM, `;`, CRLF, formula guard."""
    rows = [json.loads(line) for line in jsonl.decode("utf-8").splitlines() if line.strip()]
    fields: list[str] = []
    for row in rows:
        for name in row.get("data") or {}:
            if name not in fields:
                fields.append(name)

    def cell(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            value = " | ".join(str(v) for v in value)
        elif isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        text = str(value)
        if isinstance(value, str) and text[:1] in {"=", "+", "-", "@", "\t", "\r"}:
            text = "'" + text
        if any(ch in text for ch in ';"\r\n'):
            text = '"' + text.replace('"', '""') + '"'
        return text

    head = ["record_id", "source_url", *[f"data.{f}" for f in fields]]
    lines = [";".join(head)]
    for row in rows:
        data = row.get("data") or {}
        lines.append(
            ";".join(
                [cell(row.get("record_id")), cell(row.get("source_url"))]
                + [cell(data.get(f)) for f in fields]
            )
        )
    return b"\xef\xbb\xbf" + ("\r\n".join(lines) + "\r\n").encode("utf-8")


def error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": "req_01FAKE0000000000000000000",
    }
    error.update(extra)
    return {"error": error}


class FakeService:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.schema_violations: list[str] = []
        self.response_violations: list[str] = []
        #: (method, path regex) -> list of (status, body or bytes, headers); consumed in order.
        self.overrides: list[tuple[str, str, int, Any, dict[str, str]]] = []
        #: seconds to sleep before answering a route (method, regex)
        self.delays: dict[tuple[str, str], float] = {}
        self.result_bytes: bytes = (EXAMPLES / "result.sample.jsonl").read_bytes()
        self.result_descriptor_patch: dict[str, Any] = {}
        #: The CSV beside the result (contract 7.5); False plays a service older than it.
        self.csv_enabled = True
        self.csv_bytes: bytes = _csv_from(self.result_bytes)
        #: format of every result download answered ("jsonl" or "csv")
        self.downloads: list[str] = []
        self.download_cut_after: int | None = None  # drop the connection once after N bytes
        self.runs_by_key: dict[str, tuple[str, str]] = {}
        self.run_counter = 0
        self.mappings: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.events: dict[str, dict[str, Any]] = {}
        #: event_id -> fields that replace the example event body (type, resource ...)
        self.event_bodies: dict[str, dict[str, Any]] = {}
        self.closed_requests: dict[str, str] = {}
        #: Fields that replace the example run of `GET /v1/runs/{id}` (state, platform ...).
        self.run_patch: dict[str, Any] = {}
        self.limits_patch: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._seed_mappings()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # -- lifecycle ---------------------------------------------------------

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def start(self) -> FakeService:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # -- control -----------------------------------------------------------

    def override(
        self, method: str, pattern: str, status: int, body: Any, headers: dict | None = None
    ) -> None:
        self.overrides.append((method, pattern, status, body, headers or {}))

    def calls(self, method: str, prefix: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == method and r["path"].startswith(prefix)]

    def _seed_mappings(self) -> None:
        answer = example("get_site_mappings.response.json")
        for template in answer["templates"]:
            versions = [v for v in template["versions"]]
            self.mappings[(answer["site"], template["template_id"])] = copy.deepcopy(versions)

    # -- HTTP --------------------------------------------------------------

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:  # silence
                return

            def _dispatch(self, method: str) -> None:
                service.handle(self, method)

            def do_GET(self) -> None:  # noqa: N802
                self._dispatch("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._dispatch("POST")

            def do_PUT(self) -> None:  # noqa: N802
                self._dispatch("PUT")

        return Handler

    def _send(
        self, handler: BaseHTTPRequestHandler, status: int, body: Any, headers: dict | None = None
    ) -> None:
        if isinstance(body, bytes):
            raw = body
            content_type = (headers or {}).get("Content-Type", "application/x-ndjson")
        else:
            raw = json.dumps(body).encode("utf-8")
            content_type = "application/json"
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(raw)))
        handler.send_header("X-Request-Id", "req_01FAKE0000000000000000000")
        for key, value in (headers or {}).items():
            if key != "Content-Type":
                handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(raw)

    def handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        parsed = urlparse(handler.path)
        path = parsed.path
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        body: Any = None
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except ValueError:
                self._send(handler, 400, error_body("invalid_json", "not JSON"))
                return
        record = {
            "method": method,
            "path": path,
            "query": query,
            "headers": {k.lower(): v for k, v in handler.headers.items()},
            "body": body,
        }
        with self._lock:
            self.requests.append(record)
        if handler.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(handler, 401, error_body("unauthorized", "Missing or wrong bearer token"))
            return
        for (m, pattern), seconds in self.delays.items():
            if m == method and re.search(pattern, path):
                threading.Event().wait(seconds)
        for index, (m, pattern, status, override_body, headers) in enumerate(self.overrides):
            if m == method and re.search(pattern, path):
                self.overrides.pop(index)
                self._send(handler, status, override_body, headers)
                return
        for route_method, pattern, request_ref, response_ref in ROUTES:
            match = re.match(pattern, path)
            if route_method != method or not match:
                continue
            if request_ref is not None:
                if (
                    raw
                    and handler.headers.get("Content-Type", "").split(";")[0] != "application/json"
                ):
                    self._send(handler, 415, error_body("unsupported_media_type", "JSON only"))
                    return
                problems = schema_errors(request_ref, body)
                if problems:
                    with self._lock:
                        self.schema_violations.extend(f"{method} {path}: {p}" for p in problems)
                    self._send(
                        handler,
                        422,
                        error_body(
                            "validation_failed",
                            "The body violates the schema",
                            fields=[
                                {
                                    "field": p.split(":")[0].replace("/", "."),
                                    "code": "invalid_value",
                                    "message": p[:400],
                                }
                                for p in problems[:20]
                            ],
                        ),
                    )
                    return
            name = "route_" + re.sub(r"[^a-z]+", "_", f"{method.lower()} {pattern}").strip("_")
            groups = {k: unquote(v) for k, v in match.groupdict().items()}
            status, answer, headers = getattr(self, "_answer")(
                method, pattern, groups, body, query, record, handler
            )
            if status is None:
                return  # already sent (download)
            if response_ref and 200 <= status < 300:
                problems = schema_errors(response_ref, answer)
                if problems:
                    with self._lock:
                        self.response_violations.extend(f"{name}: {p}" for p in problems)
            self._send(handler, status, answer, headers)
            return
        self._send(handler, 404, error_body("not_found", "No such route"))

    # -- behaviour -----------------------------------------------------------

    def _answer(self, method, pattern, groups, body, query, record, handler):  # noqa: C901, PLR0911, PLR0912
        if pattern == r"^/health$":
            health = example("get_health.response.json")
            health["limits"].update(self.limits_patch)
            return 200, health, None
        if pattern == r"^/v1/config/validate$":
            return self._validate(body)
        if pattern == r"^/v1/analyze$":
            return 200, example("post_analyze.response.json"), None
        if pattern == r"^/v1/proxies/test$":
            answer = example("post_proxies_test.response.json")
            answer["tier"] = body["tier"]
            return 200, answer, None
        if pattern == r"^/v1/solvers/test$":
            return 200, example("post_solvers_test.response.json"), None
        if pattern == r"^/v1/samples$":
            return 200, example("post_samples.response.json"), None
        if pattern == r"^/v1/mappings/test$":
            return 200, example("post_mappings_test.response.json"), None
        if pattern == r"^/v1/sites$":
            return 200, example("get_sites.response.json"), None
        if pattern.startswith(r"^/v1/sites/") and method == "GET":
            return 200, self._site_mappings(groups["site"], query), None
        if pattern.startswith(r"^/v1/sites/") and method == "PUT":
            return self._activate(groups["site"], body)
        if pattern == r"^/v1/runs$" and method == "POST":
            return self._start(body, record)
        if pattern == r"^/v1/runs$":
            return 200, example("get_runs.response.json"), None
        if pattern.endswith(r"/unextracted$"):
            return 200, example("get_run_unextracted.response.json"), None
        if pattern.endswith(r"/stop$"):
            run = example("post_run_stop.response.json")
            run["run_id"] = groups["run"]
            return 200, run, None
        if pattern.endswith(r"/resume$"):
            run = example("post_run_resume.response.json")
            run["run_id"] = groups["run"]
            return 200, run, None
        if pattern.endswith(r"/result$"):
            return 200, self._descriptor(groups["run"]), None
        if pattern.endswith(r"/result/download$"):
            fmt = query.get("format", "jsonl")
            if fmt not in {"jsonl", "csv"} or (fmt == "csv" and not self.csv_enabled):
                return 422, error_body("validation_failed", "format"), None
            self._download(handler, groups["run"], fmt)
            return None, None, None
        if pattern.endswith(r"/recollect$"):
            return self._recollect(groups["run"], body or {}, record)
        if pattern.endswith(r"[A-Za-z0-9]+)$") and "runs" in pattern:
            run = example("get_run.response.json")
            run["run_id"] = groups["run"]
            run.update(copy.deepcopy(self.run_patch))
            return 200, run, None
        if "mapping-requests" in pattern and method == "GET":
            answer = example("get_mapping_request.response.json")
            answer["mapping_request_id"] = groups["mrq"]
            return 200, answer, None
        if "mapping-requests" in pattern:
            status = self.closed_requests.get(groups["mrq"])
            if status and status != "failed":
                return (
                    409,
                    error_body("mapping_request_closed", "closed", details={"status": status}),
                    None,
                )
            self.closed_requests[groups["mrq"]] = "failed"
            answer = example("post_mapping_request_fail.response.json")
            answer["mapping_request_id"] = groups["mrq"]
            return 200, answer, None
        if "/events/" in pattern:
            return self._event(pattern.rsplit("/", 1)[-1].rstrip("$"), groups["evt"], body)
        if pattern == r"^/v1/admin/storage$":
            return 200, example("get_admin_storage.response.json"), None
        if pattern == r"^/v1/admin/cleanup$":
            return 200, self._cleanup(body or {}), None
        return 404, error_body("not_found", "no behaviour"), None

    def _cleanup(self, body):
        """The contract's example answer, cut to the parts the request named."""
        dry = body.get("dry_run", True)
        answer = example(
            "post_admin_cleanup.response.dry_run.json"
            if dry
            else "post_admin_cleanup.response.json"
        )
        answer["removed"] = {
            part: entry for part, entry in answer["removed"].items() if part in body
        }
        if not body.get("compact_database"):
            answer["compaction"] = None
        freed = sum(entry["bytes"] for entry in answer["removed"].values())
        if not dry and answer["compaction"]:
            freed += answer["compaction"]["bytes_freed"] or 0
        answer["bytes_freed"] = freed
        if not any(
            (body.get(part) or {}).get("include_unfetched")
            for part in ("result_files", "run_records")
        ):
            return answer
        answer["kept"]["unfetched_results"] = 0
        return answer

    def _validate(self, body):
        limits = example("get_health.response.json")["limits"]
        limits.update(self.limits_patch)
        errors = []
        merged = example("post_config_validate.response.json")["config"]
        for layer in (
            body.get("profile", {}).get("defaults") or {},
            body.get("site_settings") or {},
            body.get("run_inputs") or {},
        ):
            for key, value in layer.items():
                if value in (None, "", [], {}):
                    continue
                merged[key] = value
        if (
            isinstance(merged.get("max_pages"), int)
            and merged["max_pages"] > limits["max_pages_per_job"]
        ):
            errors.append(
                {
                    "field": "max_pages",
                    "code": "limit_exceeded",
                    "message": f"max_pages {merged['max_pages']} is above the service limit {limits['max_pages_per_job']}",
                    "limit": limits["max_pages_per_job"],
                }
            )
        if merged.get("discovery_mode") != "url_list":
            merged["urls"] = []
        if errors:
            return 200, {"valid": False, "config": None, "errors": errors, "warnings": []}, None
        answer = example("post_config_validate.response.json")
        answer["config"] = merged
        return 200, answer, None

    def _site_mappings(self, site, query):
        templates = []
        for (row_site, template_id), versions in sorted(self.mappings.items()):
            if row_site != site:
                continue
            if query.get("template_id") and query["template_id"] != template_id:
                continue
            active = [v for v in versions if v["status"] == "active"]
            shown = active if query.get("versions", "active") == "active" else versions
            templates.append(
                {
                    "template_id": template_id,
                    "active_version": active[0]["version"] if active else None,
                    "versions_total": len(versions),
                    "versions": copy.deepcopy(shown),
                }
            )
        return {"site": site, "templates": templates}

    def _activate(self, site, body):
        template_id = body["template_id"]
        if template_id != body["mapping"]["template_id"]:
            return (
                422,
                error_body(
                    "validation_failed",
                    "mismatch",
                    fields=[
                        {
                            "field": "template_id",
                            "code": "template_id_mismatch",
                            "message": "mismatch",
                        }
                    ],
                ),
                None,
            )
        versions = self.mappings.setdefault((site, template_id), [])
        active = next((v for v in versions if v["status"] == "active"), None)
        sha = hashlib.sha256(canonical(body["mapping"]).encode("utf-8")).hexdigest()
        if active and active["content_sha256"] == sha:
            return (
                200,
                {
                    "site": site,
                    "template_id": template_id,
                    "changed": False,
                    "active": active,
                    "resolved_mapping_request_ids": [],
                },
                None,
            )
        active_version = active["version"] if active else 0
        if active_version != body["expected_version"]:
            return (
                409,
                error_body(
                    "mapping_version_conflict",
                    f"{template_id}: expected version {body['expected_version']}, active version is {active_version}",
                    details={
                        "site": site,
                        "template_id": template_id,
                        "expected_version": body["expected_version"],
                        "active_version": active_version,
                    },
                ),
                None,
            )
        if active:
            active["status"] = "superseded"
            active["superseded_at"] = "2026-11-13T10:00:00Z"
        new = {
            "site": site,
            "template_id": template_id,
            "version": active_version + 1,
            "status": "active",
            "content_sha256": sha,
            "created_at": "2026-11-13T10:00:00Z",
            "superseded_at": None,
            "origin": body.get("origin") or None,
            "mapping": body["mapping"],
        }
        versions.append(new)
        return (
            200,
            {
                "site": site,
                "template_id": template_id,
                "changed": True,
                "active": new,
                "resolved_mapping_request_ids": [],
            },
            None,
        )

    def _start(self, body, record):
        key = record["headers"].get("idempotency-key")
        if not key:
            return 400, error_body("idempotency_key_required", "missing"), None
        if not re.fullmatch(r"[A-Za-z0-9:._-]{1,255}", key):
            return 400, error_body("idempotency_key_invalid", "bad key"), None
        fingerprint = hashlib.sha256(
            canonical({"config": body["config"], "platform": body["platform"]}).encode()
        ).hexdigest()
        with self._lock:
            existing = self.runs_by_key.get(key)
            if existing and existing[1] != fingerprint:
                return (
                    409,
                    error_body(
                        "idempotency_key_reused", "other body", details={"run_id": existing[0]}
                    ),
                    None,
                )
            status = 200 if existing else 201
            if not existing:
                self.run_counter += 1
                existing = (f"run_01FAKE{self.run_counter:020d}", fingerprint)
                self.runs_by_key[key] = existing
        run = example("post_runs.response.json")
        run["run_id"] = existing[0]
        run["idempotency_key"] = key
        run["platform"] = body["platform"]
        return status, run, {"Location": f"/v1/runs/{existing[0]}"}

    def _descriptor(self, run_id):
        answer = example("get_run_result.response.json")
        answer.update(
            {
                "run_id": run_id,
                "size_bytes": len(self.result_bytes),
                "record_count": self.result_bytes.count(b"\n"),
                "sha256": hashlib.sha256(self.result_bytes).hexdigest(),
                "download_path": f"/v1/runs/{run_id}/result/download",
            }
        )
        if self.csv_enabled:
            answer["csv"] = {
                "file_name": f"lumen-shop.example_{run_id}.csv",
                "size_bytes": len(self.csv_bytes),
                "sha256": hashlib.sha256(self.csv_bytes).hexdigest(),
                "download_path": f"/v1/runs/{run_id}/result/download?format=csv",
            }
        else:
            answer.pop("csv", None)
            answer.pop("fetched_at", None)
        answer.update(self.result_descriptor_patch)
        return answer

    def _recollect(self, parent, body, record):
        key = record["headers"].get("idempotency-key")
        if not key:
            return 400, error_body("idempotency_key_required", "missing"), None
        if not re.fullmatch(r"[A-Za-z0-9:._-]{1,255}", key):
            return 400, error_body("idempotency_key_invalid", "bad key"), None
        reasons = body.get("reasons") or [
            "fetch_failed",
            "blocked",
            "incomplete",
            "no_template",
            "template_failed",
            "stopped",
        ]
        fingerprint = hashlib.sha256(
            canonical({"parent": parent, "reasons": reasons}).encode()
        ).hexdigest()
        with self._lock:
            existing = self.runs_by_key.get(key)
            if existing and existing[1] != fingerprint:
                return 409, error_body("idempotency_key_reused", "other body"), None
            status = 200 if existing else 201
            if not existing:
                self.run_counter += 1
                existing = (f"run_01RECO{self.run_counter:020d}", fingerprint)
                self.runs_by_key[key] = existing
        run = example("post_runs.response.json")
        run["run_id"] = existing[0]
        run["idempotency_key"] = key
        run["platform"] = body["platform"]
        by_reason = {"fetch_failed": 10, "blocked": 2, "incomplete": 3}
        by_reason = {reason: number for reason, number in by_reason.items() if reason in reasons}
        run["recollect_of"] = {
            "run_id": parent,
            "reasons": reasons,
            "url_count": max(1, sum(by_reason.values())),
            "by_reason": by_reason,
        }
        return status, run, {"Location": f"/v1/runs/{existing[0]}"}

    def _download(self, handler, run_id, fmt="jsonl"):
        data = self.csv_bytes if fmt == "csv" else self.result_bytes
        self.downloads.append(fmt)
        sha = hashlib.sha256(data).hexdigest()
        start = 0
        status = 200
        range_header = handler.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-", range_header)
            if match:
                start = int(match.group(1))
                status = 206
        body = data[start:]
        headers = {
            "Content-Type": "text/csv; charset=utf-8" if fmt == "csv" else "application/x-ndjson",
            "ETag": f'"{sha}"',
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'attachment; filename="lumen-shop.example_{run_id}.{fmt}"',
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
        handler.send_response(status)
        for key, value in headers.items():
            handler.send_header(key, value)
        cut = self.download_cut_after
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if cut is not None and status == 200:
            self.download_cut_after = None
            handler.wfile.write(body[:cut])
            handler.wfile.flush()
            handler.close_connection = True
            return
        handler.wfile.write(body)

    def _event(self, verb, event_id, body):
        event = self.events.setdefault(
            event_id,
            {
                "state": "delivered",
                "claim_id": None,
                "claimant": None,
                "stages": [],
                "generation": 1,
                "live": True,
            },
        )
        base = example("post_event_claim.response.json")["event"]
        base = {**base, "event_id": event_id, "delivery_generation": event["generation"]}
        base.update(self.event_bodies.get(event_id, {}))
        base = {
            key: value for key, value in base.items() if value is not None or key in {"template_id"}
        }
        if verb == "claim":
            claimant = body["claimant"]
            if event["state"] == "handled":
                return 409, error_body("event_already_handled", "handled"), None
            if event["state"] == "claimed" and event["claimant"]["job_id"] != claimant["job_id"]:
                if event["live"]:
                    return (
                        409,
                        error_body(
                            "event_already_claimed",
                            "claimed",
                            details={
                                "claimant": event["claimant"],
                                "expires_at": "2026-11-13T16:00:00Z",
                            },
                        ),
                        None,
                    )
                return 409, error_body("event_stale", "stale"), None
            if event["state"] != "claimed":
                event["claim_id"] = f"clm_01FAKE{event['generation']:04d}{len(self.requests):016d}"
                event["claimant"] = claimant
                event["state"] = "claimed"
            last = event["stages"][-1] if event["stages"] else None
            return (
                200,
                {
                    "event": base,
                    "state": "claimed",
                    "claim_id": event["claim_id"],
                    "claimed_at": "2026-11-13T10:00:00Z",
                    "expires_at": "2026-11-13T16:00:00Z",
                    "delivery_generation": event["generation"],
                    "resend_mode": event.get("resend_mode"),
                    "progress": {
                        "stages": [
                            {
                                "stage": s,
                                "recorded_at": "2026-11-13T10:00:00Z",
                                "claimant_job_id": event["claimant"]["job_id"],
                            }
                            for s in event["stages"]
                        ],
                        "last_stage": last,
                    },
                },
                None,
            )
        if verb in {"stage", "complete"}:
            if body["claim_id"] != event["claim_id"]:
                return (
                    409,
                    error_body(
                        "claim_superseded",
                        "The claim was revoked by a resend",
                        details={"event_id": event_id, "delivery_generation": event["generation"]},
                    ),
                    None,
                )
            if verb == "stage":
                if body["stage"] == "completed" and "processed" not in event["stages"]:
                    return 409, error_body("stage_out_of_order", "processed first"), None
                if body["stage"] not in event["stages"]:
                    event["stages"].append(body["stage"])
            else:
                event["state"] = "handled"
        if verb == "resend":
            if event["state"] == "claimed" and event["live"] and not body.get("revoke_live_claim"):
                return (
                    409,
                    error_body("event_claim_live", "live", details={"claimant": event["claimant"]}),
                    None,
                )
            if body.get("expected_generation") not in (None, event["generation"]):
                return 409, error_body("event_generation_conflict", "generation"), None
            event["generation"] += 1
            event["claim_id"] = None
            event["state"] = "pending"
            event["resend_mode"] = body["mode"]
            if body["mode"] == "from_start":
                event["stages"] = []
        record = example("post_event_stage.response.json")
        record.update({"event_id": event_id, "state": event["state"]})
        record["delivery"]["generation"] = event["generation"]
        record["delivery"]["resend_mode"] = event.get("resend_mode")
        record["claim"] = (
            {
                "claimant": event["claimant"],
                "claimed_at": "2026-11-13T10:00:00Z",
                "expires_at": "2026-11-13T16:00:00Z",
            }
            if event["state"] == "claimed"
            else None
        )
        last = event["stages"][-1] if event["stages"] else None
        record["progress"] = {
            "stages": [
                {
                    "stage": s,
                    "recorded_at": "2026-11-13T10:00:00Z",
                    "claimant_job_id": (event["claimant"] or {}).get("job_id", "job_x"),
                }
                for s in event["stages"]
            ],
            "last_stage": last,
        }
        record["handled_at"] = "2026-11-13T10:05:00Z" if event["state"] == "handled" else None
        return 200, record, None
