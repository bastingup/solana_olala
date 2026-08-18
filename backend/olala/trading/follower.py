"""Follow daemon: watches followed traders and emits copy signals.

Polls each followed trader's recent signatures against a persisted cursor,
reconstructs any new swaps, and hands them to the trading engine in
chronological order (oldest first). The cursor only ever advances over
entries that were actually processed, so an RPC failure mid-poll resumes
exactly where it stopped and a burst larger than the per-tick budget is
carried into the next tick — trades are never silently skipped and never
executed twice. On first contact with a trader the cursor is armed at
their newest signature so history is never replayed as live trades.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from ..chain.provider import ChainError, RpcProvider
from ..config import ConfigStore
from ..discovery.reconstruction import TradeReconstructor
from ..domain.models import CopySignal
from ..services.daemon import Daemon
from ..services.traders import TraderRegistry
from .engine import TradingEngine

logger = logging.getLogger(__name__)

# Sized for high-frequency traders: the poll window must outrun the
# fastest burst between two push-triggered polls (~1.5s apart), and the
# fetch budget drains a backlog quickly without starving other traders.
SIGNATURES_PER_POLL = 30
MAX_TX_FETCHES_PER_TRADER = 10
PROCESSED_MEMORY = 500


class FollowDaemon(Daemon):
    def __init__(self, store: ConfigStore, provider: RpcProvider,
                 registry: TraderRegistry, engine: TradingEngine) -> None:
        super().__init__("follower", store.config.follow.poll_interval_sec)
        self._store = store
        self._provider = provider
        self._registry = registry
        self._engine = engine
        self._reconstructor = TradeReconstructor()
        # Defense in depth against replays: signatures already handed to
        # the engine, bounded LRU.
        self._processed: OrderedDict[str, None] = OrderedDict()
        # Serializes polls so a push-triggered poll and the interval tick
        # never walk the same trader's cursor concurrently.
        self._poll_lock = threading.Lock()

    def tick(self) -> None:
        for profile in self._registry.followed():
            try:
                with self._poll_lock:
                    self._poll_trader(profile)
            except ChainError as exc:
                logger.warning("poll failed for trader %s: %s",
                               profile.address, exc)

    def poll_now(self, address: str) -> None:
        """Immediate poll of one trader, triggered by an on-chain push
        notification. Same cursor protocol as the interval tick."""
        profile = self._registry.get(address)
        if profile is None or profile.status.value != "followed":
            return
        try:
            with self._poll_lock:
                self._poll_trader(profile)
        except ChainError as exc:
            logger.warning("push-triggered poll failed for %s: %s",
                           address, exc)

    def _poll_trader(self, profile) -> None:
        address = profile.address
        cursor = self._registry.follow_cursor(address)
        entries = self._provider.get_signatures(
            address, limit=SIGNATURES_PER_POLL)
        if not entries:
            return
        if not cursor:
            self._registry.update(profile,
                                  follow_cursor=entries[0]["signature"])
            return

        fresh = []
        cursor_seen = False
        for entry in entries:
            if entry["signature"] == cursor:
                cursor_seen = True
                break
            fresh.append(entry)
        if not fresh:
            return
        if not cursor_seen:
            logger.warning(
                "trader %s produced more than %d transactions between "
                "polls; entries older than this window cannot be copied",
                address, SIGNATURES_PER_POLL)

        fetches = 0
        # Oldest first; advance the cursor only over entries we processed.
        for entry in reversed(fresh):
            signature = entry["signature"]
            if entry.get("err") is None and signature not in self._processed:
                if fetches >= MAX_TX_FETCHES_PER_TRADER:
                    break  # Budget spent: the rest carries to next tick.
                fetches += 1
                tx = self._provider.get_transaction(signature)
                trade = self._reconstructor.reconstruct(
                    address, signature, tx)
                self._remember(signature)
                if trade is not None:
                    self._engine.handle_signal(CopySignal(
                        trader=address, side=trade.side, mint=trade.mint,
                        trader_sol_amount=trade.sol_amount, observed=trade))
            self._registry.update(profile, follow_cursor=signature)

    def _remember(self, signature: str) -> None:
        self._processed[signature] = None
        while len(self._processed) > PROCESSED_MEMORY:
            self._processed.popitem(last=False)
