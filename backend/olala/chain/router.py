"""Ordered fall-through across RPC sources, by policy.

Routing is by POLICY NAME, not by RPC method: ``tracking``, ``history``,
``metadata``, ``broadcast``, ``confirm``, ``stream``. A policy reads like
intent, and the operator reorders sources per policy without knowing
which methods each one implies.

Three rules carry the weight:

**Rejections do not fail over.** A ``SourceRejected`` means our request
was wrong; asking four more nodes the same bad question is pure waste.

**Sessions pin a source.** Multi-call sequences must be served by one
source or they are incoherent. The sharp case is money: broadcast a swap
on Helius, then ask publicnode for its status, and publicnode — which
never saw it — answers ``null``. The executor reads that as "definitively
never landed" and writes a TIMEOUT receipt while the swap is confirming.
Inside a session, a failure raises instead of silently switching, so the
caller restarts the whole sequence knowingly.

**A source that cannot grant budget in time is skipped, not waited on.**
That is what makes fall-through proactive rather than a post-mortem.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any, Callable, Iterator

from ..config import AppConfig
from .errors import (ChainError, SourceError, SourceRateLimited,
                     SourceUnavailable, SourceUnsupported)
from .sources.base import BatchItem, CircuitBreaker
from .sources.json_rpc import JsonRpcSource, build_sources

logger = logging.getLogger(__name__)

# How long to wait for a source's budget before trying the next one.
# Long enough to absorb ordinary jitter, short enough that a throttled
# source never becomes the reason a poll cycle overruns.
DEFAULT_RESERVE_TIMEOUT_SEC = 1.5

# How recently a source must have served ROUTED traffic to count as the
# one carrying the load, rather than a standby that merely answers.
ACTIVE_WINDOW_SEC = 10.0
# Beyond this we have not confirmed the source lately — longer than the
# metered probe interval, so a healthy Helius never looks stale.
READY_WINDOW_SEC = 600.0


class NoSourceAvailable(ChainError):
    """Every source in the policy is unusable right now.

    Deliberately NOT a ``SourceError``: this is a statement about the
    whole chain of sources, not about one of them.
    """


class RpcRouter:
    """Chooses which source serves a call, and what happens when it fails."""

    def __init__(self, config: AppConfig,
                 sources: dict[str, JsonRpcSource] | None = None,
                 reserve_timeout_sec: float = DEFAULT_RESERVE_TIMEOUT_SEC
                 ) -> None:
        self._sources = sources if sources is not None else build_sources(config)
        self._policies = {
            name: [s for s in getattr(config.routing, name)
                   if s in self._sources]
            for name in ("tracking", "history", "metadata", "broadcast",
                         "confirm", "stream")
        }
        self._breakers = {name: CircuitBreaker()
                          for name in self._sources}
        self._reserve_timeout = reserve_timeout_sec
        self._lock = threading.Lock()
        self._routed: dict[str, int] = {}
        self._routed_at: dict[str, float] = {}
        self._failovers = 0
        for policy, names in self._policies.items():
            if not names:
                logger.warning("routing policy %r has no usable source",
                               policy)

    # -- introspection -----------------------------------------------------

    @property
    def sources(self) -> dict[str, JsonRpcSource]:
        return dict(self._sources)

    def policy(self, name: str) -> list[str]:
        return list(self._policies.get(name, ()))

    def available(self, policy: str) -> list[str]:
        """Sources in this policy that are not circuit-broken right now."""
        return [n for n in self._policies.get(policy, ())
                if self._sources[n].enabled and self._breakers[n].closed]

    def batch_capable(self, policy: str) -> str | None:
        """The preferred source in this policy that can batch, if any.

        The tracker asks this to decide which gear it can run in; when
        the answer is None it round-robins instead of batching.
        """
        for name in self.available(policy):
            if self._sources[name].supports_batch:
                return name
        return None

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            routed = dict(self._routed)
            failovers = self._failovers
        return {
            "routed": routed,
            "failovers": failovers,
            "sources": {
                name: {
                    **source.stats.to_dict(),
                    "enabled": source.enabled,
                    "breaker_open_for_sec": round(
                        self._breakers[name].open_for_sec, 1),
                    "supports_batch": source.supports_batch,
                    "max_batch": source.max_batch,
                    "metered": source.metered,
                    "state": self.source_state(name),
                }
                for name, source in self._sources.items()
            },
        }

    def source_state(self, name: str) -> str:
        """One word for what this source is doing for us right now.

        Computed here rather than in the UI because only the router knows
        the difference between "not needed" and "not working" — and a
        standby endpoint shown in the same colour as a broken one hides
        the very failure the fall-through exists to survive.

            off      configured but disabled (no credential, say)
            active   serving traffic now
            ready    answered recently, standing by
            down     its last contact failed, or its breaker is open
            unknown  never contacted; nothing to claim either way
        """
        source = self._sources.get(name)
        if source is None or not source.enabled:
            return "off"
        if not self._breakers[name].closed:
            return "down"
        stats = source.stats
        if not stats.last_contact_at:
            return "unknown"
        if not stats.responding:
            return "down"
        # "Active" means SERVING, which is not the same as "answered
        # something recently" — a health probe would otherwise make an
        # idle standby look like it was carrying the load for ten seconds
        # out of every thirty. Only calls the router actually routed
        # count, and probes go straight to the source, bypassing it.
        with self._lock:
            routed_at = self._routed_at.get(name, 0.0)
        now = time.time()
        if now - routed_at <= ACTIVE_WINDOW_SEC:
            return "active"
        return "ready" if now - stats.last_ok_at <= READY_WINDOW_SEC \
            else "unknown"

    # -- routing -----------------------------------------------------------

    def call(self, policy: str, method: str, params: list[Any],
             cost: float = 1.0) -> Any:
        """One RPC call, served by the first source that can take it."""
        return self._run(
            policy, method, cost,
            lambda source: source.call(method, params))

    def batch(self, policy: str, items: list[BatchItem],
              timeout: float | None = None) -> list[Any]:
        """One batched request, served by a batch-capable source.

        Chunking to the source's measured ``max_batch`` happens here, so
        callers may pass a roster of any size.

        The budget wait is derived from COST, not borrowed from the
        single-call timeout. A 42-address batch against a 10/s ceiling
        inherently needs ~4.2 seconds of accumulation — which is exactly
        the interval the caller derived from the same two numbers — so
        judging it by the 1.5s meant for one call rejects every sweep
        forever.
        """
        if not items:
            return []
        source = self._pick_batch_source(policy, items)
        results: list[Any] = []
        for start in range(0, len(items), source.max_batch):
            chunk = items[start:start + source.max_batch]
            cost = sum(item.cost for item in chunk)
            wait = self._batch_timeout(source, cost, timeout)
            if not source.try_reserve(cost, wait):
                raise SourceRateLimited(
                    f"{source.name} has no budget for {len(chunk)} sub-calls "
                    f"within {wait:.1f}s", source=source.name)
            try:
                results.extend(source.batch(chunk))
            except SourceError as exc:
                self._breakers[source.name].record_failure(exc.retry_after)
                raise
            self._breakers[source.name].record_success()
            self._count(source.name)
        return results

    def _batch_timeout(self, source, cost: float,
                       explicit: float | None) -> float:
        if explicit is not None:
            return max(explicit, 0.0)
        rate = getattr(source, "max_wallet_calls_per_sec", 0.0) or 0.0
        if rate <= 0:
            return self._reserve_timeout
        # Time to accumulate the batch, plus a little slack for jitter.
        return max(self._reserve_timeout, cost / rate + 1.0)

    def _pick_batch_source(self, policy: str,
                           items: list[BatchItem]) -> JsonRpcSource:
        name = self.batch_capable(policy)
        if name is None:
            raise NoSourceAvailable(
                f"no batch-capable source available for policy {policy!r} "
                f"({len(items)} sub-calls requested)")
        return self._sources[name]

    def _run(self, policy: str, method: str, cost: float,
             action: Callable[[JsonRpcSource], Any]) -> Any:
        candidates = self._policies.get(policy)
        if not candidates:
            raise NoSourceAvailable(f"routing policy {policy!r} has no source")

        last_error: Exception | None = None
        skipped: list[str] = []
        for name in candidates:
            source = self._sources[name]
            breaker = self._breakers[name]
            if not source.enabled or not breaker.closed:
                skipped.append(name)
                continue
            if not source.supports(method):
                skipped.append(name)
                continue
            if not source.try_reserve(cost, self._reserve_timeout):
                # Throttled here — do not queue behind it, ask the next.
                skipped.append(name)
                last_error = SourceRateLimited(
                    f"{name} could not grant budget for {method}",
                    source=name)
                continue
            try:
                result = action(source)
            except SourceError as exc:
                last_error = exc
                breaker.record_failure(exc.retry_after)
                if not exc.failover:
                    # Our request is wrong; every source will say so.
                    raise
                if isinstance(exc, SourceUnsupported):
                    logger.debug("%s cannot serve %s; falling through",
                                 name, method)
                else:
                    logger.warning("%s failed %s (%s); falling through",
                                   name, method, exc)
                self._note_failover()
                continue
            breaker.record_success()
            self._count(name)
            return result

        raise self._exhausted(policy, method, candidates, skipped, last_error)

    def _exhausted(self, policy: str, method: str, candidates: list[str],
                   skipped: list[str], last_error: Exception | None
                   ) -> ChainError:
        detail = f"{method} failed on every source in policy {policy!r} "
        detail += f"(tried {candidates}"
        if skipped:
            detail += f", unavailable: {skipped}"
        detail += ")"
        if last_error is not None:
            detail += f": {last_error}"
        return NoSourceAvailable(detail)

    def _count(self, name: str) -> None:
        with self._lock:
            self._routed[name] = self._routed.get(name, 0) + 1
            self._routed_at[name] = time.time()

    def _note_failover(self) -> None:
        with self._lock:
            self._failovers += 1

    # -- pinned sessions ---------------------------------------------------

    @contextlib.contextmanager
    def session(self, policy: str,
                pin: str | None = None) -> Iterator["PinnedSession"]:
        """Serve a multi-call sequence from ONE source.

        ``pin`` forces a specific source — how a confirmation is bound to
        whichever node actually broadcast the transaction. Without it the
        first usable source in the policy is chosen and then held.
        """
        name = pin or next(iter(self.available(policy)), None)
        if name is None or name not in self._sources:
            raise NoSourceAvailable(
                f"no source available to pin for policy {policy!r}"
                + (f" (requested {pin!r})" if pin else ""))
        yield PinnedSession(self, self._sources[name])


class PinnedSession:
    """Every call goes to one source; a failure aborts, never switches.

    Silently switching mid-sequence is what turns "the swap is
    confirming" into "the swap definitively never landed".
    """

    def __init__(self, router: RpcRouter, source: JsonRpcSource) -> None:
        self._router = router
        self._source = source

    @property
    def source_name(self) -> str:
        return self._source.name

    def call(self, method: str, params: list[Any], cost: float = 1.0) -> Any:
        if not self._source.try_reserve(cost, self._router._reserve_timeout):
            raise SourceRateLimited(
                f"{self._source.name} has no budget for {method} inside a "
                f"pinned session", source=self._source.name)
        try:
            result = self._source.call(method, params)
        except SourceError:
            self._router._breakers[self._source.name].record_failure()
            raise
        except Exception as exc:                      # noqa: BLE001
            raise SourceUnavailable(f"{method}: {exc}",
                                    source=self._source.name) from exc
        self._router._breakers[self._source.name].record_success()
        self._router._count(self._source.name)
        return result
