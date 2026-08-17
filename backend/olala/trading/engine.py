"""Trading engine: turns copy signals into risk-gated executions.

The engine is the only component allowed to call an executor. It selects
paper or live execution per order: live requires a real wallet the
operator has armed — in every other case orders are paper. It never
originates trades; it only follows signals and the panic stop.
"""

from __future__ import annotations

import logging

from ..chain.jupiter import JupiterError
from ..chain.market_data import MarketDataService
from ..chain.provider import ChainError
from ..config import ConfigStore
from ..domain.models import (CopySignal, ExitReason, Position, TokenInfo,
                             TradeSide)
from ..domain.wallet import Wallet
from ..events import EventBus
from ..risk.engine import RiskEngine
from ..risk.token_safety import TokenSafetyScreen
from ..security.keystore import KeystoreError
from ..services.traders import TraderRegistry
from ..trading.portfolio import PortfolioManager
from .executor import ExecutionError, TradeExecutor

logger = logging.getLogger(__name__)


class TradingEngine:
    def __init__(self, store: ConfigStore, portfolio: PortfolioManager,
                 registry: TraderRegistry, market_data: MarketDataService,
                 safety: TokenSafetyScreen, risk: RiskEngine, bus: EventBus,
                 paper_executor: TradeExecutor,
                 live_executor: TradeExecutor) -> None:
        self._store = store
        self._portfolio = portfolio
        self._registry = registry
        self._market_data = market_data
        self._safety = safety
        self._risk = risk
        self._bus = bus
        self._paper_executor = paper_executor
        self._live_executor = live_executor

    def _wallet_may_trade(self, wallet: Wallet) -> bool:
        """Paper wallets always simulate; live wallets trade only while
        the operator has armed them."""
        if wallet.is_paper:
            return True
        return wallet.armed

    def _executor_for(self, wallet: Wallet) -> TradeExecutor:
        if wallet.is_paper:
            return self._paper_executor
        if not self._wallet_may_trade(wallet):
            # Defense in depth: routing must never hand a disarmed live
            # wallet to any executor.
            raise ExecutionError("live wallet is disarmed")
        return self._live_executor

    # -- signal handling ---------------------------------------------------

    def handle_signal(self, signal: CopySignal) -> None:
        token = self._market_data.get_token_info(signal.mint)
        payload = signal.to_dict()
        payload["symbol"] = token.symbol if token else f"{signal.mint[:4]}…"
        self._bus.publish("copy_signal", payload)
        profile = self._registry.get(signal.trader)
        if profile is None or not profile.assigned_wallet_id:
            return
        wallet = self._portfolio.get_wallet(profile.assigned_wallet_id)
        if wallet is None:
            return
        if not self._wallet_may_trade(wallet):
            self._reject(signal, wallet,
                         "live wallet is dark — arm it to trade")
            return
        try:
            if signal.side is TradeSide.BUY:
                self._handle_buy(signal, wallet, token)
            else:
                self._handle_sell(signal, wallet)
        except (ExecutionError, ChainError, JupiterError,
                KeystoreError) as exc:
            logger.warning("execution failed for signal %s: %s",
                           signal.observed.signature, exc)
            self._bus.publish("execution_error", {
                "signal": signal.to_dict(), "error": str(exc)})

    def _handle_buy(self, signal: CopySignal, wallet: Wallet,
                    token: TokenInfo | None) -> None:
        config = self._store.config
        if token is None:
            self._reject(signal, wallet, "no market data for token")
            return
        if not config.dev_mode:
            report = self._safety.check(token, config.filters, config.risk)
            if not report.safe:
                self._reject(signal, wallet, f"safety: {report.reason}")
                return
        is_resize = self._portfolio.find_open(
            wallet.id, signal.trader, signal.mint) is not None
        exposure = self._portfolio.exposure(wallet.id, signal.mint)
        verdict = self._risk.evaluate_entry(config, token, exposure, is_resize)
        if not verdict.approved:
            self._reject(signal, wallet, verdict.reason)
            return
        fill = self._executor_for(wallet).buy(wallet, token, verdict.size_sol)
        position = self._portfolio.apply_buy(
            wallet, signal.trader, token, fill)
        self._bus.publish("trade_executed", {
            "position_id": position.id, "wallet_id": wallet.id,
            "side": "buy", "symbol": token.symbol, "mint": token.mint,
            "sol_amount": fill.sol_amount, "resize": is_resize,
            "trader": signal.trader})

    def _handle_sell(self, signal: CopySignal, wallet: Wallet) -> None:
        position = self._portfolio.find_open(
            wallet.id, signal.trader, signal.mint)
        if position is None:
            return
        self.close_position(position, ExitReason.TRADER_EXIT)

    # -- exits -------------------------------------------------------------

    def close_position(self, position: Position, reason: ExitReason) -> None:
        wallet = self._portfolio.get_wallet(position.wallet_id)
        if wallet is None:
            return
        if not self._wallet_may_trade(wallet):
            self._bus.publish("execution_error", {
                "position_id": position.id,
                "error": f"cannot close {position.symbol}: wallet is "
                         "disarmed — arm it to manage its positions"})
            return
        if not self._portfolio.begin_close(position):
            return  # Another thread already owns this close.
        token = self._market_data.get_token_info(position.mint)
        if token is None:
            # Market data outage: exit anyway at the last known mark with
            # worst-case modeled slippage rather than stay unmanaged.
            token = TokenInfo(
                mint=position.mint, symbol=position.symbol, name="",
                price_usd=0.0, price_sol=position.last_price_sol,
                liquidity_usd=0.0, market_cap_usd=0.0, pair_address="",
                dex="", pair_created_at=0.0)
        try:
            fill = self._executor_for(wallet).sell(
                wallet, token, position.quantity)
        except (ExecutionError, ChainError, JupiterError,
                KeystoreError) as exc:
            self._portfolio.abort_close(position.id)
            logger.warning("close failed for position %s: %s",
                           position.id, exc)
            self._bus.publish("execution_error", {
                "position_id": position.id, "error": str(exc)})
            return
        self._portfolio.apply_close(wallet, position, fill, reason)
        self._bus.publish("trade_executed", {
            "position_id": position.id, "wallet_id": wallet.id,
            "side": "sell", "symbol": position.symbol, "mint": position.mint,
            "sol_amount": fill.sol_amount, "reason": reason.value,
            "trader": position.trader})

    def _reject(self, signal: CopySignal, wallet: Wallet,
                reason: str) -> None:
        logger.info("signal rejected (%s): %s", reason,
                    signal.observed.signature)
        self._bus.publish("risk_rejected", {
            "signal": signal.to_dict(), "wallet_id": wallet.id,
            "reason": reason})
