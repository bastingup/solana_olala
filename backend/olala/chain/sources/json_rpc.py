"""The one JSON-RPC source implementation.

There is deliberately no class per vendor. Helius, publicnode and
mainnet-beta differ only in URL, credential, rate ceiling, batch support
and whether they are metered — every one of which is data. A subclass per
provider is precisely the hardcoding this rework removes: adding,
reordering or disabling a source must be a configuration edit.

Two behaviours here are load-bearing and were learned by measurement:

* **Batch responses are matched by JSON-RPC ``id``, never by position.**
  mainnet-beta returned a 50-element batch out of order. Zipping by index
  would have silently attributed one wallet's transactions to another —
  the worst possible failure for a copy trader.
* **A batch costs the sum of its sub-calls.** Public nodes meter by
  sub-call, so a 50-address batch spends 50 units of budget. Charging it
  as one request is what makes a "3-second poll" throttle after 40s.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from typing import Any

import requests

from ...config import AppConfig, SourceConfig
from ..errors import (SourceDataError, SourceError, SourceRateLimited,
                      SourceUnavailable, SourceUnsupported,
                      classify_rpc_error)
from ..http import parse_retry_after
from ..rate_limiter import RateLimiter
from .base import BatchItem, SourceStats

logger = logging.getLogger(__name__)


def redact(endpoint: str) -> str:
    """Endpoints carry API keys — never let one reach a log file."""
    head, _, _ = endpoint.partition("?")
    return head


class JsonRpcSource:
    """One Solana JSON-RPC endpoint family, described by configuration."""

    def __init__(self, name: str, config: SourceConfig,
                 timeout_sec: float = 15.0) -> None:
        self.name = name
        self._config = config
        self._timeout = timeout_sec
        endpoints = [_with_key(url, config.api_key)
                     for url in config.endpoints if url]
        if not endpoints:
            raise ValueError(f"source {name!r} has no endpoints")
        self._endpoints = itertools.cycle(endpoints)
        self._primary = endpoints[0]
        self._ws = _with_key(config.ws_endpoint, config.api_key)
        self._session = requests.Session()
        self._session.headers.update({"content-type": "application/json"})
        self._limiter = RateLimiter(
            max(config.max_wallet_calls_per_sec, 0.1),
            burst=max(int(config.max_wallet_calls_per_sec), 1))
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._stats = SourceStats()
        self._unsupported: set[str] = set()

    # -- description -------------------------------------------------------

    @property
    def supports_batch(self) -> bool:
        return bool(self._config.supports_batch)

    @property
    def max_batch(self) -> int:
        return max(1, int(self._config.max_batch))

    @property
    def max_wallet_calls_per_sec(self) -> float:
        return max(float(self._config.max_wallet_calls_per_sec), 0.1)

    @property
    def metered(self) -> bool:
        return bool(self._config.metered)

    @property
    def monthly_credit_cap(self) -> int:
        return int(self._config.monthly_credit_cap)

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled)

    @property
    def stats(self) -> SourceStats:
        return self._stats

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    def ws_endpoint(self) -> str:
        if self._ws:
            return self._ws
        return self._primary.replace("https://", "wss://", 1)

    def supports(self, method: str) -> bool:
        with self._lock:
            return method not in self._unsupported

    # -- budget ------------------------------------------------------------

    def try_reserve(self, cost: float = 1.0,
                    timeout: float | None = None) -> bool:
        """Take ``cost`` from this source's budget, or refuse.

        Refusing rather than blocking is what lets the router move to the
        next source instead of queueing behind a throttled one.
        """
        return self._limiter.acquire(cost, timeout)

    # -- calls -------------------------------------------------------------

    def call(self, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids),
                   "method": method, "params": params}
        body = self._post(payload, method=method, sub_calls=1)
        if not isinstance(body, dict):
            raise SourceDataError(
                f"{method}: expected an object, got {type(body).__name__}",
                source=self.name)
        if "error" in body:
            raise self._classify(body["error"], method)
        return body.get("result")

    def batch(self, items: list[BatchItem]) -> list[Any]:
        """Run sub-calls in one request; results in REQUEST order.

        A failed sub-call yields its exception in that slot rather than
        raising, so one bad address cannot discard the rest.
        """
        if not items:
            return []
        if not self.supports_batch:
            raise SourceUnsupported(f"{self.name} does not batch",
                                    source=self.name)

        base_id = next(self._ids) * 1000
        payload = [{"jsonrpc": "2.0", "id": base_id + index,
                    "method": item.method, "params": item.params}
                   for index, item in enumerate(items)]
        cost = sum(item.cost for item in items)
        body = self._post(payload, method="batch", sub_calls=len(items),
                          cost=cost)

        if isinstance(body, dict) and "error" in body:
            # A single error object for the whole batch is legal JSON-RPC.
            raise self._classify(body["error"], "batch")
        if not isinstance(body, list):
            raise SourceDataError(
                f"batch: expected an array, got {type(body).__name__}",
                source=self.name)

        # Match on id. Position is NOT reliable — mainnet-beta returns
        # batch responses out of order, and zipping by index would
        # attribute one wallet's transactions to another.
        by_id: dict[Any, dict] = {}
        for entry in body:
            if isinstance(entry, dict) and "id" in entry:
                by_id[entry["id"]] = entry

        results: list[Any] = []
        for index, item in enumerate(items):
            entry = by_id.get(base_id + index)
            if entry is None:
                results.append(SourceIncompleteResponse(
                    f"{item.method}: no response for sub-call {index}",
                    source=self.name))
                continue
            if "error" in entry:
                results.append(self._classify(entry["error"], item.method))
                continue
            results.append(entry.get("result"))
        return results

    # -- transport ---------------------------------------------------------

    def _post(self, payload: Any, *, method: str, sub_calls: int,
              cost: float | None = None) -> Any:
        endpoint = next(self._endpoints)
        started = time.monotonic()
        try:
            response = self._session.post(endpoint, json=payload,
                                          timeout=self._timeout)
        except requests.RequestException as exc:
            self._note_failure(str(exc))
            raise SourceUnavailable(f"{method}: {exc}",
                                    source=self.name) from exc
        finally:
            self._stats.last_latency_sec = time.monotonic() - started

        status = response.status_code
        if status == 429:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            cooldown = self._limiter.penalize(retry_after)
            self._stats.rate_limited += 1
            self._note_failure(f"HTTP 429 (paused {cooldown:.1f}s)")
            raise SourceRateLimited(
                f"{method}: {self.name} is rate limiting us",
                source=self.name, retry_after=retry_after)
        if status >= 400:
            self._note_failure(f"HTTP {status}")
            raise SourceUnavailable(
                f"{method}: {redact(endpoint)} returned HTTP {status}",
                source=self.name)

        try:
            body = response.json()
        except ValueError as exc:
            self._note_failure("non-JSON body")
            raise SourceDataError(f"{method}: non-JSON response: {exc}",
                                  source=self.name) from exc

        self._stats.last_ok_at = time.time()
        self._stats.calls += 1
        self._stats.sub_calls += sub_calls
        if sub_calls > 1:
            self._stats.batch_calls += 1
        if self.metered:
            self._stats.metered_units += sub_calls
        return body

    def _classify(self, error: Any, method: str) -> SourceError:
        if not isinstance(error, dict):
            return SourceDataError(f"{method}: {error}", source=self.name)
        exc = classify_rpc_error(error, source=self.name, method=method)
        if isinstance(exc, SourceUnsupported):
            with self._lock:
                if method not in self._unsupported and method != "batch":
                    self._unsupported.add(method)
                    logger.info("%s does not implement %s — it will not be "
                                "asked again", self.name, method)
        if isinstance(exc, SourceRateLimited):
            self._limiter.penalize(exc.retry_after)
            self._stats.rate_limited += 1
        self._note_failure(str(exc))
        return exc

    def _note_failure(self, detail: str) -> None:
        self._stats.failures += 1
        self._stats.last_error = detail
        self._stats.last_failure_at = time.time()


class SourceIncompleteResponse(SourceDataError):
    """A batch came back missing one of the sub-calls we asked for."""


def _with_key(url: str, api_key: str) -> str:
    """Attach the credential a keyed endpoint needs, if any."""
    if not url or not api_key or "api-key=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}api-key={api_key}"


def build_sources(config: AppConfig) -> dict[str, JsonRpcSource]:
    """Every configured, enabled source that has somewhere to connect."""
    sources: dict[str, JsonRpcSource] = {}
    for name, source_config in config.sources.items():
        if not source_config.enabled or not source_config.endpoints:
            continue
        try:
            sources[name] = JsonRpcSource(
                name, source_config, config.chain.request_timeout_sec)
        except ValueError as exc:
            logger.warning("source %s not usable: %s", name, exc)
    return sources
