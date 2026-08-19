"""Win-rate-first skill measurement: windowing, stale bags, SHARP, and
the DEX census enumerator."""

import time

import pytest

from olala.config import OnChainFilters
from olala.discovery.scanner import RpcBudget
from olala.discovery.scoring import TraderScorer
from olala.domain.models import ObservedTrade, TraderStatus, TradeSide

from fakes import FakeJupiterTokens
from test_discovery_v2 import human_signatures, make_daemon

NOW = time.time()


def trade(side, amount, price, ago_days, mint="m1"):
    at = NOW - ago_days * 86_400
    return ObservedTrade(
        trader="t1", signature=f"s{ago_days}-{mint}-{side.value}",
        side=side, mint=mint, token_amount=amount,
        sol_amount=amount * price, price_sol=price, block_time=at)


def round_trip(ago_days, win=True, mint=None):
    mint = mint or f"m{ago_days}"
    buy_price, sell_price = 0.01, (0.012 if win else 0.008)
    # ~72-minute holds: comfortably inside the copyability gate.
    return [trade(TradeSide.BUY, 100, buy_price, ago_days, mint),
            trade(TradeSide.SELL, 100, sell_price, ago_days - 0.05, mint)]


# -- stale bags ------------------------------------------------------------

def test_stale_bags_count_as_losses():
    trades = []
    for i in range(10):  # 10 clean wins
        trades.extend(round_trip(30 + i, win=True))
    for i in range(10):  # 10 unsold bags bought 20 days ago
        trades.append(trade(TradeSide.BUY, 100, 0.01, 20, mint=f"bag{i}"))
    stats = TraderScorer().compute_stats("t1", trades, now=NOW)
    assert stats.win_rate == 1.0          # raw rate looks perfect
    assert stats.open_bags == 10          # ...but the bags are counted
    assert stats.adjusted_win_rate == 0.5  # 10 wins / (10 + 10 bags)
    assert stats.bag_cost_sol == pytest.approx(10.0)


def test_fresh_open_positions_are_not_bags():
    trades = [t for i in range(5) for t in round_trip(30 + i, win=True)]
    trades.append(trade(TradeSide.BUY, 100, 0.01, 2, mint="fresh"))
    stats = TraderScorer().compute_stats("t1", trades, stale_bag_days=7.0,
                                         now=NOW)
    assert stats.open_bags == 0
    assert stats.adjusted_win_rate == 1.0


def test_dust_leftovers_are_not_bags():
    trades = [trade(TradeSide.BUY, 100, 0.0001, 20, mint="dusty")]
    stats = TraderScorer().compute_stats("t1", trades, now=NOW)
    assert stats.open_bags == 0  # 0.01 SOL leftover is rounding, not a bag


# -- SHARP -----------------------------------------------------------------

def test_sharpe_rewards_consistency():
    steady = [t for i in range(10) for t in round_trip(30 + i, win=True)]
    lumpy = []
    for i in range(9):
        lumpy.extend(round_trip(30 + i, win=False))
    lumpy.append(trade(TradeSide.BUY, 100, 0.01, 50, mint="moon"))
    lumpy.append(trade(TradeSide.SELL, 100, 0.10, 49.9, mint="moon"))
    steady_stats = TraderScorer().compute_stats("t1", steady, now=NOW)
    lumpy_stats = TraderScorer().compute_stats("t1", lumpy, now=NOW)
    assert steady_stats.sharpe > lumpy_stats.sharpe


def test_sharpe_needs_enough_trades():
    stats = TraderScorer().compute_stats(
        "t1", [t for t in round_trip(30)], now=NOW)
    assert stats.sharpe == 0.0


# -- windowed judgment -----------------------------------------------------

def test_filter_judges_skill_inside_window_history_outside():
    """An old wallet whose recent window is strong passes even though the
    windowed history span is shorter than min_history_days."""
    config = OnChainFilters(min_history_days=90, min_trades=10,
                          min_win_rate=0.6, min_sharpe=0.0)
    window_trades = [t for i in range(25)
                     for t in round_trip(0.02 + i * 1.5, win=(i % 4 != 0))]
    from olala.discovery.filters import TraderAdmissionFilter
    from conftest import make_token
    from fakes import FakeMarketData
    scorer = TraderScorer()
    stats = scorer.compute_stats("t1", window_trades, now=NOW)
    assert stats.closed_round_trips >= config.min_round_trips
    market = FakeMarketData(
        {t.mint: make_token(mint=t.mint) for t in window_trades})
    # Windowed span is ~37 days — passes only because the full record
    # says 120 days.
    passed, reason = TraderAdmissionFilter(market).evaluate(
        config, stats, window_trades, full_history_days=120.0)
    assert passed, reason
    failed, reason = TraderAdmissionFilter(market).evaluate(
        config, stats, window_trades, full_history_days=40.0)
    assert not failed
    assert "history" in reason


def test_erratic_returns_rejected_by_sharpe_gate():
    config = OnChainFilters(min_trades=10, min_win_rate=0.3, min_sharpe=0.5)
    from olala.discovery.filters import TraderAdmissionFilter
    from conftest import make_token
    from fakes import FakeMarketData
    lumpy = []
    for i in range(20):
        lumpy.extend(round_trip(20 + i, win=(i % 2 == 0)))
    stats = TraderScorer().compute_stats("t1", lumpy, now=NOW)
    stats.first_trade_at = NOW - 100 * 86_400  # satisfy history/activity
    stats.last_trade_at = NOW - 60
    market = FakeMarketData(
        {t.mint: make_token(mint=t.mint) for t in lumpy})
    passed, reason = TraderAdmissionFilter(market).evaluate(
        config, stats, lumpy)
    assert not passed
    assert "SHARP" in reason


# -- DEX census ------------------------------------------------------------

def stage_program_flow(provider, program, sig_prefix, trader, tx):
    provider.signatures[program] = [
        {"signature": f"{sig_prefix}-1", "err": None, "blockTime": NOW}]
    provider.transactions[f"{sig_prefix}-1"] = tx


def test_census_promotes_repeat_traders_only(db, bus, config_store):
    from fakes import make_swap_tx
    from test_discovery_v2 import ELITE
    provider, registry, daemon = make_daemon(
        db, bus, config_store, jupiter=FakeJupiterTokens([]))
    programs = config_store.config.discovery.census_programs
    tx = make_swap_tx(ELITE, -2_000_005_000, "MintX111", 100.0)
    stage_program_flow(provider, programs[0], "c1", ELITE, tx)
    provider.signatures[ELITE] = human_signatures()

    daemon.onchain._census_flow(config_store.config, RpcBudget(60))
    # One sighting: tallied but not promoted.
    assert registry.get(ELITE) is None
    assert db.frequent_sightings(1) == [(ELITE, 1)]

    daemon.onchain._census_flow(config_store.config, RpcBudget(60))
    # Second sighting crosses census_min_sightings=2: promoted.
    profile = registry.get(ELITE)
    assert profile is not None
    assert profile.status is TraderStatus.CANDIDATE
    assert daemon._counters["census_promoted"] == 1


def test_census_sightings_survive_restart(db, bus, config_store):
    db.record_sightings({"WalletA", "WalletB"})
    db.record_sightings({"WalletA"})
    assert db.frequent_sightings(2) == [("WalletA", 2)]
    assert ("WalletB", 1) in db.frequent_sightings(1)
