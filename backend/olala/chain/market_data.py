"""Market data via DexScreener (free, keyless).

Provides token-level price, liquidity, and market-cap snapshots with a
short-lived cache so daemons can query aggressively without exhausting the
upstream rate budget.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

from ..domain.models import TokenInfo
from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

DEXSCREENER_BASE = "https://api.dexscreener.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
CACHE_TTL_SEC = 45.0


class MarketDataService:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._limiter = RateLimiter(requests_per_second=4.0, burst=8)
        self._cache: dict[str, tuple[float, TokenInfo | None]] = {}
        self._lock = threading.Lock()

    def get_token_info(self, mint: str) -> TokenInfo | None:
        """Best (deepest SOL-quoted) pair snapshot for a token, cached."""
        with self._lock:
            cached = self._cache.get(mint)
            if cached and time.time() - cached[0] < CACHE_TTL_SEC:
                return cached[1]
        info = self._fetch_token_info(mint)
        with self._lock:
            self._cache[mint] = (time.time(), info)
        return info

    def search_winners(self, min_liquidity_usd: float,
                       min_change_pct: float,
                       limit: int = 8) -> list[dict]:
        """Fallback winner finder: SOL-quoted Solana pairs from the search
        endpoint, ranked by 24h price change. Used only when the primary
        trending source is down."""
        self._limiter.acquire()
        try:
            response = self._session.get(
                f"{DEXSCREENER_BASE}/latest/dex/search",
                params={"q": "SOL"}, timeout=15)
            response.raise_for_status()
            pairs = (response.json() or {}).get("pairs") or []
        except (requests.RequestException, ValueError) as exc:
            logger.warning("winner search failed: %s", exc)
            return []
        winners = {}
        for pair in pairs:
            if pair.get("chainId") != "solana":
                continue
            if (pair.get("quoteToken") or {}).get("address") != SOL_MINT:
                continue
            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0.0)
            change = float((pair.get("priceChange") or {}).get("h24") or 0.0)
            if liquidity < min_liquidity_usd or change < min_change_pct:
                continue
            base = pair.get("baseToken") or {}
            mint = base.get("address")
            if mint and (mint not in winners
                         or change > winners[mint]["price_change_pct"]):
                txns = pair.get("txns", {}).get("h24") or {}
                winners[mint] = {
                    "mint": mint, "symbol": base.get("symbol") or "?",
                    "liquidity_usd": liquidity,
                    "price_change_pct": change,
                    "txns_24h": int(txns.get("buys") or 0)
                    + int(txns.get("sells") or 0),
                    "organic_score": 0.0, "verified": False,
                }
        ranked = sorted(winners.values(),
                        key=lambda w: -w["price_change_pct"])
        return ranked[:limit]

    def _fetch_token_info(self, mint: str) -> TokenInfo | None:
        self._limiter.acquire()
        try:
            response = self._session.get(
                f"{DEXSCREENER_BASE}/latest/dex/tokens/{mint}", timeout=15)
            response.raise_for_status()
            pairs = (response.json() or {}).get("pairs") or []
        except (requests.RequestException, ValueError) as exc:
            logger.warning("dexscreener fetch failed for %s: %s", mint, exc)
            return None

        best: dict[str, Any] | None = None
        best_liquidity = -1.0
        for pair in pairs:
            if pair.get("chainId") != "solana":
                continue
            if (pair.get("quoteToken") or {}).get("address") != SOL_MINT:
                continue
            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0.0)
            if liquidity > best_liquidity:
                best, best_liquidity = pair, liquidity
        if not best:
            return None

        base = best.get("baseToken") or {}
        market_cap = float(best.get("marketCap") or best.get("fdv") or 0.0)
        return TokenInfo(
            mint=mint,
            symbol=base.get("symbol") or "?",
            name=base.get("name") or "",
            price_usd=float(best.get("priceUsd") or 0.0),
            price_sol=float(best.get("priceNative") or 0.0),
            liquidity_usd=best_liquidity,
            market_cap_usd=market_cap,
            pair_address=best.get("pairAddress") or "",
            dex=best.get("dexId") or "",
            pair_created_at=float(best.get("pairCreatedAt") or 0.0) / 1000.0,
        )
