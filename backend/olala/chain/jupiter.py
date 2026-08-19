"""Jupiter aggregator client (free ``lite-api`` tier, keyless).

Used by the live execution path to obtain swap quotes and pre-built
transactions. Paper mode does not depend on this client.
"""

from __future__ import annotations

import logging
from typing import Any

from .http import HttpClient, HttpError
from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

JUPITER_BASE = "https://lite-api.jup.ag"


class JupiterError(HttpError):
    pass


class JupiterClient:
    def __init__(self, slippage_bps: int = 100) -> None:
        self._http = HttpClient(
            JUPITER_BASE, name="jupiter", error_cls=JupiterError,
            limiter=RateLimiter(requests_per_second=1.0, burst=2))
        self._slippage_bps = slippage_bps

    def get_quote(self, input_mint: str, output_mint: str, amount: int,
                  slippage_bps: int | None = None) -> dict[str, Any]:
        """Quote for swapping ``amount`` (base units) of input for output.

        Slippage defaults to the configured tolerance rather than a
        literal buried in this signature — it decides how much of a live
        swap the market may take.
        """
        tolerance = (self._slippage_bps if slippage_bps is None
                     else slippage_bps)
        return self._http.get("/swap/v1/quote", params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(tolerance),
        })

    def top_tokens(self, interval: str = "24h",
                   limit: int = 60) -> list[dict[str, Any]]:
        """Trending tokens with their stats — keyless, from the public
        tokens API. ``price_change_pct`` is a percentage (verified live).
        """
        items = self._http.get(
            f"/tokens/v2/toptrending/{interval}",
            params={"limit": max(1, min(limit, 100))})
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
        body = self._http.post("/swap/v1/swap", json={
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        }, timeout=20)
        transaction = (body or {}).get("swapTransaction")
        if not transaction:
            raise JupiterError(f"swap build returned no transaction: {body}")
        return transaction
