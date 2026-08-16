import pytest

from olala.domain.models import (CopySignal, ExitReason, ObservedTrade,
                                 TraderProfile, TraderStatus, TradeSide)
from olala.risk.engine import RiskEngine
from olala.services.traders import TraderRegistry
from olala.trading.engine import TradingEngine
from olala.trading.executor import PaperExecutor
from olala.trading.portfolio import PortfolioManager

from conftest import make_token
from fakes import FakeMarketData, FakeProvider


class ApprovingSafety:
    def check(self, token, filters, risk):
        from olala.risk.token_safety import SafetyReport
        return SafetyReport(True)


class RefusingSafety:
    def check(self, token, filters, risk):
        from olala.risk.token_safety import SafetyReport
        return SafetyReport(False, "test refusal")


@pytest.fixture
def world(db, bus, config_store, token):
    portfolio = PortfolioManager(db, bus, config_store, FakeProvider())
    registry = TraderRegistry(db, bus)
    market = FakeMarketData({token.mint: token})
    events = []
    bus_queue = bus.subscribe()

    def drain():
        while not bus_queue.empty():
            events.append(bus_queue.get_nowait())
        return [e["type"] for e in events]

    def raw_events():
        drain()
        return events

    def engine(safety=None):
        return TradingEngine(
            config_store, portfolio, registry, market,
            safety or ApprovingSafety(), RiskEngine(), bus,
            paper_executor=PaperExecutor(), live_executor=None)

    wallet = portfolio.wallets()[0]
    profile = TraderProfile(address="t1", status=TraderStatus.FOLLOWED,
                            assigned_wallet_id=wallet.id)
    registry.update(profile)
    return type("World", (), {
        "portfolio": portfolio, "registry": registry, "wallet": wallet,
        "engine": engine, "drain": drain, "raw_events": raw_events,
        "market": market})


def signal(side, mint="MintA111", sol=5.0):
    observed = ObservedTrade(
        trader="t1", signature="sig1", side=side, mint=mint,
        token_amount=100.0, sol_amount=sol, price_sol=sol / 100.0,
        block_time=1_755_000_000)
    return CopySignal(trader="t1", side=side, mint=mint,
                      trader_sol_amount=sol, observed=observed)


def test_buy_signal_opens_position(world, token):
    world.engine().handle_signal(signal(TradeSide.BUY))
    kinds = world.drain()
    assert "copy_signal" in kinds
    assert "position_opened" in kinds
    assert "trade_executed" in kinds
    positions = world.portfolio.open_positions(world.wallet.id)
    assert len(positions) == 1
    assert positions[0].symbol == token.symbol


def test_safety_refusal_blocks_trade(world):
    world.engine(safety=RefusingSafety()).handle_signal(
        signal(TradeSide.BUY))
    kinds = world.drain()
    assert "risk_rejected" in kinds
    assert "position_opened" not in kinds
    assert world.portfolio.open_positions(world.wallet.id) == []


def test_unknown_token_rejected(world):
    world.engine().handle_signal(signal(TradeSide.BUY, mint="Unknown111"))
    kinds = world.drain()
    assert "risk_rejected" in kinds
    assert world.portfolio.open_positions(world.wallet.id) == []


def test_sell_signal_closes_position(world):
    engine = world.engine()
    engine.handle_signal(signal(TradeSide.BUY))
    engine.handle_signal(signal(TradeSide.SELL))
    kinds = world.drain()
    assert "position_closed" in kinds
    assert world.portfolio.open_positions(world.wallet.id) == []


def test_sell_without_position_is_noop(world):
    world.engine().handle_signal(signal(TradeSide.SELL))
    kinds = world.drain()
    assert "position_closed" not in kinds
    assert "execution_error" not in kinds


def test_unassigned_trader_ignored(world):
    profile = TraderProfile(address="t2", status=TraderStatus.CANDIDATE)
    world.registry.update(profile)
    sig = signal(TradeSide.BUY)
    sig.trader = "t2"
    sig.observed.trader = "t2"
    world.engine().handle_signal(sig)
    assert world.portfolio.open_positions(world.wallet.id) == []


def test_panic_close_without_market_data(world):
    engine = world.engine()
    engine.handle_signal(signal(TradeSide.BUY))
    position = world.portfolio.open_positions(world.wallet.id)[0]
    world.market.tokens.clear()  # market data outage
    engine.close_position(position, ExitReason.PANIC_STOP)
    assert position.status.value == "closed"
    assert position.exit_reason == "panic_stop"


def test_copy_signal_carries_symbol(world, token):
    world.engine().handle_signal(signal(TradeSide.BUY))
    copy_signals = [e for e in world.raw_events()
                    if e["type"] == "copy_signal"]
    assert copy_signals
    assert copy_signals[0]["data"]["symbol"] == token.symbol


def test_copy_signal_symbol_falls_back_to_mint(world):
    world.engine().handle_signal(signal(TradeSide.BUY, mint="Unknown111"))
    copy_signals = [e for e in world.raw_events()
                    if e["type"] == "copy_signal"]
    assert copy_signals[0]["data"]["symbol"].startswith("Unkn")
