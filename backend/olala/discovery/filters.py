"""Trader admission filters.

A trader is followed only if every check passes. Filters never loosen
themselves to keep the system busy: failing traders are rejected with the
first failing reason recorded.
"""

from __future__ import annotations

import statistics
from collections import Counter

from ..chain.market_data import MarketDataService
from ..config import FilterConfig
from ..domain.models import ObservedTrade, TraderStats

TOKEN_SAMPLE_SIZE = 5


class TraderAdmissionFilter:
    def __init__(self, market_data: MarketDataService) -> None:
        self._market_data = market_data

    def evaluate(self, config: FilterConfig, stats: TraderStats,
                 trades: list[ObservedTrade],
                 full_history_days: float | None = None) -> tuple[bool, str]:
        """``stats``/``trades`` are windowed to the skill window; the
        history requirement judges the wallet's FULL record when given."""
        history_days = (full_history_days if full_history_days is not None
                        else stats.history_days)
        if history_days < config.min_history_days:
            return False, (f"history {history_days:.0f}d "
                           f"< {config.min_history_days}d")
        if stats.total_trades < config.min_trades:
            return False, f"trades {stats.total_trades} < {config.min_trades}"
        if stats.closed_round_trips < config.min_round_trips:
            return False, (f"only {stats.closed_round_trips} closed round "
                           f"trips — win rate not meaningful")
        if stats.adjusted_win_rate < config.min_win_rate:
            bags = (f" ({stats.open_bags} stale bags counted as losses)"
                    if stats.open_bags else "")
            return False, (f"adjusted win rate {stats.adjusted_win_rate:.0%} "
                           f"< {config.min_win_rate:.0%}{bags}")
        if stats.sharpe < config.min_sharpe:
            return False, (f"SHARP {stats.sharpe:.2f} < "
                           f"{config.min_sharpe:.2f} — returns too erratic")
        if stats.inactive_hours > config.max_inactive_hours:
            return False, (f"inactive {stats.inactive_hours:.0f}h "
                           f"> {config.max_inactive_hours}h")
        if stats.realized_pnl_sol <= 0:
            return False, "not net profitable"

        # Copyability: profitable is not enough — the style must be
        # followable at our latency. Bots fail here even when their PnL
        # sails through everything above.
        if stats.trades_per_day > config.max_trades_per_day:
            return False, (f"{stats.trades_per_day:.0f} trades/day reads "
                           f"as a bot (max {config.max_trades_per_day:.0f})")
        if stats.median_hold_minutes < config.min_median_hold_minutes:
            return False, (f"median hold {stats.median_hold_minutes:.1f}m "
                           f"< {config.min_median_hold_minutes:.0f}m — "
                           "uncopyable at our latency")

        quality_ok, reason, median_liquidity = self._token_quality(
            config, trades)
        stats.median_token_liquidity_usd = median_liquidity
        if not quality_ok:
            return False, reason
        return True, ""

    def _token_quality(self, config: FilterConfig,
                       trades: list[ObservedTrade]) -> tuple[bool, str, float]:
        """Do this trader's most-traded tokens meet the market-cap band?"""
        counts = Counter(t.mint for t in trades)
        top_mints = [mint for mint, _ in counts.most_common(TOKEN_SAMPLE_SIZE)]
        liquidity_values: list[float] = []
        market_caps: list[float] = []
        for mint in top_mints:
            info = self._market_data.get_token_info(mint)
            if info is None:
                continue
            liquidity_values.append(info.liquidity_usd)
            market_caps.append(info.market_cap_usd)
        if not liquidity_values:
            return False, "no market data for traded tokens", 0.0
        median_liquidity = statistics.median(liquidity_values)
        median_market_cap = statistics.median(market_caps)
        if median_liquidity < config.min_token_liquidity_usd:
            return (False, f"median token liquidity ${median_liquidity:,.0f} "
                    f"below ${config.min_token_liquidity_usd:,.0f}",
                    median_liquidity)
        if not (config.min_token_market_cap_usd <= median_market_cap
                <= config.max_token_market_cap_usd):
            return (False, f"median token mcap ${median_market_cap:,.0f} "
                    "outside allowed band", median_liquidity)
        return True, "", median_liquidity
