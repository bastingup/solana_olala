"""Measured performance: the second layer of the hierarchy.

Closed positions are never deleted — they become each trader's realized,
fee-inclusive track record IN OUR SETUP. That record colours the moons and
decides which traders sit on the live wallets: the top proven performers
take the premium (live) seats, the unproven start on paper, and rebalancing
only ever moves a FLAT trader so real money is never liquidated to reshuffle.
"""

import pytest
from solders.keypair import Keypair

from olala.api.server import AppContext
from olala.domain.models import (ExitReason, Fill, TradeSide, TraderProfile,
                                 TraderStatus, MIN_CLOSED_FOR_RANK)
from olala.persistence.database import Database
from olala.security.keystore import EncryptedKeystore
from olala.trading.portfolio import PortfolioManager

from conftest import make_token
from fakes import FakeMarketData, FakeProvider

A = "TraderAAAA1111111111111111111111111111111111"
B = "TraderBBBB2222222222222222222222222222222222"
C = "TraderCCCC3333333333333333333333333333333333"


def close_cycle(portfolio, wallet, trader, token, invested, proceeds):
    """Open a position for `trader` and close it, realizing proceeds - invested."""
    buy = Fill(order_id="b", side=TradeSide.BUY, mint=token.mint,
               quantity=1000.0, price_sol=0.001, sol_amount=invested,
               fee_sol=0.0001)
    position = portfolio.apply_buy(wallet, trader, token, buy)
    sell = Fill(order_id="s", side=TradeSide.SELL, mint=token.mint,
                quantity=position.quantity, price_sol=0.001,
                sol_amount=proceeds, fee_sol=0.0001)
    portfolio.apply_close(wallet, position, sell, ExitReason.TRADER_EXIT)


# -- the measured aggregate ------------------------------------------------

@pytest.fixture
def portfolio(db, bus, config_store):
    return PortfolioManager(db, bus, config_store, FakeProvider())


def test_closed_positions_aggregate_into_a_trader_record(portfolio, token):
    wallet = portfolio.wallets()[0]
    close_cycle(portfolio, wallet, A, token, 1.0, 1.5)     # +0.5
    close_cycle(portfolio, wallet, A, token, 1.0, 0.8)     # -0.2
    close_cycle(portfolio, wallet, A, token, 1.0, 2.0)     # +1.0

    perf = portfolio.trader_performance()[A]
    assert perf.realized_pnl_sol == pytest.approx(1.3)
    assert perf.closed_count == 3
    assert perf.wins == 2
    assert perf.proven is True


def test_a_trader_is_unproven_below_the_sample_floor(portfolio, token):
    wallet = portfolio.wallets()[0]
    for _ in range(MIN_CLOSED_FOR_RANK - 1):
        close_cycle(portfolio, wallet, A, token, 1.0, 1.2)
    assert portfolio.trader_performance()[A].proven is False


def test_the_record_survives_a_restart(portfolio, token, db, bus, config_store):
    wallet = portfolio.wallets()[0]
    close_cycle(portfolio, wallet, A, token, 1.0, 1.5)
    close_cycle(portfolio, wallet, A, token, 1.0, 1.5)
    # A fresh portfolio over the same DB rebuilds the aggregate from the
    # retained closed positions — the history is not memory-only.
    reborn = PortfolioManager(db, bus, config_store, FakeProvider())
    perf = reborn.trader_performance()[A]
    assert perf.closed_count == 2
    assert perf.realized_pnl_sol == pytest.approx(1.0)


def test_close_fires_the_on_close_hook(portfolio, token):
    seen = []
    portfolio.on_close = lambda position: seen.append(position.id)
    wallet = portfolio.wallets()[0]
    close_cycle(portfolio, wallet, A, token, 1.0, 1.5)
    assert len(seen) == 1


def test_has_open_for_trader_tracks_flatness(portfolio, token):
    wallet = portfolio.wallets()[0]
    buy = Fill(order_id="b", side=TradeSide.BUY, mint=token.mint,
               quantity=1000.0, price_sol=0.001, sol_amount=1.0, fee_sol=0.0)
    position = portfolio.apply_buy(wallet, A, token, buy)
    assert portfolio.has_open_for_trader(A) is True
    sell = Fill(order_id="s", side=TradeSide.SELL, mint=token.mint,
                quantity=position.quantity, price_sol=0.001, sol_amount=1.2,
                fee_sol=0.0)
    portfolio.apply_close(wallet, position, sell, ExitReason.TRADER_EXIT)
    assert portfolio.has_open_for_trader(A) is False


# -- live-wallet prioritization (AppContext) -------------------------------

@pytest.fixture
def ctx(tmp_path, config_store):
    return AppContext(
        config_store=config_store,
        database=Database(path=tmp_path / "api.db"),
        keystore=EncryptedKeystore(path=tmp_path / "keystore.enc"),
        provider=FakeProvider(),
        market_data=FakeMarketData({"MintA111": make_token()}))


def follow(ctx, address, wallet_id):
    ctx.registry.update(TraderProfile(
        address=address, status=TraderStatus.FOLLOWED,
        assigned_wallet_id=wallet_id))


def paper_wallet(ctx):
    return next(w for w in ctx.portfolio.wallets() if w.is_paper)


def add_live(ctx):
    return ctx.portfolio.add_live_wallet("Vault", str(Keypair().pubkey()))


def prove(ctx, trader, wallet, pnl_each, n=MIN_CLOSED_FOR_RANK):
    token = make_token()
    for _ in range(n):
        close_cycle(ctx.portfolio, wallet, trader, token, 1.0, 1.0 + pnl_each)


def test_top_proven_performer_is_placed_on_the_live_wallet(ctx):
    live = add_live(ctx)
    paper = paper_wallet(ctx)
    follow(ctx, A, paper.id)
    follow(ctx, B, paper.id)
    prove(ctx, A, paper, pnl_each=+0.5)      # strong, proven
    prove(ctx, B, paper, pnl_each=-0.1)      # weak, proven

    plan = ctx._plan_assignment()
    assert plan[A] == live.id                # best performer -> live seat
    assert plan[B] != live.id               # weaker -> paper


def test_unproven_traders_stay_off_live_wallets(ctx):
    live = add_live(ctx)
    paper = paper_wallet(ctx)
    follow(ctx, A, paper.id)
    follow(ctx, B, paper.id)
    prove(ctx, A, paper, pnl_each=-0.2)      # proven, even if losing
    # B has no closed positions at all -> unproven

    plan = ctx._plan_assignment()
    # A proven fills the live seat before the unproven B, whatever A's PnL.
    assert plan[A] == live.id
    assert plan[B] != live.id


def test_a_trader_holding_a_position_is_not_moved_by_a_rebalance(ctx):
    live = add_live(ctx)
    paper = paper_wallet(ctx)
    follow(ctx, A, paper.id)                 # proven winner, should go live
    follow(ctx, B, live.id)                  # unproven, on the live seat

    # B is holding an open position on the live wallet BEFORE the rebalance,
    # so moving it off would mean liquidating real money.
    token = make_token()
    ctx.portfolio.apply_buy(
        ctx.portfolio.get_wallet(live.id), B, token,
        Fill(order_id="b", side=TradeSide.BUY, mint=token.mint,
             quantity=1000.0, price_sol=0.001, sol_amount=1.0, fee_sol=0.0))

    # A becomes the top proven performer; the close hook rebalances.
    prove(ctx, A, paper, pnl_each=+0.5)

    # A (flat, proven) takes a live seat; B stays put because it is NOT flat
    # — the safe-swap rule never liquidates to reshuffle.
    assert ctx.registry.get(A).assigned_wallet_id == live.id
    assert ctx.registry.get(B).assigned_wallet_id == live.id
    assert ctx.portfolio.has_open_for_trader(B) is True


def test_a_flat_unproven_trader_yields_the_live_seat(ctx):
    """The counterpart: with nothing to liquidate, a weaker/unproven trader
    IS moved off the live wallet so a proven performer can take it."""
    live = add_live(ctx)
    paper = paper_wallet(ctx)
    follow(ctx, A, paper.id)                 # proven winner
    follow(ctx, B, live.id)                  # unproven, flat, squatting live
    prove(ctx, A, paper, pnl_each=+0.5)

    assert ctx.registry.get(A).assigned_wallet_id == live.id
    assert ctx.registry.get(B).assigned_wallet_id != live.id


def test_closing_a_position_republishes_performance_and_rebalances(ctx):
    add_live(ctx)
    paper = paper_wallet(ctx)
    follow(ctx, A, paper.id)
    q = ctx.bus.subscribe()                  # events published from here on
    prove(ctx, A, paper, pnl_each=+0.4)      # 3 closes -> hook runs each time

    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait()["type"])
    assert "trader_performance" in kinds     # the moon-colour feed refreshed
    payload = ctx._performance_payload()
    assert payload["traders"][A]["proven"] is True
    assert payload["traders"][A]["realized_pnl_sol"] == pytest.approx(1.2)


# -- performance-scaled position sizing ------------------------------------

def test_measured_rank_scales_the_position_size(ctx):
    """The best-ranked proven trader earns the full size bonus; the
    worst-ranked proven and every unproven trader get none."""
    paper = paper_wallet(ctx)
    follow(ctx, A, paper.id)
    follow(ctx, B, paper.id)
    prove(ctx, A, paper, pnl_each=+0.5)      # best proven
    prove(ctx, B, paper, pnl_each=-0.1)      # worst proven
    bonus = ctx.store.config.risk.perf_size_bonus_max

    assert ctx.engine._performance_factor(A) == pytest.approx(1.0 + bonus)
    assert ctx.engine._performance_factor(B) == pytest.approx(1.0)
    assert ctx.engine._performance_factor(C) == pytest.approx(1.0)  # unproven
