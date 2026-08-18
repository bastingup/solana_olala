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
                    min_trades: int = 20, min_active_days: int = 0,
                    sort: str = "win_percentage",
                    max_trades_per_day: float | None = None,
                    max_pages: int = 1) -> list[dict[str, Any]]:
        """Top wallets ranked by ``sort`` (``win_percentage``,
        ``realized`` PnL, or ``trades``), paginated until ``limit``
        KEEPERS are collected or ``max_pages`` is spent.

        ``min_trades`` and ``min_active_days`` are pushed to the service.
        The nomination window is widened to fit ``min_active_days`` —
        you cannot demand 30 active days inside a 7-day board (the
        server correctly returns an empty set for that), so persistence
        is enforced on a board at least that wide.

        The API exposes no maximum-activity filter — every server-side
        filter is a minimum — so ``max_trades_per_day`` is applied here,
        from payload data, at zero RPC cost. Pages beyond the first that
        fail are tolerated: whatever was collected is returned.
        """
        days = next((d for d in _SUPPORTED_WINDOW_DAYS if d <= window_days),
                    _SUPPORTED_WINDOW_DAYS[-1])
        if min_active_days > days:
            days = next((d for d in reversed(_SUPPORTED_WINDOW_DAYS)
                         if d >= min_active_days),
                        _SUPPORTED_WINDOW_DAYS[0])
        params: dict[str, Any] = {
            "sort": sort, "direction": "desc",
            "days": days, "limit": 100,
            "minTrades": max(1, min_trades),
            "excludeArbitrage": "true", "pnlMode": "strict",
        }
        if min_active_days > 0:
            params["minDays"] = min_active_days

        keepers: list[dict[str, Any]] = []
        cursor: str | None = None
        for page in range(max(1, max_pages)):
            if cursor:
                params["cursor"] = cursor
            try:
                body = self._fetch_page(params)
            except SolanaTrackerError:
                if page == 0:
                    raise
                logger.warning("leaderboard page %d failed; returning %d "
                               "keepers from %d page(s)", page + 1,
                               len(keepers), page)
                break
            for item in body.get("traders") or []:
                entry = self._parse(item)
                if entry is None:
                    continue
                rate = entry["trades_per_day"]
                if (max_trades_per_day is not None and rate is not None
                        and rate > max_trades_per_day):
                    continue
                keepers.append(entry)
            cursor = (body.get("pagination") or {}).get("nextCursor")
            if len(keepers) >= limit or not cursor:
                break
        return keepers[:limit]

    def _fetch_page(self, params: dict[str, Any]) -> dict[str, Any]:
        self._limiter.acquire()
        try:
            response = self._session.get(
                f"{TRACKER_BASE}/v2/pnl/leaderboard/top",
                params=params, timeout=15)
            if response.status_code in (401, 403):
                raise SolanaTrackerError(
                    f"leaderboard not available on this API key "
                    f"(HTTP {response.status_code})")
            if response.status_code == 429:
                raise SolanaTrackerError(
                    "rate limited by Solana Tracker (HTTP 429)")
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise SolanaTrackerError(
                f"leaderboard fetch failed: {exc}") from exc

    @staticmethod
    def _parse(item: dict[str, Any]) -> dict[str, Any] | None:
        address = item.get("wallet")
        if not address:
            return None
        win_rate = item.get("winRate")
        period = item.get("period") or {}
        counts = item.get("counts") or {}
        trades = counts.get("trades")
        active_days = period.get("tradingDays")
        return {
            "address": address,
            "win_rate": (win_rate / 100.0
                         if isinstance(win_rate, (int, float)) else None),
            "pnl_usd": period.get("realized"),
            "trade_count": trades,
            "trades_per_day": (trades / active_days
                               if trades and active_days else None),
        }
