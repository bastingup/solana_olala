"""Reconstruct DEX swaps from raw transactions.

Instead of decoding every AMM's instruction layout, we diff the trader's
pre/post balances: a transaction where exactly one non-SOL token balance
moves against an opposite SOL movement is a swap, regardless of which DEX
routed it. This is robust across Raydium, Orca, Meteora, Jupiter routes, etc.
"""

from __future__ import annotations

import logging
from typing import Any

from ..domain.models import ObservedTrade, TradeSide

logger = logging.getLogger(__name__)

WSOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
MIN_TRADE_SOL = 0.01


class TradeReconstructor:
    def reconstruct(self, trader: str, signature: str,
                    tx: dict[str, Any] | None) -> ObservedTrade | None:
        """Return the trader's swap in this transaction, if it is one."""
        if not tx:
            return None
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            return None
        message = (tx.get("transaction") or {}).get("message") or {}
        account_keys = message.get("accountKeys") or []
        trader_index = next(
            (i for i, key in enumerate(account_keys)
             if (key.get("pubkey") if isinstance(key, dict) else key) == trader),
            None)
        if trader_index is None:
            return None

        sol_delta = self._sol_delta(meta, trader_index)
        token_deltas = self._token_deltas(meta, trader)
        sol_delta += token_deltas.pop(WSOL_MINT, 0.0)

        moved = {mint: delta for mint, delta in token_deltas.items()
                 if abs(delta) > 1e-9}
        if len(moved) != 1:
            return None
        mint, token_delta = next(iter(moved.items()))
        if abs(sol_delta) < MIN_TRADE_SOL:
            return None
        if token_delta > 0 and sol_delta < 0:
            side = TradeSide.BUY
        elif token_delta < 0 and sol_delta > 0:
            side = TradeSide.SELL
        else:
            return None

        sol_amount = abs(sol_delta)
        token_amount = abs(token_delta)
        return ObservedTrade(
            trader=trader, signature=signature, side=side, mint=mint,
            token_amount=token_amount, sol_amount=sol_amount,
            price_sol=sol_amount / token_amount,
            block_time=float(tx.get("blockTime") or 0.0))

    def _sol_delta(self, meta: dict[str, Any], trader_index: int) -> float:
        pre = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        if trader_index >= len(pre) or trader_index >= len(post):
            return 0.0
        delta = (post[trader_index] - pre[trader_index]) / LAMPORTS_PER_SOL
        if trader_index == 0:
            # The fee payer's balance drop includes the network fee; it is
            # not part of the traded amount.
            delta += (meta.get("fee") or 0) / LAMPORTS_PER_SOL
        return delta

    def _token_deltas(self, meta: dict[str, Any], trader: str) -> dict[str, float]:
        deltas: dict[str, float] = {}
        for entry in meta.get("preTokenBalances") or []:
            if entry.get("owner") != trader:
                continue
            amount = float((entry.get("uiTokenAmount") or {}).get("uiAmount") or 0.0)
            deltas[entry["mint"]] = deltas.get(entry["mint"], 0.0) - amount
        for entry in meta.get("postTokenBalances") or []:
            if entry.get("owner") != trader:
                continue
            amount = float((entry.get("uiTokenAmount") or {}).get("uiAmount") or 0.0)
            deltas[entry["mint"]] = deltas.get(entry["mint"], 0.0) + amount
        return deltas
