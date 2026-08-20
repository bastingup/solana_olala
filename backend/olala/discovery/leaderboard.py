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

from ..domain.models import TraderStats, TraderStatus
from ..events import EventBus
from ..services.traders import TraderRegistry
from .roster import Roster

logger = logging.getLogger(__name__)

SOURCE_NAME = "Solana Tracker PnL leaderboard"

# Sweeps a seated trader may be missing from the qualified board before
# losing its seat. One absence is as likely to be a service hiccup as a
# real change; acting on it would churn the roster on every bad minute.
ABSENCES_BEFORE_RETIREMENT = 3


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
        # Consecutive sweeps a followed trader has been absent from the
        # qualified board.
        self._absences: dict[str, int] = {}
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
            limit=board.limit,
            min_trades=board.min_trades,
            min_active_days=board.min_active_days,
            sort=board.sort,
            max_trades_per_day=(board.max_trades_per_day
                                if board.max_trades_per_day > 0 else None),
            max_pages=board.pages,
            page_size=board.page_size,
            min_roi_pct=board.min_roi_pct,
            min_win_rate=board.min_win_rate,
            min_avg_buy_usd=board.min_avg_buy_usd,
            max_last_trade_age_sec=board.max_last_trade_hours * 3600.0,
            min_volume_usd=board.min_volume_usd,
            require_closed_trades=board.require_closed_trades)

        total = len(entries)

        # PASS ONE — score everything against today's board, and refresh
        # every wallet we already know. This must finish before any seat
        # is contested: comparing a newcomer against an incumbent's
        # STALE score let board order decide the outcome, so a trader
        # that had slipped could keep its seat purely by being scored
        # first. It is also the whole of "has anything changed about our
        # traders of interest?" — free, because the sweep already
        # carries their current numbers.
        scored: list[tuple[dict, float, int]] = []
        for position, entry in enumerate(entries):
            score = self._score(entry, position, total)
            self.rank[entry["address"]] = float(total - position)
            scored.append((entry, score, position))
            profile = self._registry.get(entry["address"])
            if profile is not None:
                self._refresh(profile, entry, score)

        # PASS TWO — allocate seats, best first.
        followed = 0
        watchlisted = 0
        for entry, score, position in scored:
            profile = self._registry.get(entry["address"])
            if profile is not None and profile.status is TraderStatus.FOLLOWED:
                continue
            if profile is None:
                seated = self._follow(config, entry, score, position, total)
            else:
                # The watchlist. Skipping known addresses outright, as
                # this used to, meant a wallet passed over once for a
                # full roster could never be reconsidered however much
                # it improved — the pool could only ever shrink.
                seated = self._reconsider(config, profile, entry, score)
            if seated:
                followed += 1
            else:
                watchlisted += 1

        retired = self._retire_absent(config, {e["address"] for e in entries})

        if followed:
            self._bus.publish("discovery_scan", {
                "source": SOURCE_NAME, "new_candidates": followed})
        logger.info("leaderboard: %d names qualified (sort=%s), %d seated, "
                    "%d held on the watchlist, %d retired for falling off",
                    total, board.sort, followed, watchlisted, retired)
        return followed

    def _retire_absent(self, config, qualified: set[str]) -> int:
        """Free seats held by traders that no longer qualify.

        Only wallets ON the board get re-scored, so a seated trader that
        drops off it — went dormant, slowed down, stopped clearing the
        volume bar — would otherwise keep its seat and its admission-day
        score indefinitely, copying nothing. Over a multi-day run that
        silently converts the roster into a museum.

        Absence is counted over consecutive sweeps rather than acted on
        at once: one missing sweep is as likely to be a service hiccup
        as a real change, and evicting on it would churn the roster
        every time the API had a bad minute.
        """
        retired = 0
        for profile in self._roster.followed():
            address = profile.address
            if address in qualified:
                self._absences.pop(address, None)
                continue
            missed = self._absences.get(address, 0) + 1
            self._absences[address] = missed
            if missed < ABSENCES_BEFORE_RETIREMENT:
                continue
            profile.status = TraderStatus.RETIRED
            profile.rejection_reason = (
                f"no longer on the qualified board ({missed} sweeps)")
            self._registry.update(profile, event="trader_retired")
            self._absences.pop(address, None)
            retired += 1
            logger.info("retired %s…: off the qualified board for %d "
                        "sweeps — the seat goes to someone trading",
                        address[:8], missed)
        return retired

    def _refresh(self, profile, entry: dict, score: float) -> None:
        """Bring a known trader's score and stats up to date."""
        if score == profile.score:
            return
        previous = profile.score
        profile.score = score
        profile.stats = self._stats(entry)
        self._registry.update(profile)
        if score < previous - 0.1:
            logger.info("followed %s… slipped on the board (%.3f -> %.3f); "
                        "a stronger name can now take the seat",
                        profile.address[:8], previous, score)

    def _reconsider(self, config, profile, entry: dict,
                    score: float) -> bool:
        """Compete for a seat with a wallet we already know.

        These are the traders of interest: they qualified again today,
        so they contest a seat on the same terms as a new name.
        """
        if not self._roster.claim_seat(config, profile.address, score):
            # No seat today. It stays a CANDIDATE — on the watchlist,
            # with fresh numbers — rather than being written off.
            profile.status = TraderStatus.CANDIDATE
            profile.rejection_reason = ""
            self._registry.update(profile)
            return False
        self._roster.follow(profile, score, stats=self._stats(entry))
        logger.info("re-seated %s… from the watchlist (score %.3f)",
                    profile.address[:8], score)
        return True

    # -- internals ---------------------------------------------------------

    def _follow(self, config, entry: dict, score: float, position: int,
                total: int) -> bool:
        address = entry["address"]
        # Register first: claim_seat EVICTS an incumbent when the roster
        # is full, so it must never run before we know the seat can
        # actually be filled.
        if not self._registry.add_candidate(address):
            return False
        profile = self._registry.get(address)
        if not self._roster.claim_seat(config, address, score):
            # Not a rejection: it qualified, there was simply no seat.
            # It stays a CANDIDATE so the next sweep reconsiders it.
            profile.score = score
            profile.stats = self._stats(entry)
            self._registry.update(profile)
            return False
        # No follow cursor: WalletTracker arms it at the trader's newest
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
