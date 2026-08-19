"""One HTTP client for every REST upstream we talk to.

``jupiter``, ``market_data`` and ``solana_tracker`` were three copies of
the same skeleton — session, limiter, timeout, ``raise_for_status``,
``except (RequestException, ValueError)`` — each with a slightly
different set of gaps. The sharpest was that ``solana_tracker`` detected
HTTP 429 and never told its own bucket about it, so the limiter happily
kept issuing into a wall. Throttle feedback is wired in here, once, for
everybody.

Errors carry the status code and any ``Retry-After``, so callers can
distinguish "throttled, try later" from "this key is not entitled" from
"the service is broken" — the same discrimination the RPC source layer
needs, and the reason the old blanket ``ChainError`` could not support
fall-through.
"""

from __future__ import annotations

import email.utils
import logging
import time
from typing import Any

import requests

from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 15.0


class HttpError(RuntimeError):
    """An upstream REST call failed.

    ``status`` is the HTTP status when there was one, ``retry_after`` the
    parsed ``Retry-After`` in seconds when the server supplied it.
    """

    def __init__(self, message: str, *, status: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def rate_limited(self) -> bool:
        return self.status == 429

    @property
    def unauthorized(self) -> bool:
        return self.status in (401, 403)


def parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` as seconds. Accepts both legal forms."""
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        # A malformed header must never become an exception on the
        # throttling path — we simply fall back to our own cooldown.
        return None
    if parsed is None:
        return None
    return max(parsed.timestamp() - time.time(), 0.0)


class HttpClient:
    """Rate-limited JSON-over-HTTP client for one upstream service."""

    def __init__(self, base_url: str, *, limiter: RateLimiter,
                 error_cls: type[HttpError] = HttpError,
                 headers: dict[str, str] | None = None,
                 timeout: float = DEFAULT_TIMEOUT_SEC,
                 name: str | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._limiter = limiter
        self._error_cls = error_cls
        self._timeout = timeout
        self._name = name or self._base
        self._session = requests.Session()
        self._session.headers.update({"accept": "application/json"})
        if headers:
            self._session.headers.update(headers)

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    def get(self, path: str, *, params: dict[str, Any] | None = None,
            timeout: float | None = None) -> Any:
        return self._request("GET", path, params=params, timeout=timeout)

    def post(self, path: str, *, json: Any = None,
             timeout: float | None = None) -> Any:
        return self._request("POST", path, json=json, timeout=timeout)

    def _request(self, method: str, path: str, *,
                 params: dict[str, Any] | None = None,
                 json: Any = None, timeout: float | None = None) -> Any:
        self._limiter.acquire()
        url = path if path.startswith("http") else f"{self._base}/{path.lstrip('/')}"
        try:
            response = self._session.request(
                method, url, params=params, json=json,
                timeout=timeout if timeout is not None else self._timeout)
        except requests.RequestException as exc:
            raise self._error_cls(f"{self._name}: {method} {path} failed: "
                                  f"{exc}") from exc

        status = response.status_code
        if status == 429:
            # The bucket MUST hear about this, or it keeps issuing into a
            # wall — the bug this consolidation exists to kill.
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            cooldown = self._limiter.penalize(retry_after)
            logger.warning("%s rate limited (HTTP 429); pausing %.1fs",
                           self._name, cooldown)
            raise self._error_cls(f"{self._name}: rate limited (HTTP 429)",
                                  status=status, retry_after=retry_after)
        if status in (401, 403):
            raise self._error_cls(
                f"{self._name}: not authorised for {path} (HTTP {status})",
                status=status)
        if status >= 400:
            raise self._error_cls(
                f"{self._name}: {method} {path} returned HTTP {status}",
                status=status)

        try:
            return response.json()
        except ValueError as exc:
            raise self._error_cls(f"{self._name}: {method} {path} returned a "
                                  f"non-JSON body: {exc}",
                                  status=status) from exc
