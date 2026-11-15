"""HTTP client for the scraper service API v1 (contract `openapi.yaml`).

Every call carries the profile's bearer token, is bounded in time and size,
does not follow redirects (the SDK's pinned opener refuses them) and maps any
non-2xx answer through `errors.from_service_error`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse

from flowsteward_extension_sdk.http import Request, open_pinned_url, resolve_pinned_ips

from handlers.common import as_text
from handlers.errors import ScraperError, from_service_error

JSON_MAX_BYTES = 16 * 1024 * 1024
ERROR_BODY_MAX_BYTES = 256 * 1024
CHUNK_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 300
USER_AGENT = "flowsteward-web-scraper-extension/1.0"
_MEDIA_JSON = "application/json"


def _allow_private() -> bool:
    return str(os.environ.get("FS_ALLOW_PRIVATE_REMOTE_URLS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def normalize_service_url(raw: str) -> str:
    url = as_text(raw).rstrip("/")
    if not url:
        raise ScraperError(
            "invalid_connection",
            "The scraper profile has no service URL. Enter the address of the scraper "
            "service, for example http://web-scraper:8080.",
            no_external_effect=True,
        )
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ScraperError(
            "invalid_connection",
            "The service URL must start with http:// or https://.",
            no_external_effect=True,
        )
    if parsed.username or parsed.password:
        raise ScraperError(
            "invalid_connection",
            "The service URL must not contain a user name or password; the token is "
            "stored separately.",
            no_external_effect=True,
        )
    if not parsed.hostname:
        raise ScraperError("invalid_connection", "The service URL has no host name.")
    if parsed.query or parsed.fragment:
        raise ScraperError(
            "invalid_connection", "The service URL must not contain a query or fragment."
        )
    return url


class ServiceClient:
    def __init__(self, base_url: str, token: str, *, timeout_seconds: int | None = None) -> None:
        self.base_url = normalize_service_url(base_url)
        if not as_text(token):
            raise ScraperError(
                "invalid_connection",
                "The scraper profile has no service token. Enter the token the service "
                "operator configured.",
                no_external_effect=True,
            )
        self._token = token
        try:
            timeout = int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_SECONDS
        self.timeout = max(5, min(timeout, MAX_TIMEOUT_SECONDS))
        parsed = urlparse(self.base_url)
        self._host = (parsed.hostname or "").lower()
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._scheme = parsed.scheme

    # -- addressing ---------------------------------------------------------

    def _pinned(self) -> tuple[str, ...]:
        try:
            ips = tuple(
                resolve_pinned_ips(self._host, port=self._port, purpose="Scraper service URL")
            )
        except ValueError as exc:
            raise ScraperError(
                "network_blocked",
                f"{exc}. A scraper service on a private network needs the platform setting "
                "FS_ALLOW_PRIVATE_REMOTE_URLS=1.",
                no_external_effect=True,
            ) from exc
        if self._scheme == "http" and not _allow_private():
            raise ScraperError(
                "network_blocked",
                "A public scraper service must be reached over https://.",
                no_external_effect=True,
            )
        return ips

    def url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url}{path}"
        params = {key: value for key, value in (query or {}).items() if value not in (None, "")}
        if params:
            url = f"{url}?{urlencode(params)}"
        return url

    @staticmethod
    def segment(value: str) -> str:
        return quote(as_text(value), safe="")

    # -- calls --------------------------------------------------------------

    def _headers(self, *, accept: str = _MEDIA_JSON, extra: dict[str, str] | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept,
            "User-Agent": USER_AGENT,
        }
        headers.update(extra or {})
        return headers

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        accept_statuses: tuple[int, ...] = (),
    ) -> tuple[int, Any, dict[str, str]]:
        """One JSON call. Returns (status, parsed body, response headers)."""
        data = None
        request_headers = self._headers(extra=headers)
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = _MEDIA_JSON
            request_headers["Content-Length"] = str(len(data))
        request = Request(self.url(path, query), data=data, method=method, headers=request_headers)
        pinned = self._pinned()
        try:
            with open_pinned_url(
                request,
                timeout_seconds=float(timeout or self.timeout),
                purpose="Scraper service",
                pinned_ips=pinned,
            ) as response:
                status = int(getattr(response, "status", 200) or 200)
                raw = _read_limited(response, JSON_MAX_BYTES)
                response_headers = {k.lower(): v for k, v in response.headers.items()}
        except HTTPError as exc:
            status = int(exc.code)
            raw = _read_limited(exc, ERROR_BODY_MAX_BYTES)
            parsed = _parse_json(raw, allow_empty=True)
            if status in accept_statuses:
                return status, parsed, {k.lower(): v for k, v in (exc.headers or {}).items()}
            raise from_service_error(status, parsed) from None
        except TimeoutError as exc:
            raise ScraperError(
                "service_timeout",
                f"The scraper service did not answer within {int(timeout or self.timeout)} "
                "seconds.",
                retryable=True,
            ) from exc
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise ScraperError(
                    "service_timeout",
                    "The scraper service did not answer in time.",
                    retryable=True,
                ) from exc
            raise ScraperError(
                "service_unreachable",
                f"The scraper service at {self.base_url} could not be reached: {reason}.",
                retryable=True,
                no_external_effect=True,
            ) from exc
        except (ConnectionError, OSError) as exc:
            if isinstance(exc, ValueError):
                raise ScraperError("network_blocked", str(exc), no_external_effect=True) from exc
            raise ScraperError(
                "service_unreachable",
                f"The scraper service at {self.base_url} could not be reached: {exc}.",
                retryable=True,
                no_external_effect=True,
            ) from exc
        except ValueError as exc:
            raise ScraperError("network_blocked", str(exc), no_external_effect=True) from exc
        return status, _parse_json(raw, allow_empty=False), response_headers

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)[1]

    def post(self, path: str, body: Any, **kwargs: Any) -> Any:
        return self.request("POST", path, body, **kwargs)[1]

    def put(self, path: str, body: Any, **kwargs: Any) -> Any:
        return self.request("PUT", path, body, **kwargs)[1]

    def stream(
        self,
        path: str,
        *,
        accept: str,
        offset: int = 0,
        etag: str = "",
        timeout: int | None = None,
    ) -> tuple[int, dict[str, str], Iterator[bytes]]:
        """Open a streamed download; the caller iterates and closes through the iterator."""
        extra: dict[str, str] = {}
        if offset > 0:
            extra["Range"] = f"bytes={offset}-"
            if etag:
                extra["If-Range"] = etag
        request = Request(
            self.url(path), method="GET", headers=self._headers(accept=accept, extra=extra)
        )
        pinned = self._pinned()
        try:
            response = open_pinned_url(
                request,
                timeout_seconds=float(timeout or self.timeout),
                purpose="Scraper service download",
                pinned_ips=pinned,
            )
        except HTTPError as exc:
            raw = _read_limited(exc, ERROR_BODY_MAX_BYTES)
            raise from_service_error(int(exc.code), _parse_json(raw, allow_empty=True)) from None
        except TimeoutError as exc:
            raise ScraperError(
                "service_timeout", "The result download did not start in time.", retryable=True
            ) from exc
        except URLError as exc:
            raise ScraperError(
                "service_unreachable",
                f"The scraper service could not be reached for the download: "
                f"{getattr(exc, 'reason', exc)}.",
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise ScraperError("network_blocked", str(exc), no_external_effect=True) from exc
        status = int(getattr(response, "status", 200) or 200)
        headers = {k.lower(): v for k, v in response.headers.items()}

        def chunks() -> Iterator[bytes]:
            try:
                while True:
                    chunk = response.read(CHUNK_BYTES)
                    if not chunk:
                        return
                    yield bytes(chunk)
            finally:
                response.close()

        return status, headers, chunks()


def _read_limited(response: Any, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = response.read(CHUNK_BYTES)
        except TimeoutError as exc:
            raise ScraperError(
                "service_timeout", "The scraper service stopped answering.", retryable=True
            ) from exc
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ScraperError(
                "invalid_service_response",
                f"The scraper service answered with more than {limit // (1024 * 1024)} MiB.",
            )
        chunks.append(bytes(chunk))
    return b"".join(chunks)


def _parse_json(raw: bytes, *, allow_empty: bool) -> Any:
    if not raw:
        if allow_empty:
            return {}
        raise ScraperError(
            "invalid_service_response", "The scraper service answered with an empty body."
        )
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if allow_empty:
            return {}
        raise ScraperError(
            "invalid_service_response", "The scraper service did not answer with JSON."
        ) from exc
