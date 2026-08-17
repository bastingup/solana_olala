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
import time
from abc import ABC, abstractmethod
from typing import Callable

from solders.transaction import VersionedTransaction

from ..chain.jupiter import SOL_MINT, JupiterClient
from ..chain.provider import LAMPORTS_PER_SOL, ChainError, RpcProvider
from ..discovery.reconstruction import TradeReconstructor
from ..domain.models import (Fill, Receipt, ReceiptStatus, TokenInfo,
                             TradeSide, new_id)
from ..domain.wallet import Wallet
from ..security.keystore import EncryptedKeystore

logger = logging.getLogger(__name__)

PAPER_FEE_SOL = 0.000105
PAPER_BASE_SPREAD = 0.001
MAX_MODELED_IMPACT = 0.05

# A transaction's blockhash expires ~60-90s after send; a signature still
# unconfirmed after this window can never land, so giving up is safe —
# "timeout" is a definitive no-execution, not an unknown.
CONFIRM_TIMEOUT_SEC = 100.0
CONFIRM_POLL_SEC = 2.0


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
    """Real execution through Jupiter. Selected only for armed live wallets.

    Nothing is booked from the quote. Every order is sent, then CONFIRMED
    on chain, then its actual amounts are reconstructed from the landed
    transaction; the quote only sizes the order. Every attempt — landed,
    reverted, or expired — is recorded as a :class:`Receipt` through
    ``on_receipt``, forming the audit trail the operator sees.
    """

    def __init__(self, jupiter: JupiterClient, provider: RpcProvider,
                 keystore: EncryptedKeystore,
                 on_receipt: Callable[[Receipt], None] | None = None,
                 confirm_timeout_sec: float = CONFIRM_TIMEOUT_SEC,
                 confirm_poll_sec: float = CONFIRM_POLL_SEC) -> None:
        self._jupiter = jupiter
        self._provider = provider
        self._keystore = keystore
        self._on_receipt = on_receipt
        self._confirm_timeout = confirm_timeout_sec
        self._confirm_poll = confirm_poll_sec
        self._reconstructor = TradeReconstructor()

    def buy(self, wallet: Wallet, token: TokenInfo, sol_amount: float) -> Fill:
        lamports = int(sol_amount * LAMPORTS_PER_SOL)
        quote = self._jupiter.get_quote(SOL_MINT, token.mint, lamports)
        decimals = self._provider.get_token_decimals(token.mint)
        quoted_tokens = int(quote["outAmount"]) / (10 ** decimals)
        return self._execute(wallet, token, TradeSide.BUY, quote,
                             quoted_sol=sol_amount,
                             quoted_tokens=quoted_tokens)

    def sell(self, wallet: Wallet, token: TokenInfo, quantity: float) -> Fill:
        decimals = self._provider.get_token_decimals(token.mint)
        base_units = int(quantity * (10 ** decimals))
        quote = self._jupiter.get_quote(token.mint, SOL_MINT, base_units)
        quoted_sol = int(quote["outAmount"]) / LAMPORTS_PER_SOL
        return self._execute(wallet, token, TradeSide.SELL, quote,
                             quoted_sol=quoted_sol, quoted_tokens=quantity)

    # -- the send → confirm → reconstruct pipeline -------------------------

    def _execute(self, wallet: Wallet, token: TokenInfo, side: TradeSide,
                 quote: dict, quoted_sol: float,
                 quoted_tokens: float) -> Fill:
        order_id = new_id()
        signature = self._sign_and_send(wallet, quote)
        receipt = Receipt(signature=signature, order_id=order_id,
                          wallet_id=wallet.id, side=side, mint=token.mint,
                          status=ReceiptStatus.TIMEOUT,
                          quoted_sol=quoted_sol,
                          quoted_tokens=quoted_tokens)
        try:
            status = self._await_confirmation(signature)
        except ExecutionError as exc:
            receipt.detail = str(exc)
            self._record(receipt)
            raise

        receipt.slot = int(status.get("slot") or 0)
        if status.get("err") is not None:
            # It landed, and the program rejected it: nothing moved
            # except the network fee.
            receipt.status = ReceiptStatus.FAILED
            receipt.detail = f"transaction failed on chain: {status['err']}"
            self._record(receipt)
            raise ExecutionError(receipt.detail)

        receipt.status = ReceiptStatus.CONFIRMED
        actual_sol, actual_tokens, fee_sol, block_time, note = \
            self._actuals_from_chain(wallet, signature, token,
                                     quoted_sol, quoted_tokens)
        receipt.actual_sol = actual_sol
        receipt.actual_tokens = actual_tokens
        receipt.fee_sol = fee_sol
        receipt.block_time = block_time
        receipt.detail = note
        self._record(receipt)

        price = actual_sol / actual_tokens if actual_tokens else 0.0
        logger.info("live %s confirmed for %s: %s (%.6f SOL, %.6f tokens)",
                    side.value, wallet.id, signature, actual_sol,
                    actual_tokens)
        return Fill(order_id=order_id, side=side, mint=token.mint,
                    quantity=actual_tokens, price_sol=price,
                    sol_amount=actual_sol, fee_sol=fee_sol,
                    signature=signature)

    def _sign_and_send(self, wallet: Wallet, quote: dict) -> str:
        signer = self._keystore.get_signer(wallet.address)
        tx_b64 = self._jupiter.build_swap_transaction(quote, wallet.address)
        unsigned = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(unsigned.message, [signer])
        signature = self._provider.send_transaction(
            base64.b64encode(bytes(signed)).decode())
        logger.info("live swap sent by %s: %s", wallet.id, signature)
        return signature

    def _await_confirmation(self, signature: str) -> dict:
        """Poll until the signature confirms; raise once it cannot.

        Past the blockhash window an unlanded transaction is dead, so the
        timeout below is a definitive no-execution — the caller may treat
        it exactly like a rejected order.
        """
        deadline = time.monotonic() + self._confirm_timeout
        while time.monotonic() < deadline:
            try:
                status = self._provider.get_signature_status(signature)
            except ChainError as exc:
                logger.warning("confirmation poll failed for %s: %s",
                               signature, exc)
                status = None
            if status is not None:
                level = status.get("confirmationStatus")
                if level in ("confirmed", "finalized") \
                        or status.get("err") is not None:
                    return status
            time.sleep(self._confirm_poll)
        raise ExecutionError(
            f"transaction {signature} not confirmed within "
            f"{self._confirm_timeout:.0f}s — blockhash expired, order is "
            "dead")

    def _actuals_from_chain(self, wallet: Wallet, signature: str,
                            token: TokenInfo, quoted_sol: float,
                            quoted_tokens: float
                            ) -> tuple[float, float, float, float, str]:
        """True fill amounts from the landed transaction's balance diffs.

        Falls back to the quoted amounts (flagged in the receipt) only if
        the transaction cannot be fetched or reconstructed — the fill is
        still real, we just could not read its exact numbers.
        """
        try:
            tx = self._provider.get_transaction(signature)
        except ChainError as exc:
            logger.warning("could not fetch landed tx %s: %s",
                           signature, exc)
            tx = None
        if tx is not None:
            fee_sol = ((tx.get("meta") or {}).get("fee") or 0) \
                / LAMPORTS_PER_SOL
            block_time = float(tx.get("blockTime") or 0.0)
            trade = self._reconstructor.reconstruct(
                wallet.address, signature, tx)
            if trade is not None and trade.mint == token.mint:
                return (trade.sol_amount, trade.token_amount, fee_sol,
                        block_time, "")
            return (quoted_sol, quoted_tokens, fee_sol, block_time,
                    "amounts from quote — landed tx did not reconstruct")
        return (quoted_sol, quoted_tokens, 0.0, 0.0,
                "amounts from quote — landed tx unavailable")

    def _record(self, receipt: Receipt) -> None:
        if self._on_receipt is None:
            return
        try:
            self._on_receipt(receipt)
        except Exception:
            logger.exception("receipt recording failed for %s",
                             receipt.signature)
