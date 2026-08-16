"""Birdeye Data Services client.

One job: the trader leaderboard. ``/trader/gainers-losers`` returns the
top-PnL wallets on Solana over a window — a ranked list of provably
profitable traders, which replaces blind pool sampling as the primary
candidate source. Requires a (free-tier) Birdeye API key; every failure
mode raises ``BirdeyeError`` so the caller can fall back to on-chain
sampling.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

BIRDEYE_BASE = "https://public-api.birdeye.so"


class BirdeyeError(RuntimeError):
    pass


class BirdeyeClient:
    def __init__(self, api_key: str) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "X-API-KEY": api_key,
            "x-chain": "solana",
            "accept": "application/json",
        })
        self._limiter = RateLimiter(requests_per_second=0.5, burst=2)

    def top_traders(self, window: str = "1W",
                    limit: int = 10) -> list[dict[str, Any]]:
        """Top-PnL wallet addresses for the window (e.g. "1W", "today").

        Returns dicts with at least ``address``; ``pnl``, ``volume`` and
        ``trade_count`` ride along when the API provides them.
        """
        self._limiter.acquire()
        try:
            response = self._session.get(
                f"{BIRDEYE_BASE}/trader/gainers-losers",
                params={"type": window, "sort_by": "PnL",
                        "sort_type": "desc", "offset": 0,
                        "limit": max(1, min(limit, 10))},
                timeout=15)
            if response.status_code in (401, 403):
                raise BirdeyeError(
                    f"gainers-losers not available on this API key "
                    f"(HTTP {response.status_code})")
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise BirdeyeError(f"leaderboard fetch failed: {exc}") from exc

        if not body.get("success", True):
            raise BirdeyeError(f"leaderboard request rejected: {body}")
        items = ((body.get("data") or {}).get("items")) or []
        traders = []
        for item in items:
            address = item.get("address") or item.get("owner")
            if not address:
                continue
            traders.append({
                "address": address,
                "pnl": item.get("pnl"),
                "volume": item.get("volume"),
                "trade_count": item.get("trade_count"),
            })
        return traders
