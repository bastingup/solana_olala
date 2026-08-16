from olala.discovery.scoring import TraderScorer
from olala.domain.models import ObservedTrade, TradeSide


def trade(side, amount, price, t, mint="m1"):
    return ObservedTrade(
        trader="t1", signature=f"s{t}", side=side, mint=mint,
        token_amount=amount, sol_amount=amount * price, price_sol=price,
        block_time=1_700_000_000 + t)


def test_fifo_round_trips_and_wins():
    trades = [
        trade(TradeSide.BUY, 100, 0.010, 1),
        trade(TradeSide.BUY, 100, 0.020, 2),
        # Sells 150: matches 100 @0.010 + 50 @0.020 = 2.0 cost,
        # proceeds 150*0.02=3.0 -> win
        trade(TradeSide.SELL, 150, 0.020, 3),
        # Sells 50 @0.005: cost 50*0.020=1.0, proceeds 0.25 -> loss
        trade(TradeSide.SELL, 50, 0.005, 4),
    ]
    stats = TraderScorer().compute_stats("t1", trades)
    assert stats.total_trades == 4
    assert stats.closed_round_trips == 2
    assert stats.wins == 1
    assert abs(stats.realized_pnl_sol - (1.0 - 0.75)) < 1e-9
    assert stats.win_rate == 0.5


def test_sell_without_observed_buy_not_scored():
    stats = TraderScorer().compute_stats(
        "t1", [trade(TradeSide.SELL, 100, 0.01, 1)])
    assert stats.closed_round_trips == 0
    assert stats.total_trades == 1


def test_distinct_tokens_counted():
    trades = [trade(TradeSide.BUY, 1, 0.01, i, mint=f"m{i % 3}")
              for i in range(6)]
    stats = TraderScorer().compute_stats("t1", trades)
    assert stats.distinct_tokens == 3


def test_score_bounded_and_win_rate_dominant():
    scorer = TraderScorer()
    from olala.domain.models import TraderStats
    weak = TraderStats(address="a", closed_round_trips=300, wins=150,
                       first_trade_at=0, last_trade_at=200 * 86_400)
    strong = TraderStats(address="b", closed_round_trips=300, wins=270,
                         first_trade_at=0, last_trade_at=200 * 86_400)
    assert 0.0 <= scorer.score(weak) <= 1.0
    assert scorer.score(strong) > scorer.score(weak)


def test_empty_history_scores_zero():
    stats = TraderScorer().compute_stats("t1", [])
    assert stats.total_trades == 0
    assert TraderScorer().score(stats) == 0.0
