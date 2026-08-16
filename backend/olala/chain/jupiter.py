"""Jupiter aggregator client (free ``lite-api`` tier, keyless).

Used by the live execution path to obtain swap quotes and pre-built
transactions. Paper mode does not depend on this client.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

JUPITER_BASE = "https://lite-api.jup.ag"
SOL_MINT = "So11111111111111111111111111111111111111112"


class JupiterError(RuntimeError):
    pass


class JupiterClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._limiter = RateLimiter(requests_per_second=1.0, burst=2)

    def get_quote(self, input_mint: str, output_mint: str, amount: int,
                  slippage_bps: int = 100) -> dict[str, Any]:
        """Quote for swapping ``amount`` (base units) of input for output."""
        self._limiter.acquire()
        try:
            response = self._session.get(f"{JUPITER_BASE}/swap/v1/quote", params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": str(slippage_bps),
            }, timeout=15)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise JupiterError(f"quote failed: {exc}") from exc

    def top_tokens(self, interval: str = "24h",
                   limit: int = 60) -> list[dict[str, Any]]:
        """Trending tokens with their stats — keyless, from the public
        tokens API. ``price_change_pct`` is a percentage (verified live).
        """
        self._limiter.acquire()
        try:
            response = self._session.get(
                f"{JUPITER_BASE}/tokens/v2/toptrending/{interval}",
                params={"limit": max(1, min(limit, 100))}, timeout=15)
            response.raise_for_status()
            items = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise JupiterError(f"trending fetch failed: {exc}") from exc
        tokens = []
        for item in items or []:
            mint = item.get("id")
            if not mint:
                continue
            stats = item.get(f"stats{interval}") or {}
            tokens.append({
                "mint": mint,
                "symbol": item.get("symbol") or "?",
                "liquidity_usd": float(item.get("liquidity") or 0.0),
                "price_change_pct": float(stats.get("priceChange") or 0.0),
                "txns_24h": int(stats.get("numBuys") or 0)
                + int(stats.get("numSells") or 0),
                "organic_score": float(item.get("organicScore") or 0.0),
                "verified": bool(item.get("isVerified")),
            })
        return tokens

    def build_swap_transaction(self, quote: dict[str, Any],
                               user_pubkey: str) -> str:
        """Return a base64-serialized unsigned swap transaction."""
        self._limiter.acquire()
        try:
            response = self._session.post(f"{JUPITER_BASE}/swap/v1/swap", json={
                "quoteResponse": quote,
                "userPublicKey": user_pubkey,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto",
            }, timeout=20)
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise JupiterError(f"swap build failed: {exc}") from exc
        transaction = body.get("swapTransaction")
        if not transaction:
            raise JupiterError(f"swap build returned no transaction: {body}")
        return transaction
