"""Solana Tracker Data API client.

One job: the PnL leaderboard. ``/v2/pnl/leaderboard/top`` returns wallets
ranked by windowed, pre-computed PnL with win rates, arbitrage bots
excluded upstream — a candidate source that has already looked at every
active wallet so we do not have to. The service only NOMINATES: our own
on-chain reconstruction still judges every candidate before it is
followed.

Requires a free-tier API key (10k requests/month, 3 req/s). Every
failure mode — missing entitlement, rate limit, outage, malformed body —
raises ``SolanaTrackerError`` so the caller can fall through to the
on-chain census and winners' holders sources.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

TRACKER_BASE = "https://data.solanatracker.io"

# The leaderboard window only supports these rolling spans.
_SUPPORTED_WINDOW_DAYS = (90, 30, 7, 1)


class SolanaTrackerError(RuntimeError):
    pass


class SolanaTrackerClient:
    def __init__(self, api_key: str) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "x-api-key": api_key,
            "accept": "application/json",
        })
        self._limiter = RateLimiter(requests_per_second=1.0, burst=1)

    def top_traders(self, window_days: int = 90, limit: int = 100,
                    min_trades: int = 20) -> list[dict[str, Any]]:
        """Best-win-rate wallets over the rolling window.

        Returns dicts with ``address`` and, when provided, ``win_rate``
        (0..1), ``pnl_usd`` and ``trade_count``.
        """
        days = next((d for d in _SUPPORTED_WINDOW_DAYS if d <= window_days),
                    _SUPPORTED_WINDOW_DAYS[-1])
        self._limiter.acquire()
        try:
            response = self._session.get(
                f"{TRACKER_BASE}/v2/pnl/leaderboard/top",
                params={
                    "sort": "win_percentage", "direction": "desc",
                    "days": days, "limit": max(1, min(limit, 100)),
                    "minTrades": max(1, min_trades),
                    "excludeArbitrage": "true", "pnlMode": "strict",
                }, timeout=15)
            if response.status_code in (401, 403):
                raise SolanaTrackerError(
                    f"leaderboard not available on this API key "
                    f"(HTTP {response.status_code})")
            if response.status_code == 429:
                raise SolanaTrackerError(
                    "rate limited by Solana Tracker (HTTP 429)")
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise SolanaTrackerError(
                f"leaderboard fetch failed: {exc}") from exc

        traders = []
        for item in body.get("traders") or []:
            address = item.get("wallet")
            if not address:
                continue
            win_rate = item.get("winRate")
            period = item.get("period") or {}
            counts = item.get("counts") or {}
            traders.append({
                "address": address,
                "win_rate": (win_rate / 100.0
                             if isinstance(win_rate, (int, float)) else None),
                "pnl_usd": period.get("realized"),
                "trade_count": counts.get("trades"),
            })
        return traders
