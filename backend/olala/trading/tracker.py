"""Watching the wallets we follow.

This is the reconciliation half of tracking. Detection is the WebSocket's
job — it is sub-second and nearly free. The sweep's job is to PROVE that
nothing was missed, and its aggressiveness is therefore tied inversely to
how much the stream can be trusted:

``ROUND_ROBIN``
    One wallet per tick. Costs ~1 wallet-call per second whatever the
    roster size, and covers everyone in `roster × tick` seconds (42s for
    42 wallets). Used whenever the stream has recently proven itself
    live. Measured to be about **ten times cheaper** than batching.

``BATCH``
    Every followed wallet in ONE request, at a derived interval. Used at
    startup, whenever the stream is unproven or degraded, and after a
    detected gap. Buys full coverage in ~5 seconds precisely when the
    cheap gear cannot be trusted.

The interval is **derived, never hardcoded**:
``max(min_interval_sec, roster / max_wallet_calls_per_sec)``. Public nodes
meter by sub-call, so cost is `wallets ÷ interval` — measured on
publicnode, 10 wallet-calls/s runs clean and 16.7/s throttles within
about forty seconds. Deriving it means adding wallets widens the cadence
instead of silently producing 429s.

If no healthy source can batch (mainnet-beta cannot; Helius refuses
roster-sized batches), ``BATCH`` degrades to round-robin rather than
failing. One call per tick is something every source can serve, which
makes it the floor of the whole system.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from ..chain.errors import ChainError, SourceIncomplete
from ..chain.provider import RpcProvider, signatures_params
from ..chain.router import NoSourceAvailable
from ..chain.signature_walk import Watermark, collect_fresh
from ..chain.sources.base import BatchItem
from ..config import ConfigStore
from ..discovery.reconstruction import TradeReconstructor
from ..domain.models import CopySignal, TradeSide
from ..persistence.database import Database
from ..services.daemon import Daemon
from ..services.traders import TraderRegistry
from .signals import SignalQueue

logger = logging.getLogger(__name__)

TRACKING_POLICY = "tracking"
HISTORY_POLICY = "history"
#: Slots of margin kept in the processed ledger below the watermark.
#: Comfortably longer than any realistic reorg on Solana.
REORG_MARGIN_SLOTS = 300
#: Consecutive healthy observations before downshifting to the cheap
#: gear, so a single notification cannot flap the mode.
DOWNSHIFT_PROOFS = 2


def _is_legacy(watermark: Watermark) -> bool:
    """A cursor from before the watermark carried a slot."""
    return bool(watermark.signature) and watermark.slot == 0


class Gear(str, Enum):
    BATCH = "batch"
    ROUND_ROBIN = "round_robin"


@dataclass
class TrackingStatus:
    """What the tracker is doing, for the operator and for tests."""

    gear: str = Gear.BATCH.value
    roster: int = 0
    configured_interval_sec: float = 0.0
    achieved_interval_sec: float = 0.0
    last_sweep_at: float = 0.0
    full_coverage_sec: float = 0.0
    stream_proven: bool = False
    last_stream_proof_at: float = 0.0
    skipped_cycles: int = 0
    signals_emitted: int = 0
    stale_entries_blocked: int = 0
    gaps_detected: int = 0
    #: Pre-slot cursors that were still visible and simply learned their
    #: slot. Nothing was lost.
    legacy_upgraded: int = 0
    #: Pre-slot cursors that were no longer visible, so the marker was
    #: moved to the newest entry. Trades in the gap were NOT copied.
    legacy_rearmed: int = 0
    batch_source: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gear": self.gear, "roster": self.roster,
            "configured_interval_sec": round(self.configured_interval_sec, 2),
            "achieved_interval_sec": round(self.achieved_interval_sec, 2),
            "last_sweep_at": self.last_sweep_at,
            "full_coverage_sec": round(self.full_coverage_sec, 1),
            "stream_proven": self.stream_proven,
            "last_stream_proof_at": self.last_stream_proof_at,
            "skipped_cycles": self.skipped_cycles,
            "signals_emitted": self.signals_emitted,
            "stale_entries_blocked": self.stale_entries_blocked,
            "gaps_detected": self.gaps_detected,
            "legacy_upgraded": self.legacy_upgraded,
            "legacy_rearmed": self.legacy_rearmed,
            "batch_source": self.batch_source,
            "errors": list(self.errors[-5:]),
        }


class WalletTracker(Daemon):
    """Polls followed wallets and emits copy signals."""

    def __init__(self, store: ConfigStore, provider: RpcProvider,
                 registry: TraderRegistry, db: Database,
                 queue: SignalQueue,
                 on_status: Callable[[dict], None] | None = None) -> None:
        super().__init__("tracker", store.config.tracking.tick_sec)
        self._store = store
        self._provider = provider
        self._router = getattr(provider, "router", None)
        self._registry = registry
        self._db = db
        self._queue = queue
        self._on_status = on_status
        self._reconstructor = TradeReconstructor()
        self._lock = threading.Lock()
        self._watermarks: dict[str, Watermark] = {}
        self._processed: dict[str, dict[str, int]] = {}
        self._last_seen: dict[str, float] = {}
        self._cursor_index = 0
        self._next_batch_at = 0.0
        self._last_sweep_at = 0.0
        self._stream_proofs = 0
        self._last_stream_proof = 0.0
        self.status = TrackingStatus()
        self._load_watermarks()

    def interval_sec(self) -> float:
        return max(self._store.config.tracking.tick_sec, 0.05)

    # -- state -------------------------------------------------------------

    def _load_watermarks(self) -> None:
        for address, (slot, signature) in self._db.load_watermarks().items():
            if slot or signature:
                self._watermarks[address] = Watermark(slot=slot,
                                                      signature=signature)

    def _watermark(self, address: str) -> Watermark:
        with self._lock:
            return self._watermarks.get(address, Watermark())

    def _processed_for(self, address: str) -> dict[str, int]:
        with self._lock:
            cached = self._processed.get(address)
        if cached is not None:
            return cached
        mark = self._watermark(address)
        loaded = self._db.load_processed(
            address, max(mark.slot - REORG_MARGIN_SLOTS, 0))
        with self._lock:
            self._processed[address] = loaded
        return loaded

    # -- stream health -----------------------------------------------------

    def note_stream_alive(self) -> None:
        """The stream proved it is delivering.

        ``logsSubscribe`` dies silently on public RPC — no error, no
        notifications — so "we heard nothing" is NOT evidence that
        nothing happened. Only positive proof lets the cheap gear run.
        """
        with self._lock:
            self._last_stream_proof = time.time()
            self._stream_proofs += 1

    def note_activity(self, address: str, signature: str = "",
                      slot: int = 0) -> None:
        """Push fast-path: a notification named a specific transaction.

        The signature and slot ride in the notification, so this skips
        ``getSignaturesForAddress`` entirely — the fastest path we have
        to a copy.

        **It never advances the watermark.** Notifications arrive at
        ``confirmed`` while the sweep reconciles at a level below it;
        letting a push move the marker would jump the sweep over
        transactions it has not yet seen. Push only ADDS to the processed
        set, which is always safe.
        """
        self.note_stream_alive()
        if not signature:
            return
        profile = self._registry.get(address)
        if profile is None or profile.status.value != "followed":
            return
        if signature in self._processed_for(address):
            return
        try:
            self._handle_signature(address, signature, slot)
        except ChainError as exc:
            logger.warning("push-path fetch failed for %s: %s", address, exc)

    def _stream_is_proven(self) -> bool:
        config = self._store.config.tracking
        with self._lock:
            proofs = self._stream_proofs
            last = self._last_stream_proof
        if proofs < DOWNSHIFT_PROOFS:
            return False
        return (time.time() - last) <= config.stream_proof_interval_sec

    # -- the tick ----------------------------------------------------------

    def tick(self) -> None:
        roster = [p.address for p in self._registry.followed()]
        proven = self._stream_is_proven()
        batch_source = (self._router.batch_capable(TRACKING_POLICY)
                        if self._router is not None else None)

        gear = Gear.ROUND_ROBIN if (proven and roster) else Gear.BATCH
        if gear is Gear.BATCH and not batch_source:
            # Nothing here can batch. One call per tick is the floor every
            # source can serve, so degrade rather than go blind.
            gear = Gear.ROUND_ROBIN

        self.status.gear = gear.value
        self.status.roster = len(roster)
        self.status.stream_proven = proven
        self.status.last_stream_proof_at = self._last_stream_proof
        self.status.batch_source = batch_source or ""

        self._forget_unfollowed(roster)
        if not roster:
            self._publish_status()
            return

        interval = self._derived_interval(len(roster), batch_source)
        self.status.configured_interval_sec = interval

        if gear is Gear.ROUND_ROBIN:
            self.status.full_coverage_sec = len(roster) * self.interval_sec()
            self._poll_next(roster)
        else:
            self.status.full_coverage_sec = interval
            now = time.monotonic()
            if now >= self._next_batch_at:
                self._sweep(roster, interval)
                # Scheduled from COMPLETION, so a slow sweep skips the
                # cycle it overran instead of stacking another on top.
                after = time.monotonic()
                if after - now > interval:
                    self.status.skipped_cycles += 1
                self._next_batch_at = after + interval
        self._publish_status()

    def _forget_unfollowed(self, roster: list[str]) -> None:
        """Drop in-memory state for traders no longer followed.

        The roster turns over as better traders replace weaker ones, and
        the processed ledger is the heavy part of this state. The
        PERSISTED rows stay: if a trader is followed again later, their
        watermark must still be there or we would replay their history
        as live trades.
        """
        current = set(roster)
        with self._lock:
            stale = [a for a in self._processed if a not in current]
            for address in stale:
                self._processed.pop(address, None)
                self._last_seen.pop(address, None)

    def _derived_interval(self, roster_size: int,
                          source_name: str | None) -> float:
        """Cadence that fits the roster inside the source's measured rate.

        Cost is `wallets ÷ interval`, so growing the roster must widen the
        interval. Hardcoding a cadence is what makes a poll look
        affordable in testing and throttle in production.
        """
        config = self._store.config
        floor = config.tracking.min_interval_sec
        source = config.sources.get(source_name or "")
        ceiling = source.max_wallet_calls_per_sec if source else 0.0
        if ceiling <= 0:
            return floor
        return max(floor, math.ceil(roster_size / ceiling))

    # -- gears -------------------------------------------------------------

    def _poll_next(self, roster: list[str]) -> None:
        """One wallet per tick, cycling the roster."""
        with self._lock:
            if self._cursor_index >= len(roster):
                self._cursor_index = 0
            address = roster[self._cursor_index]
            self._cursor_index = (self._cursor_index + 1) % len(roster)
        try:
            self._reconcile(address, first_page=None)
        except (ChainError, NoSourceAvailable) as exc:
            self._note_error(f"{address[:8]}: {exc}")
        self._mark_swept()

    def _sweep(self, roster: list[str], interval: float) -> None:
        """Every followed wallet in one batched request."""
        config = self._store.config.tracking
        items = [BatchItem("getSignaturesForAddress",
                           [address,
                            signatures_params(config.signatures_per_poll)],
                           cost=1.0)
                 for address in roster]
        try:
            # The sweep may wait up to its own interval for budget — the
            # interval was derived from that same rate, so a batch that
            # cannot be funded within it means the roster has outgrown
            # the source, which the next tick's derivation will widen.
            results = self._router.batch(TRACKING_POLICY, items,
                                         timeout=interval)
        except (ChainError, NoSourceAvailable) as exc:
            # The sweep failed wholesale; the next tick re-evaluates the
            # gear, which is how a dead batch source becomes round-robin.
            self._note_error(f"batch sweep failed: {exc}")
            return

        for address, result in zip(roster, results):
            if isinstance(result, Exception):
                self._note_error(f"{address[:8]}: {result}")
                continue
            try:
                self._reconcile(address, first_page=result or [])
            except (ChainError, NoSourceAvailable) as exc:
                self._note_error(f"{address[:8]}: {exc}")
        self._mark_swept()

    def _mark_swept(self) -> None:
        now = time.time()
        if self._last_sweep_at:
            self.status.achieved_interval_sec = now - self._last_sweep_at
        self._last_sweep_at = now
        self.status.last_sweep_at = now

    # -- reconciliation ----------------------------------------------------

    def _reconcile(self, address: str,
                   first_page: list[dict[str, Any]] | None) -> None:
        config = self._store.config.tracking
        watermark = self._watermark(address)
        processed = self._processed_for(address)

        try:
            walk = collect_fresh(
                self._fetch_signatures, address, watermark,
                processed=processed,
                page_size=config.signatures_per_poll,
                first_page=first_page)
        except SourceIncomplete as exc:
            if _is_legacy(watermark):
                # A cursor written before slots were recorded. It cannot
                # be located and cannot be compared, so it would wedge on
                # an unbridgeable gap forever. Re-arm at the newest entry
                # and say plainly that trades during the gap are not
                # copied: skipping is recoverable, duplicating is not.
                self._rearm_legacy(address, first_page)
                return
            # The gap could not be bridged. The watermark stays exactly
            # where it is: losing sight of trades is recoverable, copying
            # them twice is not.
            self.status.gaps_detected += 1
            self._note_error(str(exc))
            return

        with self._lock:
            self._last_seen[address] = time.time()

        if not watermark.armed:
            if walk.newest is not None:
                self._advance(address, walk.newest)
            return
        if _is_legacy(watermark) and walk.matched is not None:
            # A pre-slot cursor that IS still visible: teach it its own
            # slot in place. Nothing is skipped and nothing replayed —
            # without this the marker keeps slot 0 forever and every
            # comparison stays as weak as the bug we replaced.
            self._advance(address, walk.matched)
            self.status.legacy_upgraded += 1
        if not walk.fresh:
            return

        budget = config.max_transactions_per_cycle
        fetched = 0
        last_handled: dict[str, Any] | None = None
        for entry in walk.fresh:                     # oldest first
            if entry.get("err") is not None:
                # A reverted transaction moved nothing; nothing to copy,
                # but the marker may still pass it.
                last_handled = entry
                continue
            signature = entry["signature"]
            if signature in processed:
                last_handled = entry
                continue
            if fetched >= budget:
                # Budget spent: the rest carries to the next cycle. The
                # watermark stops here, so nothing is skipped.
                break
            fetched += 1
            self._handle_signature(address, signature,
                                   int(entry.get("slot") or 0))
            last_handled = entry

        if last_handled is not None:
            self._advance(address, last_handled)

    def _rearm_legacy(self, address: str,
                      first_page: list[dict[str, Any]] | None) -> None:
        page = first_page
        if not page:
            page = self._fetch_signatures(address, 1, None)
        if not page:
            return
        newest = page[0]
        logger.warning(
            "%s carried a pre-slot cursor that is no longer visible; "
            "re-arming at %s (slot %s). Trades between the two are NOT "
            "copied — skipping is recoverable, duplicating is not.",
            address[:8], str(newest.get("signature"))[:12],
            newest.get("slot"))
        self.status.legacy_rearmed += 1
        self._advance(address, newest)

    def _fetch_signatures(self, address: str, limit: int,
                          before: str | None) -> list[dict[str, Any]]:
        return self._provider.get_signatures(address, limit=limit,
                                             before=before)

    def _handle_signature(self, address: str, signature: str,
                          slot: int) -> None:
        """Claim, fetch, reconstruct, dispatch, persist.

        The CLAIM comes first and is atomic. The push path runs on the
        subscriber's dispatch thread while the sweep runs on the tracker
        thread, and both check "have I seen this signature?" before
        spending ~100 ms fetching the transaction. Checking without
        claiming leaves both free to dispatch the same trade — a
        duplicate buy, which is the exact outcome this whole design
        exists to prevent.

        Persisting comes last: an unexpected failure then leaves the
        signature claimed in memory but not on disk, so a restart
        retries it rather than losing the copy silently.
        """
        if not self._claim(address, signature, slot or 0):
            return
        try:
            tx = self._provider.get_transaction(signature)
            trade = self._reconstructor.reconstruct(address, signature, tx)
            if trade is not None:
                self._dispatch(address, trade)
        except Exception:                                   # noqa: BLE001
            # The claim must not outlive a failed attempt, or the trade is
            # lost forever: the cursor never passed it, but nothing would
            # ever pick it up again either.
            self._release(address, signature)
            raise
        self._db.record_processed([(address, signature, slot or 0)])

    def _claim(self, address: str, signature: str, slot: int) -> bool:
        """Take exclusive responsibility for one signature, or decline.

        Held for the whole attempt, so the push path and the sweep can
        never both be mid-fetch on the same signature.
        """
        with self._lock:
            seen = self._processed.setdefault(address, {})
            if signature in seen:
                return False
            seen[signature] = slot
            return True

    def _release(self, address: str, signature: str) -> None:
        """Give a claim back so the next cycle retries it."""
        with self._lock:
            self._processed.get(address, {}).pop(signature, None)

    def _dispatch(self, address: str, trade) -> None:
        config = self._store.config.tracking
        age = time.time() - (trade.block_time or 0.0)
        if (trade.side is TradeSide.BUY and trade.block_time
                and age > config.max_signal_age_sec):
            # After an outage the backlog would otherwise buy into
            # positions the trader has already exited. Exits are never
            # blocked by age — a late sell is still the right sell.
            self.status.stale_entries_blocked += 1
            logger.warning("skipping stale entry for %s: %s is %.0fs old "
                           "(limit %.0fs)", address, trade.mint, age,
                           config.max_signal_age_sec)
            return
        self._queue.submit(CopySignal(
            trader=address, side=trade.side, mint=trade.mint,
            trader_sol_amount=trade.sol_amount, observed=trade))
        self.status.signals_emitted += 1

    def _advance(self, address: str, entry: dict[str, Any]) -> None:
        slot = int(entry.get("slot") or 0)
        signature = str(entry.get("signature") or "")
        if not signature:
            return
        with self._lock:
            self._watermarks[address] = Watermark(slot=slot,
                                                  signature=signature)
        self._db.update_watermarks([(address, slot, signature)])
        if slot > REORG_MARGIN_SLOTS:
            self._prune(address, slot - REORG_MARGIN_SLOTS)

    def _prune(self, address: str, before_slot: int) -> None:
        with self._lock:
            cached = self._processed.get(address)
            if cached:
                for signature in [s for s, sl in cached.items()
                                  if sl < before_slot]:
                    cached.pop(signature, None)
        self._db.prune_processed(address, before_slot)

    # -- health ------------------------------------------------------------

    def blind_reason(self, address: str) -> str:
        """Why this trader cannot be entered right now, if so.

        Deliberately PER TRADER. The realistic failure is not "everything
        is down" — it is one wallet quietly falling out of view while the
        dashboard looks green, so we copy its buy and never see its sell.

        Exits are never gated by this; closing a position we cannot watch
        is precisely the right move.
        """
        config = self._store.config.tracking
        # Two full sweeps of grace: one missed cycle is ordinary jitter.
        limit = max(self.status.full_coverage_sec * 2.0,
                    config.min_interval_sec * 4.0)
        with self._lock:
            seen = self._last_seen.get(address, 0.0)
        if not seen:
            return ("not yet observed on chain — refusing to enter a "
                    "position we cannot watch")
        age = time.time() - seen
        if age > limit:
            return (f"last seen {age:.0f}s ago (limit {limit:.0f}s) — "
                    f"refusing to enter a position we cannot watch")
        return ""

    # -- reporting ---------------------------------------------------------

    def _note_error(self, detail: str) -> None:
        logger.warning("tracking: %s", detail)
        self.status.errors.append(detail)
        del self.status.errors[:-10]

    def _publish_status(self) -> None:
        if self._on_status is not None:
            self._on_status(self.status.to_dict())
