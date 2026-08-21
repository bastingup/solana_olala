"""Trading engine: turns copy signals into risk-gated executions.

The engine is the only component allowed to call an executor. It selects
paper or live execution per order: live requires a real wallet the
operator has armed — in every other case orders are paper. It never
originates trades; it only follows signals and the panic stop.
"""

from __future__ import annotations

import logging
from typing import Callable

from ..chain.jupiter import JupiterError
from ..constants import LAMPORTS_PER_SOL, SOL_MINT
from ..chain.market_data import FILL_PRICE_MAX_AGE_SEC, MarketDataService
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
                 live_executor: TradeExecutor,
                 tracking_health: Callable[[str], str] | None = None,
                 quoter=None) -> None:
        self._store = store
        self._portfolio = portfolio
        self._registry = registry
        self._market_data = market_data
        self._safety = safety
        self._risk = risk
        self._bus = bus
        self._paper_executor = paper_executor
        self._live_executor = live_executor
        # Returns a non-empty reason when a trader cannot currently be
        # watched, which blocks ENTRIES only.
        self._tracking_health = tracking_health
        # Asked for a real price impact when the feed reports no depth.
        self._quoter = quoter

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

    def _fill_probe(self, mint: str, size_sol: float) -> float | None:
        """Price impact of buying ``size_sol`` of ``mint``, as a percent.

        Only consulted when the price feed reports no pool depth, so the
        cost is one quote on trades that would otherwise be refused
        outright. Returns None when the venue cannot route it at all —
        which IS the answer: no route means no fill.
        """
        if self._quoter is None:
            return None
        try:
            quote = self._quoter.get_quote(
                SOL_MINT, mint, int(size_sol * LAMPORTS_PER_SOL))
        except (JupiterError, ChainError) as exc:
            logger.info("no route for %s at %.4f SOL: %s",
                        mint[:8], size_sol, exc)
            return None
        if not quote or not int(quote.get("outAmount") or 0):
            return None
        return abs(float(quote.get("priceImpactPct") or 0.0)) * 100.0

    def _performance_factor(self, trader: str) -> float:
        """A size multiplier (>= 1.0) from this trader's measured rank.

        The best-performing PROVEN traders in our own database — the same
        realized-PnL ranking that colours the moons — earn a slightly
        bigger position; the worst-ranked proven trader and every unproven
        trader get the plain market-cap size. The bonus is RELATIVE to the
        current field, so it re-scales as the roster's records change.
        """
        bonus = self._store.config.risk.perf_size_bonus_max
        if bonus <= 0:
            return 1.0
        perf = self._portfolio.trader_performance()
        proven = [m for m in perf.values() if m.proven]
        mine = perf.get(trader)
        if not proven or mine is None or not mine.proven:
            return 1.0
        low = min(m.realized_pnl_sol for m in proven)
        high = max(m.realized_pnl_sol for m in proven)
        # A single proven trader (or a flat field) sits mid-scale rather
        # than claiming the full bonus it has not really out-earned anyone
        # for.
        rank01 = 0.5 if high <= low else (mine.realized_pnl_sol - low) / (
            high - low)
        return 1.0 + bonus * rank01

    def _blind_reason(self, trader: str) -> str:
        """Why this trader may not be ENTERED right now, if so."""
        if self._tracking_health is None:
            return ""
        try:
            return self._tracking_health(trader) or ""
        except Exception:                                   # noqa: BLE001
            # A broken health probe must not block trading outright, but
            # it must be loud: silently trading blind is the bad outcome.
            logger.exception("tracking health check failed for %s", trader)
            return ""

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
        # Entering a position we cannot watch is the one failure with no
        # recovery: we would copy the buy and never see the sell. Exits
        # are never gated this way — closing a position while blind is
        # exactly what you want.
        blind = self._blind_reason(signal.trader)
        if blind:
            self._reject(signal, wallet, blind)
            return
        if token is None:
            self._reject(signal, wallet, "no market data for token")
            return
        # Token safety follows the filter switch for PAPER wallets, so
        # simulation can run wide open — but it is UNCONDITIONAL for a
        # live wallet: real money is never exposed to a honeypot because
        # a filter flag was off.
        if config.dev_mode or not wallet.is_paper:
            report = self._safety.check(
                token, config.filters_onchain, config.risk)
            if not report.safe:
                self._reject(signal, wallet, f"safety: {report.reason}")
                return
        is_resize = self._portfolio.find_open(
            wallet.id, signal.trader, signal.mint) is not None
        exposure = self._portfolio.exposure(wallet.id, signal.mint)
        verdict = self._risk.evaluate_entry(
            config, token, exposure, is_resize, self._fill_probe,
            performance_factor=self._performance_factor(signal.trader))
        if not verdict.approved:
            self._reject(signal, wallet, verdict.reason)
            return
        # Re-price immediately before filling: a cached mark is fine for
        # gating, but a stale one as the FILL price would quietly falsify
        # every paper result on fast-moving tokens.
        token = self._fresh(token)
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

    def close_position(self, position: Position, reason: ExitReason) -> bool:
        """Close one open position; True only when the close executed."""
        wallet = self._portfolio.get_wallet(position.wallet_id)
        if wallet is None:
            return False
        if not self._wallet_may_trade(wallet):
            self._bus.publish("execution_error", {
                "position_id": position.id,
                "error": f"cannot close {position.symbol}: wallet is "
                         "disarmed — arm it to manage its positions"})
            return False
        if not self._portfolio.begin_close(position):
            return False  # Another thread already owns this close.
        # Exits price at the market too — see _handle_buy.
        token = self._market_data.get_token_info(
            position.mint, max_age=FILL_PRICE_MAX_AGE_SEC)
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
            return False
        self._portfolio.apply_close(wallet, position, fill, reason)
        self._bus.publish("trade_executed", {
            "position_id": position.id, "wallet_id": wallet.id,
            "side": "sell", "symbol": position.symbol, "mint": position.mint,
            "sol_amount": fill.sol_amount, "reason": reason.value,
            "trader": position.trader})
        return True

    def _fresh(self, token: TokenInfo) -> TokenInfo:
        """Re-read the mark right before it becomes a fill price; keep
        the gating snapshot if the refresh is unavailable."""
        latest = self._market_data.get_token_info(
            token.mint, max_age=FILL_PRICE_MAX_AGE_SEC)
        return latest if latest is not None else token

    def _reject(self, signal: CopySignal, wallet: Wallet,
                reason: str) -> None:
        logger.info("signal rejected (%s): %s", reason,
                    signal.observed.signature)
        self._bus.publish("risk_rejected", {
            "signal": signal.to_dict(), "wallet_id": wallet.id,
            "reason": reason})
