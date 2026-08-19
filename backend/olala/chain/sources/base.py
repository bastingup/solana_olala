"""What every RPC source must offer the router.

A source is one endpoint family. It knows how to make a call, how much
that call costs against its own budget, and whether it can batch. It
does NOT know about fall-through, preference order or policies — that is
the router's job, and keeping the two apart is what lets a source be
swapped, disabled or added from configuration alone.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class BatchItem:
    """One sub-call inside a batched request."""

    method: str
    params: list[Any]
    #: What this sub-call costs against the source's budget. Public nodes
    #: meter per sub-call, so a 50-address batch costs 50, not 1.
    cost: float = 1.0


@dataclass
class SourceStats:
    """Live counters for one source, for the operator and for tests."""

    calls: int = 0
    failures: int = 0
    rate_limited: int = 0
    batch_calls: int = 0
    sub_calls: int = 0
    #: Sub-calls billed by a metered source this month, persisted so a
    #: restart cannot reset the budget the governor is protecting.
    metered_units: int = 0
    last_error: str = ""
    last_latency_sec: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls, "failures": self.failures,
            "rate_limited": self.rate_limited,
            "batch_calls": self.batch_calls, "sub_calls": self.sub_calls,
            "metered_units": self.metered_units,
            "last_error": self.last_error,
            "last_latency_ms": round(self.last_latency_sec * 1000.0, 1),
        }


@runtime_checkable
class RpcSource(Protocol):
    """One interchangeable RPC endpoint family."""

    name: str

    @property
    def supports_batch(self) -> bool: ...

    @property
    def max_batch(self) -> int: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def stats(self) -> SourceStats: ...

    def ws_endpoint(self) -> str: ...

    def supports(self, method: str) -> bool:
        """False once the source has told us it does not implement it."""

    def try_reserve(self, cost: float, timeout: float | None) -> bool:
        """Take budget for a call, or refuse so the caller can move on."""

    def call(self, method: str, params: list[Any]) -> Any:
        """One JSON-RPC call. Raises a ``SourceError`` subclass."""

    def batch(self, items: list[BatchItem]) -> list[Any]:
        """Run sub-calls in ONE request, results in REQUEST order.

        A failed sub-call yields its exception object in that position
        rather than raising, so one bad address cannot discard 49 good
        answers. Responses are matched by JSON-RPC ``id`` — never by
        position, which was observed to differ on mainnet-beta.
        """


class CircuitBreaker:
    """Stops hammering a source that is clearly down.

    Opens after ``threshold`` consecutive failures and stays open for a
    cooldown that doubles on each reopen, so a persistently broken source
    is checked rarely while a blip costs one call. Any success closes it.
    """

    def __init__(self, threshold: int = 3, base_cooldown_sec: float = 5.0,
                 max_cooldown_sec: float = 120.0) -> None:
        self._threshold = max(1, threshold)
        self._base = base_cooldown_sec
        self._max = max_cooldown_sec
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0
        self._cooldown = base_cooldown_sec

    @property
    def closed(self) -> bool:
        with self._lock:
            return time.monotonic() >= self._open_until

    @property
    def open_for_sec(self) -> float:
        with self._lock:
            return max(0.0, self._open_until - time.monotonic())

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0
            self._cooldown = self._base

    def record_failure(self, retry_after: float | None = None) -> None:
        with self._lock:
            self._failures += 1
            if self._failures < self._threshold and retry_after is None:
                return
            cooldown = retry_after if retry_after is not None else self._cooldown
            self._open_until = time.monotonic() + min(cooldown, self._max)
            self._cooldown = min(self._cooldown * 2.0, self._max)
