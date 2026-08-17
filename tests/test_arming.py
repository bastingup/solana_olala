"""Side-by-side paper/live wallets: per-wallet arming and the gates."""

import pytest
from solders.keypair import Keypair

from olala.domain.models import TradeSide, TraderProfile, TraderStatus
from olala.risk.engine import RiskEngine, WalletExposure
from olala.services.traders import TraderRegistry
from olala.trading.engine import TradingEngine
from olala.trading.executor import PaperExecutor
from olala.trading.portfolio import PortfolioManager

from conftest import make_token
from fakes import FakeMarketData, FakeProvider
from test_trading_engine import ApprovingSafety, signal


class RecordingLiveExecutor(PaperExecutor):
    """Live-executor stand-in: paper fills, but records that it was used."""

    def __init__(self):
        self.calls = []

    def buy(self, wallet, token, sol_amount):
        self.calls.append(("buy", wallet.id))
        return super().buy(wallet, token, sol_amount)

    def sell(self, wallet, token, quantity):
        self.calls.append(("sell", wallet.id))
        return super().sell(wallet, token, quantity)


@pytest.fixture
def live_world(db, bus, config_store, token):
    provider = FakeProvider()
    portfolio = PortfolioManager(db, bus, config_store, provider)
    registry = TraderRegistry(db, bus)
    live_wallet = portfolio.add_live_wallet("Vault", str(Keypair().pubkey()))
    provider.sol_balances[live_wallet.address] = 10.0
    registry.update(TraderProfile(address="t1",
                                  status=TraderStatus.FOLLOWED,
                                  assigned_wallet_id=live_wallet.id))
    live_executor = RecordingLiveExecutor()
    engine = TradingEngine(
        config_store, portfolio, registry, FakeMarketData({token.mint: token}),
        ApprovingSafety(), RiskEngine(), bus,
        paper_executor=PaperExecutor(), live_executor=live_executor)
    events = bus.subscribe()

    def kinds():
        collected = []
        while not events.empty():
            collected.append(events.get_nowait()["type"])
        return collected

    return type("W", (), {"portfolio": portfolio, "engine": engine,
                          "wallet": live_wallet, "live": live_executor,
                          "kinds": kinds, "store": config_store})


def test_new_live_wallet_starts_dark(live_world):
    assert live_world.wallet.armed is False
    assert live_world.wallet.to_dict()["armed"] is False


def test_dark_wallet_does_not_trade(live_world):
    live_world.engine.handle_signal(signal(TradeSide.BUY))
    assert "risk_rejected" in live_world.kinds()
    assert live_world.live.calls == []
    assert live_world.portfolio.open_positions(live_world.wallet.id) == []


def test_armed_wallet_trades_live(live_world):
    live_world.portfolio.set_wallet_armed(live_world.wallet.id, True)
    live_world.engine.handle_signal(signal(TradeSide.BUY))
    assert ("buy", live_world.wallet.id) in live_world.live.calls
    assert len(live_world.portfolio.open_positions(live_world.wallet.id)) == 1


def test_paper_wallets_always_use_paper_executor(db, bus, config_store,
                                                 token):
    portfolio = PortfolioManager(db, bus, config_store, FakeProvider())
    registry = TraderRegistry(db, bus)
    wallet = portfolio.wallets()[0]
    registry.update(TraderProfile(address="t1",
                                  status=TraderStatus.FOLLOWED,
                                  assigned_wallet_id=wallet.id))
    # live_executor=None: routing a paper order to it would crash loudly.
    engine = TradingEngine(
        config_store, portfolio, registry, FakeMarketData({token.mint: token}),
        ApprovingSafety(), RiskEngine(), bus,
        paper_executor=PaperExecutor(), live_executor=None)
    engine.handle_signal(signal(TradeSide.BUY))
    assert len(portfolio.open_positions(wallet.id)) == 1


def test_arm_state_survives_reload(db, bus, config_store):
    provider = FakeProvider()
    portfolio = PortfolioManager(db, bus, config_store, provider)
    wallet = portfolio.add_live_wallet("Vault", "SomeAddr111")
    portfolio.set_wallet_armed(wallet.id, True)

    reloaded = PortfolioManager(db, bus, config_store, provider)
    assert reloaded.get_wallet(wallet.id).armed is True


def test_arming_paper_wallet_rejected(db, bus, config_store):
    portfolio = PortfolioManager(db, bus, config_store, FakeProvider())
    with pytest.raises(ValueError):
        portfolio.set_wallet_armed(portfolio.wallets()[0].id, True)


# -- correlation gate: max 2 live wallets per token -----------------------

def exposure(is_paper, holders, resize=False):
    return WalletExposure(
        wallet_id="w1", cash_sol=10.0, equity_sol=10.0, open_positions=0,
        invested_in_mint_sol=0.3 if resize else 0.0,
        fleet_invested_in_mint_sol=0.3 if resize else 0.0,
        wallet_is_paper=is_paper, live_wallets_holding_mint=holders)


def test_third_live_wallet_blocked(config_store, token):
    verdict = RiskEngine().evaluate_entry(
        config_store.config, token, exposure(is_paper=False, holders=2),
        is_resize=False)
    assert not verdict.approved
    assert "live wallets" in verdict.reason


def test_second_live_wallet_allowed(config_store, token):
    verdict = RiskEngine().evaluate_entry(
        config_store.config, token, exposure(is_paper=False, holders=1),
        is_resize=False)
    assert verdict.approved


def test_paper_wallets_exempt_from_correlation_gate(config_store, token):
    verdict = RiskEngine().evaluate_entry(
        config_store.config, token, exposure(is_paper=True, holders=5),
        is_resize=False)
    assert verdict.approved


def test_resize_exempt_from_correlation_gate(config_store, token):
    verdict = RiskEngine().evaluate_entry(
        config_store.config, token, exposure(is_paper=False, holders=2,
                                             resize=True),
        is_resize=True)
    assert verdict.approved


def test_exposure_counts_only_live_holders(db, bus, config_store, token):
    from olala.domain.models import Fill

    provider = FakeProvider()
    portfolio = PortfolioManager(db, bus, config_store, provider)
    paper = portfolio.wallets()[0]
    live = portfolio.add_live_wallet("Vault", "Addr111")
    provider.sol_balances[live.address] = 5.0

    def fill(order):
        return Fill(order_id=order, side=TradeSide.BUY, mint=token.mint,
                    quantity=100, price_sol=0.01, sol_amount=1.0, fee_sol=0)

    portfolio.apply_buy(paper, "t1", token, fill("o1"))
    portfolio.apply_buy(live, "t2", token, fill("o2"))
    result = portfolio.exposure(paper.id, token.mint)
    assert result.live_wallets_holding_mint == 1  # paper holder not counted
    assert result.wallet_is_paper is True
