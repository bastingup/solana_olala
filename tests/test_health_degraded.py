"""Blind policy: block entries, never exits.

The operator's decision, and the right one. Entering a position we
cannot watch is the single failure with no recovery — we copy the buy
and never see the sell. Closing one while blind is exactly what you
want, so exits are never gated.

The check is PER TRADER on purpose. The realistic failure is not
"everything is down"; it is one wallet quietly dropping out of view
while the dashboard stays green.
"""

import time

from olala.domain.models import (TradeSide, TraderProfile, TraderStatus)
from olala.risk.engine import RiskEngine
from olala.services.traders import TraderRegistry
from olala.trading.engine import TradingEngine
from olala.trading.executor import PaperExecutor
from olala.trading.portfolio import PortfolioManager

from conftest import make_token
from fakes import FakeMarketData, FakeProvider
from test_trading_engine import ApprovingSafety, signal

TRADER = "t1"


def world(db, bus, config_store, token, health=None):
    portfolio = PortfolioManager(db, bus, config_store, FakeProvider())
    wallet = portfolio.add_paper_wallet("Scout", 10.0)
    registry = TraderRegistry(db, bus)
    registry.update(TraderProfile(address=TRADER,
                                  status=TraderStatus.FOLLOWED,
                                  assigned_wallet_id=wallet.id))
    engine = TradingEngine(
        config_store, portfolio, registry, FakeMarketData({token.mint: token}),
        ApprovingSafety(), RiskEngine(), bus,
        paper_executor=PaperExecutor(), live_executor=None,
        tracking_health=health)
    return engine, portfolio, wallet


def test_a_blind_trader_may_not_be_entered(db, bus, config_store):
    token = make_token()
    engine, portfolio, wallet = world(
        db, bus, config_store, token,
        health=lambda t: "last seen 900s ago — refusing to enter")
    events = bus.subscribe()

    engine.handle_signal(signal(TradeSide.BUY))

    assert portfolio.open_positions(wallet.id) == []
    kinds = []
    while not events.empty():
        kinds.append(events.get_nowait()["type"])
    assert "risk_rejected" in kinds


def test_a_blind_trader_may_still_be_exited(db, bus, config_store):
    """Getting out while blind is the whole point of not gating exits."""
    token = make_token()
    blind = {"on": False}
    engine, portfolio, wallet = world(
        db, bus, config_store, token,
        health=lambda t: "blind" if blind["on"] else "")

    engine.handle_signal(signal(TradeSide.BUY))
    assert len(portfolio.open_positions(wallet.id)) == 1

    blind["on"] = True
    engine.handle_signal(signal(TradeSide.SELL))
    assert portfolio.open_positions(wallet.id) == []


def test_a_healthy_trader_is_unaffected(db, bus, config_store):
    token = make_token()
    engine, portfolio, wallet = world(db, bus, config_store, token,
                                      health=lambda t: "")
    engine.handle_signal(signal(TradeSide.BUY))
    assert len(portfolio.open_positions(wallet.id)) == 1


def test_no_health_probe_means_no_gate(db, bus, config_store):
    token = make_token()
    engine, portfolio, wallet = world(db, bus, config_store, token)
    engine.handle_signal(signal(TradeSide.BUY))
    assert len(portfolio.open_positions(wallet.id)) == 1


def test_a_broken_health_probe_does_not_block_trading(db, bus, config_store):
    """It must be loud, but a bug in the probe must not halt the system."""
    token = make_token()

    def exploding(trader):
        raise RuntimeError("probe is broken")

    engine, portfolio, wallet = world(db, bus, config_store, token,
                                      health=exploding)
    engine.handle_signal(signal(TradeSide.BUY))
    assert len(portfolio.open_positions(wallet.id)) == 1


# -- the tracker's own verdict --------------------------------------------

def test_tracker_reports_an_unobserved_trader_as_blind(db, bus,
                                                       config_store):
    from test_tracker import make_world

    provider, registry, queue, tracker = make_world(db, bus, config_store)
    address = registry.followed()[0].address
    assert "not yet observed" in tracker.blind_reason(address)


def test_a_freshly_swept_trader_is_not_blind(db, bus, config_store):
    from test_tracker import make_world, sig_entry, sweep

    provider, registry, queue, tracker = make_world(db, bus, config_store)
    address = registry.followed()[0].address
    provider.signatures[address] = [sig_entry("s0", 100)]
    sweep(tracker)
    assert tracker.blind_reason(address) == ""


def test_a_trader_that_stopped_being_observed_goes_blind(db, bus,
                                                         config_store):
    from test_tracker import make_world, sig_entry, sweep

    provider, registry, queue, tracker = make_world(db, bus, config_store)
    address = registry.followed()[0].address
    provider.signatures[address] = [sig_entry("s0", 100)]
    sweep(tracker)

    # Rewind the observation far past any reasonable coverage window.
    tracker._last_seen[address] = time.time() - 3600
    assert "last seen" in tracker.blind_reason(address)
