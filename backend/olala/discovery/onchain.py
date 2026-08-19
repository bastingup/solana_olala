"""STREAM B — our own on-chain discovery.

**Winners' holders**: wallets holding size in a token that just ran,
who therefore bought early by construction. Nobody has vetted them.

Because nobody has done the work for us, this stream does it: a cheap
signature pre-screen here, then the daemon's full history scan and the
``filters_onchain`` admission gate. That is the deliberate asymmetry
with :mod:`~olala.discovery.leaderboard`, where a service has already
measured and ranked the wallet — ``filters_onchain`` governs THIS
stream.

A DEX census (sampling live Jupiter/Raydium/Orca flow for fee payers)
used to run alongside this. It was dropped: it cost a fixed slice of
every scan budget to enumerate wallets that are merely ACTIVE, and
activity is the one thing a copy trader can find anywhere. Winners'
holders selects on having been early into something that worked, which
is the property actually worth scanning.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from solders.pubkey import Pubkey

from ..chain.market_data import MarketDataService
from ..chain.provider import ChainError, RpcProvider
from ..constants import SECONDS_PER_DAY
from ..domain.models import TraderStatus
from ..events import EventBus
from ..persistence.database import Database
from ..services.traders import TraderRegistry
from .budget import RpcBudget

logger = logging.getLogger(__name__)

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
# The rate probe always fetches deep. getSignaturesForAddress returns up
# to 1,000 entries for the SAME single credit, and a shallow sample
# measures a wallet's most recent BURST instead of its sustained rate:
# 30 trades in half an hour (an ordinary session) reads as 1,300/day.
# Measured 2026-08-18 on a real nominee: 1,284/day at 30 signatures vs
# 42/day at 500 — a 30x overestimate that rejected a genuine trader.


class OnChainSource:
    """Winners' holders, with the pre-screen gate."""

    def __init__(self, provider: RpcProvider,
                 market_data: MarketDataService, registry: TraderRegistry,
                 db: Database, bus: EventBus, counters: dict[str, int],
                 report: Callable[..., None], jupiter=None) -> None:
        self._provider = provider
        self._market_data = market_data
        self._registry = registry
        self._db = db
        self._bus = bus
        self._counters = counters
        self._report = report
        self._jupiter = jupiter
        # Wallets seen buying early in a winner, keyed to the winner
        # mints they appeared in — multi-winner wallets are scanned first.
        self.early_hits: dict[str, set[str]] = {}
        self._winners_done: dict[str, float] = {}

    def harvest(self, config, budget) -> None:
        if not budget.exhausted:
            self._harvest_from_winners(config, budget)

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
        d = config.discovery
        self._report("finding_winners",
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
        d = config.discovery
        if not budget.take(2):
            return
        self._report("mining_holders",
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
            self.early_hits.setdefault(owner, set()).add(winner["mint"])
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
        if not config.dev_mode:
            # Filters off: bots and thin wallets are welcome through.
            return True
        if not budget.take(1):
            return False
        self._report("screening", f"Screening wallet {address[:6]}…", budget)
        # Always probe deep — same one credit, far better measurement.
        probe = PRESCREEN_MAX_FETCH
        try:
            entries = self._provider.get_signatures(address, limit=probe)
        except ChainError as exc:
            logger.warning("pre-screen failed for %s: %s", address, exc)
            return False
        self._counters["wallets_screened"] += 1

        if len(entries) < config.filters_onchain.min_trades:
            # Cannot possibly hold min_trades swaps — reject before
            # spending a full history scan on an empty wallet.
            self._counters["too_thin"] += 1
            logger.info("pre-screen rejected %s…: only %d transactions "
                        "ever (needs %d trades)", address[:8], len(entries),
                        config.filters_onchain.min_trades)
            self._reject_at_door(
                address,
                f"only {len(entries)} transactions in its entire history — "
                f"cannot hold the {config.filters_onchain.min_trades} trades "
                "required")
            return False

        if len(entries) < PRESCREEN_SIGNATURES:
            return True  # Low activity: deep scan will judge it.
        newest = float(entries[0].get("blockTime") or 0.0)
        oldest = float(entries[-1].get("blockTime") or 0.0)
        span_days = max((newest - oldest) / SECONDS_PER_DAY, 1e-6)
        rate = len(entries) / span_days
        ceiling = (config.filters_onchain.max_trades_per_day
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

