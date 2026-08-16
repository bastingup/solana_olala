"""Trader performance scoring.

Round trips are reconstructed FIFO per token: every sell is matched against
accumulated buy cost basis; a sell with positive realized PnL is a win.
Success rate is the dominant scoring signal, per product definition.
Holding durations fall out of the same matching (buy time → sell time,
quantity-weighted) and feed the copyability gate: a wallet whose median
hold is measured in seconds is a bot we cannot follow.
"""

from __future__ import annotations

import statistics
from collections import defaultdict, deque

from ..domain.models import ObservedTrade, TraderStats, TradeSide


STALE_BAG_MIN_COST_SOL = 0.05


class TraderScorer:
    def compute_stats(self, address: str, trades: list[ObservedTrade],
                      stale_bag_days: float = 7.0,
                      now: float | None = None) -> TraderStats:
        import time
        now = now if now is not None else time.time()
        stats = TraderStats(address=address)
        if not trades:
            return stats

        ordered = sorted(trades, key=lambda t: t.block_time)
        stats.first_trade_at = ordered[0].block_time
        stats.last_trade_at = ordered[-1].block_time
        stats.total_trades = len(ordered)
        stats.distinct_tokens = len({t.mint for t in ordered})

        # FIFO lots per mint: deque of [quantity, price, buy_time].
        lots: dict[str, deque] = defaultdict(deque)
        holds_minutes: list[float] = []
        trade_returns: list[float] = []
        for trade in ordered:
            if trade.side is TradeSide.BUY:
                lots[trade.mint].append(
                    [trade.token_amount, trade.price_sol, trade.block_time])
                continue
            remaining = trade.token_amount
            cost = 0.0
            matched = 0.0
            held_seconds_weighted = 0.0
            queue = lots[trade.mint]
            while remaining > 1e-9 and queue:
                lot = queue[0]
                take = min(lot[0], remaining)
                cost += take * lot[1]
                held_seconds_weighted += take * max(
                    trade.block_time - lot[2], 0.0)
                matched += take
                lot[0] -= take
                remaining -= take
                if lot[0] <= 1e-9:
                    queue.popleft()
            if matched <= 1e-9:
                # Sell without an observed buy (position predates our
                # history window) — not scoreable.
                continue
            proceeds = matched * trade.price_sol
            pnl = proceeds - cost
            stats.closed_round_trips += 1
            stats.realized_pnl_sol += pnl
            holds_minutes.append(held_seconds_weighted / matched / 60.0)
            if cost > 1e-9:
                trade_returns.append(pnl / cost)
            if pnl > 0:
                stats.wins += 1
        if holds_minutes:
            stats.median_hold_minutes = statistics.median(holds_minutes)

        # Stale bags: unsold inventory whose lots have aged past the
        # threshold — losers never realized. Counted per mint.
        stale_cutoff = now - stale_bag_days * 86_400.0
        for mint, queue in lots.items():
            remaining_cost = sum(lot[0] * lot[1] for lot in queue
                                 if lot[0] > 1e-9)
            if remaining_cost < STALE_BAG_MIN_COST_SOL:
                continue
            oldest_lot_time = min((lot[2] for lot in queue
                                   if lot[0] > 1e-9), default=now)
            if oldest_lot_time <= stale_cutoff:
                stats.open_bags += 1
                stats.bag_cost_sol += remaining_cost

        # Per-trade Sharpe: consistency of returns, not size of the
        # luckiest one. Zero spread means perfectly uniform returns —
        # the best (or worst) possible consistency, capped at ±10.
        if len(trade_returns) >= 5:
            mean = statistics.mean(trade_returns)
            spread = statistics.pstdev(trade_returns)
            if spread > 1e-9:
                stats.sharpe = max(-10.0, min(10.0, mean / spread))
            elif mean > 0:
                stats.sharpe = 10.0
            elif mean < 0:
                stats.sharpe = -10.0
        return stats

    def score(self, stats: TraderStats) -> float:
        """0..1 composite; ADJUSTED win rate IS the metric (operator
        decision) — stale bags already count as losses inside it, and the
        small side components only break ties between equal rates."""
        history_component = min(stats.history_days / 180.0, 1.0)
        volume_component = min(stats.closed_round_trips / 300.0, 1.0)
        return (0.8 * stats.adjusted_win_rate
                + 0.1 * history_component
                + 0.1 * volume_component)
