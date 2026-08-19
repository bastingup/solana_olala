import time

from olala.config import OnChainFilters
from olala.discovery.filters import TraderAdmissionFilter
from olala.domain.models import ObservedTrade, TraderStats, TradeSide

from conftest import make_token
from fakes import FakeMarketData


def good_stats(**overrides):
    now = time.time()
    values = dict(address="t1", first_trade_at=now - 120 * 86_400,
                  last_trade_at=now - 3_600, total_trades=300,
                  closed_round_trips=90, wins=60, realized_pnl_sol=50.0,
                  distinct_tokens=5, median_hold_minutes=95.0, sharpe=1.2)
    values.update(overrides)
    return TraderStats(**values)


def trades_for(mint="m1", count=3):
    return [ObservedTrade(trader="t1", signature=f"s{i}", side=TradeSide.BUY,
                          mint=mint, token_amount=1, sol_amount=0.1,
                          price_sol=0.1, block_time=i)
            for i in range(count)]


def evaluate(stats, market=None, trades=None):
    market = market or FakeMarketData({"m1": make_token(mint="m1")})
    filt = TraderAdmissionFilter(market)
    return filt.evaluate(OnChainFilters(), stats, trades or trades_for())


def test_good_trader_passes():
    passed, reason = evaluate(good_stats())
    assert passed, reason


def test_each_threshold_rejects():
    now = time.time()
    cases = {
        "history": good_stats(first_trade_at=now - 10 * 86_400),
        "trades": good_stats(total_trades=50),
        "round trips": good_stats(closed_round_trips=5, wins=4),
        "win rate": good_stats(wins=40),
        "inactive": good_stats(last_trade_at=now - 5 * 86_400),
        "unprofitable": good_stats(realized_pnl_sol=-1.0),
        "bot frequency": good_stats(total_trades=20_000),
        "scalper holds": good_stats(median_hold_minutes=0.2),
    }
    for name, stats in cases.items():
        passed, reason = evaluate(stats)
        assert not passed, f"{name} should have rejected"
        assert reason


def test_token_quality_rejects_thin_liquidity():
    market = FakeMarketData(
        {"m1": make_token(mint="m1", liquidity_usd=10_000.0)})
    passed, reason = evaluate(good_stats(), market=market)
    assert not passed
    assert "liquidity" in reason


def test_token_quality_rejects_microcap():
    market = FakeMarketData(
        {"m1": make_token(mint="m1", market_cap_usd=500_000.0)})
    passed, reason = evaluate(good_stats(), market=market)
    assert not passed
    assert "mcap" in reason


def test_no_market_data_rejects():
    passed, reason = evaluate(good_stats(), market=FakeMarketData({}))
    assert not passed
    assert "market data" in reason
