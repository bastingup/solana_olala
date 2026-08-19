"""STREAM A — the Solana Tracker PnL leaderboard.

The service has already done the work. It ranks wallets across the whole
chain and gates them server-side on trade count, active trading days,
ROI, win rate and non-arbitrage, using its own complete index. We take
its output as given and follow the names it returns.

Deliberately, **none of the ``filters`` section applies here**. Those
thresholds exist for :mod:`~olala.discovery.onchain`, where nobody has
vetted anything and we must do the work ourselves. Re-deriving the
service's judgment cost thousands of RPC calls per candidate and reached
a *worse* verdict — our window is narrower and our reconstructor cannot
read multi-hop swaps — so it dropped wallets the service had qualified.

The one limit kept is ``leaderboard.max_trades_per_day``, and it is not
a quality judgment: it is the speed past which we can neither copy a
trader nor afford the RPC to try. It is applied inside the client from
payload data, so it costs nothing.

Risk is untouched by trusting the service: token safety, position
sizing, and the ATR panic stop still gate every trade these traders
produce.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..domain.models import TraderStats, TraderStatus
from ..events import EventBus
from ..services.traders import TraderRegistry
from .roster import Roster

logger = logging.getLogger(__name__)

SOURCE_NAME = "Solana Tracker PnL leaderboard"


class LeaderboardSource:
    """Fetches the board on an interval and seats what it returns."""

    def __init__(self, tracker, registry: TraderRegistry, bus: EventBus,
                 roster: Roster, counters: dict[str, int]) -> None:
        self._tracker = tracker
        self._registry = registry
        self._bus = bus
        self._roster = roster
        self._counters = counters
        self._last_poll_at = 0.0
        # Board position per nominee (higher = better), so the on-chain
        # deep-scan queue can still prioritise service-known names.
        self.rank: dict[str, float] = {}

    @property
    def available(self) -> bool:
        return self._tracker is not None

    def due(self, config) -> bool:
        """Throttled so a free API tier lasts the month. Failures also
        start the window, so a rate-limited service is never hammered.

        The stream is live whenever a key is configured; clearing
        ``chain.solana_tracker_api_key`` is how it gets turned off.
        """
        if not self.available:
            return False
        return (time.time() - self._last_poll_at
                >= config.filters_solanatracker.interval_sec)

    def harvest(self, config) -> int:
        """Poll the board and follow its names. Returns seats taken.

        Raises whatever the client raises so the caller can fall through
        to on-chain discovery; the poll window is marked either way.
        """
        self._last_poll_at = time.time()
        board = config.filters_solanatracker
        entries = self._tracker.top_traders(
            window_days=board.window_days,
            min_trades=board.min_trades,
            min_active_days=board.min_active_days,
            sort=board.sort,
            max_trades_per_day=(board.max_trades_per_day
                                if board.max_trades_per_day > 0 else None),
            max_pages=board.pages,
            min_roi_pct=board.min_roi_pct,
            min_win_rate_pct=board.min_win_rate_pct)

        followed = 0
        for position, entry in enumerate(entries):
            address = entry["address"]
            self.rank[address] = float(len(entries) - position)
            if self._registry.get(address) is not None:
                continue
            if self._follow(config, entry, position, len(entries)):
                followed += 1
        if followed:
            self._bus.publish("discovery_scan", {
                "source": SOURCE_NAME, "new_candidates": followed})
        logger.info("leaderboard: %d names returned (sort=%s), %d followed",
                    len(entries), board.sort, followed)
        return followed

    # -- internals ---------------------------------------------------------

    def _follow(self, config, entry: dict, position: int,
                total: int) -> bool:
        address = entry["address"]
        score = self._score(entry, position, total)
        # Register first: claim_seat EVICTS an incumbent when the roster
        # is full, so it must never run before we know the seat can
        # actually be filled.
        if not self._registry.add_candidate(address):
            return False
        profile = self._registry.get(address)
        if not self._roster.claim_seat(config, address, score):
            self._roster.reject(
                profile, "roster full — did not beat the weakest seat")
            return False
        # No follow cursor: FollowDaemon arms it at the trader's newest
        # signature on first contact, so no history replays as signals.
        self._roster.follow(profile, score, stats=self._stats(entry))
        logger.info("followed %s… on service vetting (roi rank %d/%d, "
                    "%s trades, %.1f trades/day)", address[:8], position + 1,
                    total, entry.get("trade_count"),
                    entry.get("trades_per_day") or 0.0)
        return True

    @staticmethod
    def _score(entry: dict, position: int, total: int) -> float:
        """Rank the service handed us, normalised to 0..1.

        Board POSITION is the score, not win rate: position reflects
        whatever ranking the operator configured (ROI by default), while
        win rate is a single distorted field — a wallet that never sells
        its losers reports ~100%.
        """
        if total <= 0:
            return 0.0
        return round((total - position) / total, 4)

    @staticmethod
    def _stats(entry: dict) -> TraderStats:
        """Shape the service's numbers into TraderStats for display.

        These are the SERVICE's measurements, not ours; a later on-chain
        scan would overwrite them.
        """
        trades = int(entry.get("trade_count") or 0)
        win_rate = entry.get("win_rate") or 0.0
        round_trips = max(trades // 2, 0)
        return TraderStats(
            address=entry["address"],
            total_trades=trades,
            closed_round_trips=round_trips,
            wins=int(round(round_trips * win_rate)),
            last_trade_at=time.time(),
        )
