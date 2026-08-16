"""Mark daemon: streams prices over open positions and arms the panic stop.

Every tick it re-prices all open positions from market data, feeds the ATR
tracker, trails each position's stop under its peak, and closes anything
that breaches its stop. It also derives the SOL/USD rate used across the
system and pushes a portfolio heartbeat to the frontend stream.
"""

from __future__ import annotations

import logging
import time

from ..chain.market_data import MarketDataService
from ..config import ConfigStore
from ..domain.models import ExitReason
from ..events import EventBus
from ..risk.atr import AtrTracker
from ..services.daemon import Daemon
from .engine import TradingEngine
from .portfolio import PortfolioManager

logger = logging.getLogger(__name__)


class MarkDaemon(Daemon):
    def __init__(self, store: ConfigStore, portfolio: PortfolioManager,
                 market_data: MarketDataService, atr: AtrTracker,
                 engine: TradingEngine, bus: EventBus) -> None:
        super().__init__("marker", store.config.follow.price_mark_interval_sec)
        self._store = store
        self._portfolio = portfolio
        self._market_data = market_data
        self._atr = atr
        self._engine = engine
        self._bus = bus

    def tick(self) -> None:
        config = self._store.config
        open_positions = self._portfolio.open_positions()
        mints = {p.mint for p in open_positions}
        now = time.time()

        for mint in mints:
            token = self._market_data.get_token_info(mint)
            if token is None or token.price_sol <= 0:
                continue
            if token.price_usd > 0:
                self._portfolio.set_sol_price_usd(
                    token.price_usd / token.price_sol)
            self._atr.add_sample(mint, now, token.price_sol)
            updated = self._portfolio.mark_price(mint, token.price_sol)
            for position in updated:
                stop = self._atr.stop_price(
                    mint, position.peak_price_sol,
                    config.risk.atr_stop_multiplier)
                self._portfolio.update_stop(position, stop)
                if 0.0 < stop and token.price_sol < stop:
                    logger.info("panic stop hit for %s (%.8f < %.8f)",
                                position.symbol, token.price_sol, stop)
                    self._engine.close_position(position, ExitReason.PANIC_STOP)

        self._bus.publish("portfolio_tick", self._portfolio.snapshot())
