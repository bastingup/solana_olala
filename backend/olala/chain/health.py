"""Asking idle sources whether they are alive.

The router only ever contacts the source it is routing to, so a standby
endpoint accumulates no evidence at all. That leaves the operator unable
to tell "we have not needed this one" from "this one is dead" — and a
fall-through chain whose spare links are of unknown health is not a
fall-through chain you can trust. The whole point of the ordering is
that the next source will answer when the first stops.

So the spares are pinged. ``getHealth`` is the cheapest question a
Solana node answers, and a source that has served real traffic recently
is skipped entirely: its success is already proof, and re-proving it
would be pure waste.

Metered sources are probed far less often. A credit spent proving Helius
is alive is a credit not spent fetching a trade, and at one probe a
minute the reassurance would cost tens of thousands of credits a month.
"""

from __future__ import annotations

import logging
import time

from ..services.daemon import Daemon
from .errors import ChainError
from .router import RpcRouter

logger = logging.getLogger(__name__)

#: How stale a source's last contact may be before it is probed.
DEFAULT_PROBE_INTERVAL_SEC = 30.0
#: Metered sources are probed this many times less often.
METERED_PROBE_MULTIPLIER = 10.0
#: A health check that cannot get budget or an answer quickly is a
#: health check that has already told us what we needed to know.
PROBE_TIMEOUT_SEC = 2.0


class SourceHealthDaemon(Daemon):
    """Keeps every configured source's health current, cheaply."""

    def __init__(self, router: RpcRouter,
                 interval_sec: float = DEFAULT_PROBE_INTERVAL_SEC) -> None:
        # Ticks often; the per-source interval decides what actually runs.
        super().__init__("source-health", max(interval_sec / 3.0, 1.0))
        self._router = router
        self._probe_interval = interval_sec

    def tick(self) -> None:
        now = time.time()
        for name, source in self._router.sources.items():
            if not source.enabled:
                continue
            interval = self._probe_interval
            if getattr(source, "metered", False):
                interval *= METERED_PROBE_MULTIPLIER
            if now - source.stats.last_contact_at < interval:
                # Recently exercised or recently probed: already known.
                continue
            self._probe(name, source)

    def _probe(self, name: str, source) -> None:
        if not source.try_reserve(1.0, PROBE_TIMEOUT_SEC):
            # No budget for a courtesy call. Not evidence of anything, so
            # nothing is recorded either way.
            return
        try:
            source.call("getHealth", [])
        except ChainError as exc:
            # The source itself records the failure; this is just the
            # one line an operator needs to see it happen.
            logger.info("health probe: %s is not answering (%s)", name, exc)
        except Exception:                                   # noqa: BLE001
            logger.exception("health probe for %s failed unexpectedly", name)
