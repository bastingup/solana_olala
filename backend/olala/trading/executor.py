"""Trade execution.

``TradeExecutor`` is the abstract order path; ``PaperExecutor`` simulates
fills locally with a liquidity-derived slippage model, and
``LiveJupiterExecutor`` is the real Jupiter swap path. Live execution is
dormant by default: it is only ever selected for a real (non-paper)
wallet the operator has armed, which itself requires an unlocked
keystore holding that wallet's key.
"""

from __future__ import annotations

import base64
import logging
from abc import ABC, abstractmethod

from solders.transaction import VersionedTransaction

from ..chain.jupiter import SOL_MINT, JupiterClient
from ..chain.provider import LAMPORTS_PER_SOL, RpcProvider
from ..domain.models import Fill, TokenInfo, TradeSide, new_id
from ..domain.wallet import Wallet
from ..security.keystore import EncryptedKeystore

logger = logging.getLogger(__name__)

PAPER_FEE_SOL = 0.000105
PAPER_BASE_SPREAD = 0.001
MAX_MODELED_IMPACT = 0.05


class ExecutionError(RuntimeError):
    pass


class TradeExecutor(ABC):
    @abstractmethod
    def buy(self, wallet: Wallet, token: TokenInfo, sol_amount: float) -> Fill:
        """Spend ``sol_amount`` SOL on the token; returns the fill."""

    @abstractmethod
    def sell(self, wallet: Wallet, token: TokenInfo, quantity: float) -> Fill:
        """Sell ``quantity`` of the token back to SOL; returns the fill."""


class PaperExecutor(TradeExecutor):
    """Simulated fills at live market prices.

    Slippage is modeled as half the trade's share of pool liquidity plus a
    base spread, capped — small trades in deep pools fill near mid, and the
    model punishes anything that approaches the liquidity ceiling.
    """

    def _impact(self, token: TokenInfo, trade_sol: float) -> float:
        sol_usd = token.price_usd / token.price_sol if token.price_sol else 0.0
        trade_usd = trade_sol * sol_usd
        if token.liquidity_usd <= 0:
            return MAX_MODELED_IMPACT
        return min(0.5 * trade_usd / token.liquidity_usd, MAX_MODELED_IMPACT)

    def buy(self, wallet: Wallet, token: TokenInfo, sol_amount: float) -> Fill:
        price = token.price_sol * (
            1.0 + PAPER_BASE_SPREAD + self._impact(token, sol_amount))
        spendable = sol_amount - PAPER_FEE_SOL
        if spendable <= 0 or price <= 0:
            raise ExecutionError("order too small to cover fees")
        return Fill(order_id=new_id(), side=TradeSide.BUY, mint=token.mint,
                    quantity=spendable / price, price_sol=price,
                    sol_amount=sol_amount, fee_sol=PAPER_FEE_SOL)

    def sell(self, wallet: Wallet, token: TokenInfo, quantity: float) -> Fill:
        gross_sol = quantity * token.price_sol
        price = token.price_sol * (
            1.0 - PAPER_BASE_SPREAD - self._impact(token, gross_sol))
        proceeds = max(quantity * price - PAPER_FEE_SOL, 0.0)
        return Fill(order_id=new_id(), side=TradeSide.SELL, mint=token.mint,
                    quantity=quantity, price_sol=price,
                    sol_amount=proceeds, fee_sol=PAPER_FEE_SOL)


class LiveJupiterExecutor(TradeExecutor):
    """Real execution through Jupiter. Selected only for armed live wallets."""

    def __init__(self, jupiter: JupiterClient, provider: RpcProvider,
                 keystore: EncryptedKeystore) -> None:
        self._jupiter = jupiter
        self._provider = provider
        self._keystore = keystore

    def buy(self, wallet: Wallet, token: TokenInfo, sol_amount: float) -> Fill:
        lamports = int(sol_amount * LAMPORTS_PER_SOL)
        quote = self._jupiter.get_quote(SOL_MINT, token.mint, lamports)
        signature = self._sign_and_send(wallet, quote)
        decimals = self._provider.get_token_decimals(token.mint)
        quantity = int(quote["outAmount"]) / (10 ** decimals)
        return Fill(order_id=new_id(), side=TradeSide.BUY, mint=token.mint,
                    quantity=quantity,
                    price_sol=sol_amount / quantity if quantity else 0.0,
                    sol_amount=sol_amount, fee_sol=0.0, signature=signature)

    def sell(self, wallet: Wallet, token: TokenInfo, quantity: float) -> Fill:
        decimals = self._provider.get_token_decimals(token.mint)
        base_units = int(quantity * (10 ** decimals))
        quote = self._jupiter.get_quote(token.mint, SOL_MINT, base_units)
        signature = self._sign_and_send(wallet, quote)
        proceeds = int(quote["outAmount"]) / LAMPORTS_PER_SOL
        return Fill(order_id=new_id(), side=TradeSide.SELL, mint=token.mint,
                    quantity=quantity,
                    price_sol=proceeds / quantity if quantity else 0.0,
                    sol_amount=proceeds, fee_sol=0.0, signature=signature)

    def _sign_and_send(self, wallet: Wallet, quote: dict) -> str:
        signer = self._keystore.get_signer(wallet.address)
        tx_b64 = self._jupiter.build_swap_transaction(quote, wallet.address)
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(unsigned.message, [signer])
        signature = self._provider.send_transaction(
            base64.b64encode(bytes(signed)).decode())
        logger.info("live swap sent by %s: %s", wallet.id, signature)
        return signature
