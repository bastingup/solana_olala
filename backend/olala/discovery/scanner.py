"""Trader discovery daemon — orchestrates two independent streams.

**Stream A, the leaderboard** (:mod:`~olala.discovery.leaderboard`): a
service that indexes the whole chain ranks and vets the wallets, so we
take its output as given and follow the names it returns. Costs no RPC.

**Stream B, on-chain** (:mod:`~olala.discovery.onchain`): winners'
holders surfaces wallets nobody has vetted, so this daemon does the
work — pre-screen, incremental history reconstruction under a per-sweep
RPC budget, then the ``filters_onchain`` admission gate. **That section
governs this stream only**; applying it to Stream A would re-derive,
with a narrower window, a judgment the service already made better.

**Fall-through is unconditional:** Stream A runs first because it is
free, but if it is disabled, unkeyed, throttled, rate-limited or simply
broken, Stream B still runs every single sweep. Discovery never depends
on an external service being up.

Cursors persist, so scan progress survives restarts. The cross-winner
tally is in-memory and rebuilds over ticks.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from ..chain.market_data import MarketDataService
from ..chain.provider import ChainError, RpcProvider
from ..chain.solana_tracker import SolanaTrackerError
from ..config import ConfigStore
from ..constants import SECONDS_PER_DAY
from ..domain.models import TraderStatus
from ..events import EventBus
from ..persistence.database import Database
from ..services.daemon import Daemon
from ..services.traders import TraderRegistry
from .filters import TraderAdmissionFilter
from .leaderboard import LeaderboardSource
from .onchain import OnChainSource
from .reconstruction import TradeReconstructor
from .roster import Roster
from .budget import RpcBudget
from .scoring import TraderScorer

logger = logging.getLogger(__name__)

# getSignaturesForAddress returns up to 1,000 entries for the same one
# credit, so large batches make history listing nearly free — the deep
# scan's cost is the per-transaction fetches, as it should be.
SIGNATURE_BATCH = 500


class TraderDiscoveryDaemon(Daemon):
    def __init__(self, store: ConfigStore, provider: RpcProvider,
                 market_data: MarketDataService, registry: TraderRegistry,
                 db: Database, bus: EventBus,
                 assign_wallet: Callable[[str], str],
                 jupiter=None, tracker=None) -> None:
        super().__init__("discovery",
                         store.config.discovery.scan_interval_sec)
        self._store = store
        self._provider = provider
        self._market_data = market_data
        self._registry = registry
        self._db = db
        self._bus = bus
        self._assign_wallet = assign_wallet
        self._reconstructor = TradeReconstructor()
        self._scorer = TraderScorer()
        self._filter = TraderAdmissionFilter(market_data)
        self._oldest_seen: dict[str, float] = {}
        self._newest_seen: dict[str, str] = {}
        self._scanned_counts: dict[str, int] = {}
        self._counters = {"wallets_screened": 0, "bots_blocked": 0,
                          "too_thin": 0, "winners_mined": 0,
                          "smart_holders": 0, "histories_read": 0,
                          "admitted": 0, "rejected": 0}
        # Both streams compete for seats on the same terms.
        self._roster = Roster(registry, assign_wallet, self._counters, db)
        self.leaderboard = LeaderboardSource(
            tracker, registry, bus, self._roster, self._counters)
        self.onchain = OnChainSource(
            provider, market_data, registry, db, bus, self._counters,
            self._status, jupiter=jupiter)
        self._next_sweep_at = 0.0
        self.last_status: dict | None = None

    # -- live status -------------------------------------------------------

    def _status(self, phase: str, detail: str = "",
                budget: RpcBudget | None = None) -> None:
        """Narrate what discovery is doing right now to the dashboard.

        The latest payload is retained so a page loaded between sweeps
        still shows current state instead of an empty console.
        """
        config = self._store.config
        self.last_status = {
            "phase": phase,
            "detail": detail,
            "source": ("Solana Tracker PnL" if self.leaderboard.available
                       else "On-chain (winners' holders)"),
            "counters": dict(self._counters),
            "candidates": len(
                self._registry.by_status(TraderStatus.CANDIDATE)),
            "followed": len(self._registry.followed()),
            "roster_target": config.discovery.max_followed_traders,
            "next_sweep_at": self._next_sweep_at,
            "budget_left": budget._remaining if budget else None,
        }
        self._bus.publish("discovery_status", self.last_status)

    def tick(self) -> None:
        config = self._store.config
        self._next_sweep_at = time.time() + self._interval
        budget = RpcBudget(config.discovery.rpc_calls_per_scan)
        followed = self._registry.followed()
        # A full roster does NOT stop discovery: the sweep keeps hunting,
        # and any candidate that measures stronger than the weakest
        # followed trader replaces it (see _finalize_candidate).
        roster_full = len(followed) >= config.discovery.max_followed_traders
        logger.info("discovery tick: %d followed%s, %d candidates in "
                    "review, %d RPC calls budgeted",
                    len(followed), " (full — hunting for upgrades)"
                    if roster_full else "",
                    len(self._registry.by_status(TraderStatus.CANDIDATE)),
                    config.discovery.rpc_calls_per_scan)
        self._status("sweep_start",
                     "Roster full — hunting for an upgrade; a stronger "
                     "find replaces the weakest followed trader"
                     if roster_full else "Sweep started", budget)

        candidates = self._registry.by_status(TraderStatus.CANDIDATE)
        if len(candidates) < config.discovery.max_candidates_per_scan:
            try:
                self._harvest_candidates(config, budget)
            except ChainError as exc:
                logger.warning("candidate harvest failed: %s", exc)
            candidates = self._registry.by_status(TraderStatus.CANDIDATE)

        # Highest-conviction candidates first: wallets that bought early
        # into several different winners outrank single-hit finds, then
        # service-nominated wallets by their reported win rate.
        candidates.sort(key=lambda p: (
            -len(self.onchain.early_hits.get(p.address, ())),
            -self.leaderboard.rank.get(p.address, 0.0), p.discovered_at))
        for candidate in candidates:
            if budget.exhausted:
                break
            try:
                self._status("reading_history",
                             f"Reading {candidate.address[:6]}…'s trade "
                             "history from chain", budget)
                self._advance_candidate(config, candidate.address, budget)
            except ChainError as exc:
                logger.warning("candidate %s scan failed: %s",
                               candidate.address, exc)
        self._status("sweep_done",
                     "Sweep complete — waiting for the next one", budget)

    # -- candidate harvesting ---------------------------------------------

    def _harvest_candidates(self, config, budget: RpcBudget) -> None:
        """Run both streams, in cost order, with unconditional
        fall-through.

        Stream A (leaderboard) goes first because it spends no RPC and
        seats vetted names immediately. Stream B (on-chain) then runs
        NO MATTER WHAT — whether A is disabled, unkeyed, throttled,
        rate-limited, or raising. An outside service can slow discovery
        down; it can never stop it.
        """
        if self.leaderboard.due(config):
            try:
                self.leaderboard.harvest(config)
            except SolanaTrackerError as exc:
                logger.warning("leaderboard unavailable (%s) — falling "
                               "through to on-chain discovery", exc)
            except Exception:
                # Fall-through stays unconditional, but an unexpected
                # error here is OUR bug, not a service outage: log the
                # traceback so it cannot hide behind a routine warning.
                logger.exception("leaderboard stream raised unexpectedly — "
                                 "falling through to on-chain discovery")

        # Stream B: unconditional, and it owns the RPC budget.
        try:
            self.onchain.harvest(config, budget)
        except ChainError as exc:
            logger.warning("on-chain harvest failed: %s", exc)

    # -- history parsing and admission ------------------------------------

    def _advance_candidate(self, config, address: str,
                           budget: RpcBudget) -> None:
        cursor, complete = self._registry.history_cursor(address)
        if not complete:
            if not budget.take(1):
                return
            batch = self._provider.get_signatures(
                address, limit=SIGNATURE_BATCH, before=cursor or None)
            if not batch:
                complete = True
            else:
                if not cursor:
                    self._newest_seen[address] = batch[0]["signature"]
                trades = []
                # The cursor may only advance over entries whose
                # transactions were actually fetched (or need no fetch);
                # anything beyond a budget cut is revisited next tick.
                for entry in batch:
                    if entry.get("err") is None:
                        if not budget.take(1):
                            break
                        tx = self._provider.get_transaction(
                            entry["signature"])
                        trade = self._reconstructor.reconstruct(
                            address, entry["signature"], tx)
                        if trade:
                            trades.append(trade)
                    cursor = entry["signature"]
                    self._scanned_counts[address] = (
                        self._scanned_counts.get(address, 0) + 1)
                    entry_time = float(entry.get("blockTime") or 0.0)
                    if entry_time:
                        self._oldest_seen[address] = entry_time
                if trades:
                    self._db.insert_observed_trades(trades)
            profile = self._registry.get(address)
            if profile is None:
                return
            self._registry.update(profile, history_cursor=cursor,
                                  history_complete=complete)
            self._publish_progress(config, address, complete)

        if complete or self._has_enough_depth(config, address):
            self._finalize_candidate(config, address, budget)

    def _publish_progress(self, config, address: str,
                          complete: bool) -> None:
        """Report scan progress so the operator can watch discovery work."""
        scanned = self._scanned_counts.get(address, 0)
        trades = self._db.count_observed_trades(address)
        oldest = self._oldest_seen.get(address)
        depth_days = (time.time() - oldest) / SECONDS_PER_DAY if oldest else 0.0
        # The bar must track what _has_enough_depth actually requires,
        # or it reads 100% while the scan is still running.
        target_days = max(config.filters_onchain.min_history_days,
                          config.discovery.skill_window_days) * 1.1
        logger.info("candidate %s… scanned %d signatures, %d swaps, "
                    "history depth %.0fd of %.0fd needed%s",
                    address[:8], scanned, trades, depth_days, target_days,
                    " (history exhausted)" if complete else "")
        self._bus.publish("candidate_progress", {
            "address": address,
            "signatures_scanned": scanned,
            "trades_found": trades,
            "depth_days": round(depth_days, 1),
            "target_days": round(target_days, 1),
            "signatures_target": config.discovery.signatures_per_trader,
            "complete": complete,
        })

    def _has_enough_depth(self, config, address: str) -> bool:
        if not config.dev_mode:
            # Filters off: one scanned batch is enough to judge and
            # follow, so paper activity flows without a deep scan.
            return self._scanned_counts.get(address, 0) > 0
        if (self._scanned_counts.get(address, 0)
                >= config.discovery.signatures_per_trader):
            return True
        oldest = self._oldest_seen.get(address)
        if not oldest:
            return False
        covered_days = (time.time() - oldest) / SECONDS_PER_DAY
        required = max(config.filters_onchain.min_history_days,
                       config.discovery.skill_window_days)
        return covered_days >= required * 1.1

    def _finalize_candidate(self, config, address: str,
                            budget: RpcBudget) -> None:
        profile = self._registry.get(address)
        if profile is None or profile.status is not TraderStatus.CANDIDATE:
            return
        trades = self._db.load_observed_trades(address)
        # Skill is judged inside the window; the full record only proves
        # the wallet has been around and active long enough.
        cutoff = time.time() - config.discovery.skill_window_days * SECONDS_PER_DAY
        window = [t for t in trades if t.block_time >= cutoff]
        stats_full = self._scorer.compute_stats(address, trades)
        stats = self._scorer.compute_stats(
            address, window, stale_bag_days=config.filters_onchain.stale_bag_days)
        profile.stats = stats
        profile.score = self._scorer.score(stats)
        if not config.dev_mode:
            # Filters off: anyone with observed trades gets followed.
            passed = bool(window)
            reason = "" if passed else "no trades observed in window"
        else:
            passed, reason = self._filter.evaluate(
                config.filters_onchain, stats, window,
                full_history_days=stats_full.history_days)

        if not passed:
            self._roster.reject(profile, reason)
        else:
            if self._roster.claim_seat(config, address, profile.score):
                self._admit(profile, trades, budget)
            else:
                worst = min(self._registry.followed(),
                            key=lambda p: p.score)
                self._roster.reject(
                    profile,
                    f"roster full — score {profile.score:.3f} does "
                    f"not beat the weakest followed "
                    f"({worst.score:.3f} + "
                    f"{config.discovery.replace_margin:.2f} margin)")
        self._counters["histories_read"] += 1

    def _admit(self, profile, trades, budget: RpcBudget) -> None:
        address = profile.address
        # Arm the tracking watermark at the trader's CURRENT newest
        # signature, not the one from when scanning began — otherwise
        # days-old qualification-era trades replay as live signals.
        watermark: tuple[int, str] | None = None
        if budget.take(1):
            try:
                latest = self._provider.get_signatures(address, limit=1)
                if latest:
                    watermark = (int(latest[0].get("slot") or 0),
                                 latest[0]["signature"])
            except ChainError as exc:
                logger.warning("could not arm the tracking watermark for "
                               "%s: %s — the tracker will arm itself on "
                               "its next sweep", address, exc)
        self._roster.follow(profile, profile.score, watermark=watermark)
        logger.info("admitted trader %s (score %.3f, win rate %.0f%%)",
                    address, profile.score,
                    (profile.stats.win_rate if profile.stats else 0) * 100)
