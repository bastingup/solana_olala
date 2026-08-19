"""Fills must price at the market, not at a cached mark.

A stale price used as an execution price silently falsifies every paper
result on fast-moving tokens — the exact measurement the HFT profile
depends on.
"""

import time

import pytest

from olala.chain.market_data import (CACHE_TTL_SEC, FILL_PRICE_MAX_AGE_SEC,
                                     MarketDataService)
from olala.domain.models import TradeSide, TraderProfile, TraderStatus
from olala.risk.engine import RiskEngine
from olala.services.traders import TraderRegistry
from olala.trading.engine import TradingEngine
from olala.trading.executor import PaperExecutor
from olala.trading.portfolio import PortfolioManager

from conftest import make_token
from fakes import FakeProvider
from test_trading_engine import ApprovingSafety, signal


class AgingMarketData:
    """Serves a stale price until asked for a fresh one, then a new one."""

    def __init__(self, mint):
        self.stale = make_token(mint=mint, price_sol=0.010)
        self.fresh = make_token(mint=mint, price_sol=0.020)
        self.max_ages = []

    def get_token_info(self, mint, max_age=CACHE_TTL_SEC):
        self.max_ages.append(max_age)
        return self.fresh if max_age <= FILL_PRICE_MAX_AGE_SEC else self.stale

    def search_winners(self, *a, **k):
        return []


@pytest.fixture
def world(db, bus, config_store):
    market = AgingMarketData("MintA111")
    portfolio = PortfolioManager(db, bus, config_store, FakeProvider())
    registry = TraderRegistry(db, bus)
    wallet = portfolio.wallets()[0]
    registry.update(TraderProfile(address="t1",
                                  status=TraderStatus.FOLLOWED,
                                  assigned_wallet_id=wallet.id))
    engine = TradingEngine(
        config_store, portfolio, registry, market, ApprovingSafety(),
        RiskEngine(), bus, paper_executor=PaperExecutor(),
        live_executor=None)
    return market, portfolio, engine, wallet


def test_buy_fills_at_a_freshly_read_price(world):
    market, portfolio, engine, wallet = world

    engine.handle_signal(signal(TradeSide.BUY))

    position = portfolio.open_positions(wallet.id)[0]
    # Priced from the FRESH mark (0.020), not the cached one (0.010).
    assert position.entry_price_sol > 0.015
    # And the refresh really was requested with a near-zero max age.
    assert min(market.max_ages) <= FILL_PRICE_MAX_AGE_SEC


def test_close_prices_at_the_market_too(world):
    market, portfolio, engine, wallet = world
    engine.handle_signal(signal(TradeSide.BUY))
    market.max_ages.clear()

    position = portfolio.open_positions(wallet.id)[0]
    from olala.domain.models import ExitReason
    assert engine.close_position(position, ExitReason.MANUAL)

    assert min(market.max_ages) <= FILL_PRICE_MAX_AGE_SEC


def test_only_a_stale_mark_forces_a_refetch_at_fill_time():
    """Gating reads stay cheap; a fill re-reads only when the cached
    mark is old enough to misprice the trade."""
    service = MarketDataService()
    calls = []

    def fake_fetch(mint):
        calls.append(mint)
        return make_token(mint=mint)

    service._fetch_token_info = fake_fetch
    service.get_token_info("MintA111")
    service.get_token_info("MintA111")           # cached
    assert len(calls) == 1

    # A just-fetched price is genuinely fresh: no refetch, no latency.
    service.get_token_info("MintA111", max_age=FILL_PRICE_MAX_AGE_SEC)
    assert len(calls) == 1

    # Age the entry past the fill threshold but inside the browse cache.
    with service._lock:
        stamp, info = service._cache["MintA111"]
        service._cache["MintA111"] = (
            stamp - (FILL_PRICE_MAX_AGE_SEC + 1.0), info)

    service.get_token_info("MintA111")           # browsing: still cached
    assert len(calls) == 1
    service.get_token_info("MintA111", max_age=FILL_PRICE_MAX_AGE_SEC)
    assert len(calls) == 2                        # fill forced a refetch


def test_default_cache_window_is_short_enough_for_fast_tokens():
    # A mark older than this would be used to price a trade; on a
    # fast-moving token that is the difference between measuring the
    # strategy and measuring the cache.
    assert CACHE_TTL_SEC <= 15.0
    assert FILL_PRICE_MAX_AGE_SEC <= 2.0
