"""Trader discovery daemon.

Candidate sourcing is **winners' holders**, fully on-chain: find tokens
that just made a hard 24h run (keyless trending stats, DexScreener search
as fallback) and read each winner's top token holders — you cannot hold
top-20 size in a token that just ran without having bought early, so a
winner's holder list is a list of its successful entries. Program-owned
accounts (pool vaults, lockers — off-curve addresses) and pool-scale
shares are dropped; the remaining real wallets are pre-screened and
verified. Wallets holding size in MULTIPLE winners are scanned first.

Two prior approaches were measured against live mainnet and discarded:
random DEX-flow sampling (bot density ≈ 100%, elite yield ≈ 0) and
pool-history backtracking (pool signatures are ~98% MEV spam; reaching
the pre-run window costs tens of thousands of signatures on any trending
pool). The holder read costs 3 RPC calls per winner and every result is,
by construction, a wallet that entered a winner early with real size.

(When a Solana Tracker API key is configured, its PnL leaderboard is used
as an additional nomination source; without one the system is on-chain
only.)

Every candidate passes a cheap pre-screen before any deep work: one
signature-history call sized to the trade-count requirement answers both
"can this wallet even hold enough trades?" and "is it transacting at
machine frequency?". Rejects never consume a history scan. Survivors get
incremental history reconstruction under a strict per-tick RPC budget,
then the admission filters (which additionally gate on copyability:
median holding period and trades/day).

Cursors persist, so progress survives restarts. The cross-winner tally is
in-memory and rebuilds over ticks after a restart.
"""

from __future__ import annotations

import logging
from typing import Callable

from solders.pubkey import Pubkey

from ..chain.market_data import MarketDataService
from ..chain.provider import ChainError, RpcProvider
from ..config import ConfigStore
from ..domain.models import TraderStatus
from ..events import EventBus
from ..persistence.database import Database
from ..services.daemon import Daemon
from ..services.traders import TraderRegistry
from .filters import TraderAdmissionFilter
from .reconstruction import TradeReconstructor
from .scoring import TraderScorer

logger = logging.getLogger(__name__)

# getSignaturesForAddress returns up to 1,000 entries for the same one
# credit, so large batches make history listing nearly free — the deep
# scan's cost is the per-transaction fetches, as it should be.
SIGNATURE_BATCH = 500
TX_CALLS_PER_CANDIDATE = 12
WINNER_COOLDOWN_SEC = 6 * 3600.0

# Pre-screen: raw signature events per day include non-swap activity, so
# the ceiling is a multiple of the trades/day admission gate. A human at
# 40 trades/day stays far under it; the bots we caught ran thousands.
PRESCREEN_SIGNATURES = 30
PRESCREEN_RATE_MULTIPLIER = 5.0
# A wallet's swap count can never exceed its signature count, so a wallet
# with fewer signatures than the trade-count requirement is disqualified
# by arithmetic — worth knowing before we spend a history scan on it.
PRESCREEN_MAX_FETCH = 1000


class RpcBudget:
    def __init__(self, calls: int) -> None:
        self._remaining = calls

    def take(self, count: int = 1) -> bool:
        if self._remaining < count:
            return False
        self._remaining -= count
        return True

    @property
    def exhausted(self) -> bool:
        return self._remaining <= 0


class TraderDiscoveryDaemon(Daemon):
    def __init__(self, store: ConfigStore, provider: RpcProvider,
                 market_data: MarketDataService, registry: TraderRegistry,
                 db: Database, bus: EventBus,
                 assign_wallet: Callable[[], str],
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
        self._jupiter = jupiter
        self._tracker = tracker
        # The leaderboard service is an optional accelerator on a free
        # tier: polled at most every leaderboard_interval_sec, and marked
        # attempted even on failure so a rate-limited service is not
        # hammered every sweep.
        self._last_leaderboard_at = 0.0
        # Service leaderboard position per nominated wallet (higher =
        # better) — only used to decide who gets deep-scanned FIRST,
        # following whatever ranking the operator configured; judgment
        # stays ours.
        self._service_rank: dict[str, float] = {}
        self._reconstructor = TradeReconstructor()
        self._scorer = TraderScorer()
        self._filter = TraderAdmissionFilter(market_data)
        self._oldest_seen: dict[str, float] = {}
        self._newest_seen: dict[str, str] = {}
        self._scanned_counts: dict[str, int] = {}
        # Wallets seen buying early in a winner, keyed to the winner mints
        # they appeared in — multi-winner wallets are scanned first.
        self._early_hits: dict[str, set[str]] = {}
        self._winners_done: dict[str, float] = {}
        self._counters = {"census_seen": 0, "census_promoted": 0,
                          "wallets_screened": 0, "bots_blocked": 0,
                          "too_thin": 0, "winners_mined": 0,
                          "smart_holders": 0, "histories_read": 0,
                          "admitted": 0, "rejected": 0}
        self._next_sweep_at = 0.0
        self.last_status: dict | None = None

    # -- live status -------------------------------------------------------

    def _status(self, phase: str, detail: str = "",
                budget: RpcBudget | None = None) -> None:
        """Narrate what discovery is doing right now to the dashboard.

        The latest payload is retained so a page loaded between sweeps
        still shows current state instead of an empty console.
        """
        import time
        config = self._store.config
        self.last_status = {
            "phase": phase,
            "detail": detail,
            "source": ("Solana Tracker PnL" if self._tracker
                       else "Winners' holders"),
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
        import time
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
            -len(self._early_hits.get(p.address, ())),
            -self._service_rank.get(p.address, 0.0), p.discovered_at))
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
        """The census is the primary enumerator: it nominates wallets that
        provably trade, and judgment is only ever our computed win rate.
        The Solana Tracker PnL leaderboard (optional, keyed, throttled to
        its free tier) accelerates the hunt when available; winners'
        holders is the on-chain feeder that needs nothing but RPC. Every
        service failure falls through — a missing key, a rate limit, or
        an outage costs breadth, never the sweep."""
        import time
        self._census_flow(config, budget)
        if not budget.exhausted and self._leaderboard_due(config):
            # Mark the attempt regardless of outcome: a rate-limited
            # service must not be retried every sweep.
            self._last_leaderboard_at = time.time()
            try:
                self._harvest_from_tracker(config, budget)
                return
            except Exception as exc:
                logger.warning("Solana Tracker leaderboard unavailable "
                               "(%s) — falling through", exc)
        if not budget.exhausted:
            self._harvest_from_winners(config, budget)

    def _leaderboard_due(self, config) -> bool:
        import time
        if self._tracker is None:
            return False
        return (time.time() - self._last_leaderboard_at
                >= config.discovery.leaderboard_interval_sec)

    def _harvest_from_tracker(self, config, budget: RpcBudget) -> None:
        """Solana Tracker's PnL leaderboard: wallets the service already
        measured across the whole chain. Nomination only — every wallet
        still passes the pre-screen and the full on-chain deep scan.

        The client pages the board and applies our activity ceiling from
        payload data (the API has no maximum-activity filter), so every
        entry arriving here is already copyable-cadence and cost no RPC.
        """
        entries = self._tracker.top_traders(
            window_days=config.discovery.skill_window_days,
            min_trades=config.filters.min_trades,
            min_active_days=config.discovery.leaderboard_min_active_days,
            sort=config.discovery.leaderboard_sort,
            max_trades_per_day=config.filters.max_trades_per_day,
            max_pages=config.discovery.leaderboard_pages)
        found = 0
        for position, entry in enumerate(entries):
            address = entry["address"]
            self._service_rank[address] = float(len(entries) - position)
            if self._registry.get(address) is not None:
                continue
            if not self._pre_screen(config, address, budget):
                continue
            if self._registry.add_candidate(address):
                found += 1
                logger.info("leaderboard candidate %s… (service win rate "
                            "%s)", address[:8], entry.get("win_rate"))
        if found:
            self._bus.publish("discovery_scan", {
                "source": "Solana Tracker PnL leaderboard",
                "new_candidates": found})

    # -- DEX census --------------------------------------------------------

    def _census_flow(self, config, budget: RpcBudget) -> None:
        """Observe live DEX flow and tally fee payers persistently; a
        wallet seen trading in multiple sweeps is promoted to a candidate
        and gets its full window measured."""
        d = config.discovery
        seen: set[str] = set()
        for program in d.census_programs:
            if not budget.take(1 + d.census_tx_sample):
                break
            self._status("census",
                         f"Census: observing DEX flow ({program[:6]}…)",
                         budget)
            try:
                signatures = self._provider.get_signatures(program, limit=25)
            except ChainError as exc:
                logger.warning("census read of %s failed: %s",
                               program[:8], exc)
                continue
            sampled = [s for s in signatures
                       if not s.get("err")][:d.census_tx_sample]
            for entry in sampled:
                try:
                    tx = self._provider.get_transaction(entry["signature"])
                except ChainError:
                    continue
                fee_payer = self._fee_payer(tx)
                if fee_payer:
                    seen.add(fee_payer)
        if seen:
            self._db.record_sightings(seen)
            self._counters["census_seen"] += len(seen)

        promoted = 0
        for wallet, count in self._db.frequent_sightings(
                d.census_min_sightings):
            if budget.exhausted:
                break
            if self._registry.get(wallet) is not None:
                continue
            if not self._pre_screen(config, wallet, budget):
                continue
            if self._registry.add_candidate(wallet):
                promoted += 1
                self._counters["census_promoted"] += 1
                logger.info("census promoted %s… (seen trading in %d "
                            "sweeps)", wallet[:8], count)
        if promoted:
            self._bus.publish("discovery_scan", {
                "source": "DEX census", "new_candidates": promoted})

    # -- winners' holders --------------------------------------------------

    def _harvest_from_winners(self, config, budget: RpcBudget) -> None:
        winners = self._find_winner_tokens(config)
        if not winners:
            logger.info("no qualifying winner tokens this sweep")
            return
        for winner in winners[:config.discovery.winners_per_scan]:
            if budget.exhausted:
                break
            try:
                self._harvest_winner_holders(config, winner, budget)
            except ChainError as exc:
                logger.warning("holder read of %s failed: %s",
                               winner["symbol"], exc)

    def _find_winner_tokens(self, config) -> list[dict]:
        """Tokens that ran hard enough over 24h to be worth mining for
        holders, from keyless trending stats with a DexScreener fallback."""
        import time
        d = config.discovery
        self._status("finding_winners",
                     "Hunting for tokens that made a hard 24h run")
        winners: list[dict] = []
        if self._jupiter is not None:
            try:
                winners = [
                    t for t in self._jupiter.top_tokens("24h", 60)
                    if t["liquidity_usd"] >= d.winner_min_liquidity_usd
                    and t["price_change_pct"] >= d.winner_min_price_change_pct]
                winners.sort(key=lambda w: -w["price_change_pct"])
            except Exception as exc:
                logger.warning("trending source failed (%s); trying "
                               "DexScreener search", exc)
        if not winners:
            winners = self._market_data.search_winners(
                d.winner_min_liquidity_usd, d.winner_min_price_change_pct)

        winners.sort(key=lambda w: -w["price_change_pct"])
        now = time.time()
        fresh = [w for w in winners
                 if now - self._winners_done.get(w["mint"], 0.0)
                 > WINNER_COOLDOWN_SEC]
        if fresh:
            logger.info("winner tokens this sweep: %s",
                        ", ".join(f"{w['symbol']} +{w['price_change_pct']:.0f}%"
                                  for w in fresh[:6]))
        return fresh

    def _harvest_winner_holders(self, config, winner: dict,
                                budget: RpcBudget) -> None:
        """Read the winner's top holders: real wallets holding size in a
        token that just ran bought early by construction."""
        import time
        d = config.discovery
        if not budget.take(2):
            return
        self._status("mining_holders",
                     f"Reading {winner['symbol']}'s top holders "
                     f"(+{winner['price_change_pct']:.0f}% in 24h)", budget)
        largest = self._provider.get_token_largest_accounts(
            winner["mint"])[:d.winner_top_holders]
        if not largest:
            self._winners_done[winner["mint"]] = time.time()
            return
        supply = self._provider.get_token_supply(winner["mint"])
        owners = self._provider.get_token_account_owners(
            [entry["address"] for entry in largest])

        self._counters["winners_mined"] += 1
        self._winners_done[winner["mint"]] = time.time()
        found = 0
        seen: set[str] = set()
        for entry, owner in zip(largest, owners):
            if not owner or owner in seen:
                continue
            seen.add(owner)
            share = (float(entry.get("uiAmount") or 0.0) / supply
                     if supply > 0 else 0.0)
            if share > d.winner_max_holder_share:
                continue  # Pool vault / locker / exchange omnibus.
            if not self._is_wallet(owner):
                continue  # Program-owned (off-curve): not a person.
            self._counters["smart_holders"] += 1
            self._early_hits.setdefault(owner, set()).add(winner["mint"])
            if self._registry.get(owner) is not None:
                continue
            if not self._pre_screen(config, owner, budget):
                continue
            if self._registry.add_candidate(owner):
                found += 1
                logger.info("top holder of %s: %s… (%.2f%% of supply)",
                            winner["symbol"], owner[:8], share * 100)
        if found:
            self._bus.publish("discovery_scan", {
                "source": f"{winner['symbol']} holders "
                          f"(+{winner['price_change_pct']:.0f}%)",
                "new_candidates": found})

    @staticmethod
    def _is_wallet(address: str) -> bool:
        """On-curve addresses are keypairs (people); PDAs are programs."""
        try:
            return Pubkey.from_string(address).is_on_curve()
        except Exception:
            return False

    def _pre_screen(self, config, address: str, budget: RpcBudget) -> bool:
        """One cheap signature call: reject machine-frequency wallets
        before any deep scanning is spent on them."""
        if config.dev_mode:
            return True  # Dev: bots and thin wallets are welcome.
        if not budget.take(1):
            return False
        self._status("screening", f"Screening wallet {address[:6]}…", budget)
        # One call, sized to the trade requirement: it answers both
        # "enough activity to ever qualify?" and "machine frequency?"
        # for the same price as the old 30-signature peek.
        probe = min(max(config.filters.min_trades, PRESCREEN_SIGNATURES),
                    PRESCREEN_MAX_FETCH)
        try:
            entries = self._provider.get_signatures(address, limit=probe)
        except ChainError as exc:
            logger.warning("pre-screen failed for %s: %s", address, exc)
            return False
        self._counters["wallets_screened"] += 1

        if len(entries) < config.filters.min_trades:
            # Cannot possibly hold min_trades swaps — reject before
            # spending a full history scan on an empty wallet.
            self._counters["too_thin"] += 1
            logger.info("pre-screen rejected %s…: only %d transactions "
                        "ever (needs %d trades)", address[:8], len(entries),
                        config.filters.min_trades)
            self._reject_at_door(
                address,
                f"only {len(entries)} transactions in its entire history — "
                f"cannot hold the {config.filters.min_trades} trades "
                "required")
            return False

        if len(entries) < PRESCREEN_SIGNATURES:
            return True  # Low activity: deep scan will judge it.
        newest = float(entries[0].get("blockTime") or 0.0)
        oldest = float(entries[-1].get("blockTime") or 0.0)
        span_days = max((newest - oldest) / 86_400.0, 1e-6)
        rate = len(entries) / span_days
        ceiling = (config.filters.max_trades_per_day
                   * PRESCREEN_RATE_MULTIPLIER)
        if rate > ceiling:
            logger.info("pre-screen rejected %s…: ~%.0f signatures/day "
                        "(ceiling %.0f) — machine-frequency wallet",
                        address[:8], rate, ceiling)
            self._counters["bots_blocked"] += 1
            self._reject_at_door(
                address, f"~{rate:,.0f} signatures/day — machine-frequency "
                         "wallet, not a trader")
            return False
        return True

    def _reject_at_door(self, address: str, reason: str) -> None:
        """Record a pre-screen rejection so the operator sees the funnel."""
        if self._registry.add_candidate(address):
            profile = self._registry.get(address)
            profile.status = TraderStatus.REJECTED
            profile.rejection_reason = reason
            self._registry.update(profile, event="trader_rejected")

    @staticmethod
    def _fee_payer(tx) -> str | None:
        if not tx:
            return None
        message = (tx.get("transaction") or {}).get("message") or {}
        for key in message.get("accountKeys") or []:
            if isinstance(key, dict) and key.get("signer"):
                return key.get("pubkey")
        return None

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
        import time
        scanned = self._scanned_counts.get(address, 0)
        trades = self._db.count_observed_trades(address)
        oldest = self._oldest_seen.get(address)
        depth_days = (time.time() - oldest) / 86_400.0 if oldest else 0.0
        logger.info("candidate %s… scanned %d signatures, %d swaps, "
                    "history depth %.0fd of %dd needed%s",
                    address[:8], scanned, trades, depth_days,
                    config.filters.min_history_days,
                    " (history exhausted)" if complete else "")
        self._bus.publish("candidate_progress", {
            "address": address,
            "signatures_scanned": scanned,
            "trades_found": trades,
            "depth_days": round(depth_days, 1),
            "target_days": config.filters.min_history_days,
            "signatures_target": config.discovery.signatures_per_trader,
            "complete": complete,
        })

    def _has_enough_depth(self, config, address: str) -> bool:
        import time
        if config.dev_mode:
            # Dev: one scanned batch is enough to judge and follow.
            return self._scanned_counts.get(address, 0) > 0
        if (self._scanned_counts.get(address, 0)
                >= config.discovery.signatures_per_trader):
            return True
        oldest = self._oldest_seen.get(address)
        if not oldest:
            return False
        covered_days = (time.time() - oldest) / 86_400.0
        required = max(config.filters.min_history_days,
                       config.discovery.skill_window_days)
        return covered_days >= required * 1.1

    def _finalize_candidate(self, config, address: str,
                            budget: RpcBudget) -> None:
        import time
        profile = self._registry.get(address)
        if profile is None or profile.status is not TraderStatus.CANDIDATE:
            return
        trades = self._db.load_observed_trades(address)
        # Skill is judged inside the window; the full record only proves
        # the wallet has been around and active long enough.
        cutoff = time.time() - config.discovery.skill_window_days * 86_400.0
        window = [t for t in trades if t.block_time >= cutoff]
        stats_full = self._scorer.compute_stats(address, trades)
        stats = self._scorer.compute_stats(
            address, window, stale_bag_days=config.filters.stale_bag_days)
        profile.stats = stats
        profile.score = self._scorer.score(stats)
        if config.dev_mode:
            # Dev: bypass every admission gate — anyone with observed
            # trades gets followed so paper activity flows.
            passed = bool(window)
            reason = "" if passed else "no trades observed in window"
        else:
            passed, reason = self._filter.evaluate(
                config.filters, stats, window,
                full_history_days=stats_full.history_days)

        if not passed:
            self._reject_scored(profile, reason)
        else:
            followed = self._registry.followed()
            if len(followed) < config.discovery.max_followed_traders:
                self._admit(profile, trades, budget)
            else:
                # Roster full: a measurably stronger candidate evicts the
                # weakest followed trader. The margin is hysteresis —
                # statistical noise must not churn the roster.
                worst = min(followed, key=lambda p: p.score)
                margin = config.discovery.replace_margin
                if profile.score > worst.score + margin:
                    self._retire_for_replacement(worst, profile)
                    self._admit(profile, trades, budget)
                else:
                    self._reject_scored(
                        profile,
                        f"roster full — score {profile.score:.3f} does "
                        f"not beat the weakest followed "
                        f"({worst.score:.3f} + {margin:.2f} margin)")
        self._counters["histories_read"] += 1

    def _admit(self, profile, trades, budget: RpcBudget) -> None:
        address = profile.address
        profile.status = TraderStatus.FOLLOWED
        profile.rejection_reason = ""
        profile.assigned_wallet_id = self._assign_wallet()
        # Arm the follow cursor at the trader's CURRENT newest
        # signature, not the one from when scanning began — otherwise
        # days-old qualification-era trades replay as live signals.
        follow_cursor = self._newest_seen.get(
            address, trades[-1].signature if trades else "")
        if budget.take(1):
            try:
                latest = self._provider.get_signatures(address, limit=1)
                if latest:
                    follow_cursor = latest[0]["signature"]
            except ChainError as exc:
                logger.warning("could not refresh follow cursor for "
                               "%s: %s", address, exc)
        self._registry.update(profile, follow_cursor=follow_cursor,
                              event="trader_admitted")
        self._counters["admitted"] += 1
        logger.info("admitted trader %s (score %.3f, win rate %.0f%%)",
                    address, profile.score,
                    (profile.stats.win_rate if profile.stats else 0) * 100)

    def _reject_scored(self, profile, reason: str) -> None:
        profile.status = TraderStatus.REJECTED
        profile.rejection_reason = reason or "did not meet the bar"
        self._registry.update(profile, event="trader_rejected")
        self._counters["rejected"] += 1
        logger.info("rejected trader %s: %s", profile.address[:8],
                    profile.rejection_reason)

    def _retire_for_replacement(self, worst, replacement) -> None:
        """Evict the weakest followed trader for a stronger find. Open
        positions stay with their wallet, protected by the panic stop,
        and close out on their own exits — mirroring manual unfollow."""
        worst.status = TraderStatus.RETIRED
        worst.rejection_reason = (
            f"replaced by {replacement.address[:6]}… "
            f"(score {replacement.score:.3f} vs {worst.score:.3f})")
        self._registry.update(worst, event="trader_retired")
        logger.info("retired trader %s: %s", worst.address[:8],
                    worst.rejection_reason)
