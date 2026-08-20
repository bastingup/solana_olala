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
on-chain winners' holders source.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .http import HttpClient, HttpError
from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

TRACKER_BASE = "https://data.solanatracker.io"

# The leaderboard window only supports these rolling spans.
_SUPPORTED_WINDOW_DAYS = (90, 30, 7, 1)

# MEASURED: the service honours `limit` up to 500 and silently caps
# there (1000 returns 500). At 100 we spent five requests per 500
# wallets against a 10,000/month allowance.
MAX_PAGE_SIZE = 500


def _tradable(entry: dict[str, Any], max_trades_per_day: float | None,
              min_avg_buy_usd: float, max_last_trade_age_sec: float,
              now: float, min_volume_usd: float = 0.0,
              require_closed_trades: bool = False) -> bool:
    """Can we actually copy this wallet, at our size and our latency?

    Purely payload data — no extra request, no RPC.
    """
    rate = entry["trades_per_day"]
    if (max_trades_per_day is not None and rate is not None
            and rate > max_trades_per_day):
        return False
    if require_closed_trades and not (entry.get("closed_trades") or 0):
        # No completed round trip means the service has nothing to
        # measure: its win rate is absent and its realized PnL cannot be
        # checked against anything. Not a bad trader — an unverifiable
        # one, which is worse to act on.
        return False
    if min_volume_usd > 0:
        volume = entry.get("volume_usd")
        # A wallet earning on dust cannot show volume: the pools will
        # not absorb it. Real volume at a human trade count means the
        # positions were big, which means the tokens were not dust.
        if volume is None or volume < min_volume_usd:
            return False
    if min_avg_buy_usd > 0:
        avg_buy = entry.get("avg_buy_usd")
        # Unknown size is not evidence of a big one. A wallet we cannot
        # size up is exactly the kind that turns out to trade dust.
        if avg_buy is None or avg_buy < min_avg_buy_usd:
            return False
    if max_last_trade_age_sec > 0:
        last = entry.get("last_trade_at")
        if last is None or (now - last) > max_last_trade_age_sec:
            return False
    return True


class SolanaTrackerError(HttpError):
    pass


class SolanaTrackerClient:
    def __init__(self, api_key: str) -> None:
        self._http = HttpClient(
            TRACKER_BASE, name="solanatracker",
            error_cls=SolanaTrackerError,
            headers={"x-api-key": api_key},
            limiter=RateLimiter(requests_per_second=1.0, burst=1))

    def top_traders(self, window_days: int = 90, limit: int = 100,
                    min_trades: int = 20, min_active_days: int = 0,
                    sort: str = "win_percentage",
                    max_trades_per_day: float | None = None,
                    max_pages: int = 1, min_roi_pct: float = 0.0,
                    min_win_rate: float = 0.0,
                    page_size: int = MAX_PAGE_SIZE,
                    min_avg_buy_usd: float = 0.0,
                    max_last_trade_age_sec: float = 0.0,
                    min_volume_usd: float = 0.0,
                    require_closed_trades: bool = False
                    ) -> list[dict[str, Any]]:
        """Top wallets ranked by ``sort`` (``win_percentage``,
        ``realized`` PnL, or ``trades``), paginated until ``limit``
        KEEPERS are collected or ``max_pages`` is spent.

        ``min_trades`` and ``min_active_days`` are pushed to the service.
        The nomination window is widened to fit ``min_active_days`` —
        you cannot demand 30 active days inside a 7-day board (the
        server correctly returns an empty set for that), so persistence
        is enforced on a board at least that wide.

        Every server-side filter the API offers is a MINIMUM, so the
        three ceilings we care about are applied here from payload data,
        at zero extra cost:

        ``max_trades_per_day``
            the speed past which we cannot copy a trader at our latency;
        ``min_avg_buy_usd``
            a proxy for pool depth. The board carries the trader's
            average buy in dollars, and a wallet that routinely spends
            $14 a trade is working in pools that cannot absorb our
            order — measured live, that is the p10 of this board;
        ``max_last_trade_age_sec``
            recency. A 30-day PnL ranking happily returns wallets that
            stopped trading a week ago, and a dormant trader occupies a
            seat that copies nothing.

        Pages beyond the first that fail are tolerated: whatever was
        collected is returned.
        """
        days = next((d for d in _SUPPORTED_WINDOW_DAYS if d <= window_days),
                    _SUPPORTED_WINDOW_DAYS[-1])
        if min_active_days > days:
            days = next((d for d in reversed(_SUPPORTED_WINDOW_DAYS)
                         if d >= min_active_days),
                        _SUPPORTED_WINDOW_DAYS[0])
        params: dict[str, Any] = {
            "sort": sort, "direction": "desc",
            "days": days,
            # MEASURED: the service serves up to 500 per request and
            # caps there. Asking for 100 cost five times the requests
            # for the same wallets, against a 10k/month allowance.
            "limit": max(1, min(int(page_size), MAX_PAGE_SIZE)),
            "minTrades": max(1, min_trades),
            "excludeArbitrage": "true", "pnlMode": "strict",
        }
        if min_active_days > 0:
            params["minDays"] = min_active_days
        # Quality floors, in the percent units the API expects. minRoi is
        # the one that matters most: it demands return on capital, which
        # volume machines cannot fake.
        if min_roi_pct > 0:
            params["minRoi"] = min_roi_pct
        if min_win_rate > 0:
            # Caller speaks fractions; the API speaks percent.
            params["minWinRate"] = min_win_rate * 100.0

        keepers: list[dict[str, Any]] = []
        cursor: str | None = None
        now = time.time()
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
                if not _tradable(entry, max_trades_per_day, min_avg_buy_usd,
                                 max_last_trade_age_sec, now,
                                 min_volume_usd, require_closed_trades):
                    continue
                keepers.append(entry)
            cursor = (body.get("pagination") or {}).get("nextCursor")
            if len(keepers) >= limit or not cursor:
                break
        return keepers[:limit]

    def _fetch_page(self, params: dict[str, Any]) -> dict[str, Any]:
        # Status handling — including feeding a 429 back to our own
        # bucket, which this client used to skip — lives in HttpClient.
        return self._http.get("/v2/pnl/leaderboard/top", params=params) or {}

    def wallet_trades(self, wallet: str,
                      limit: int = 100) -> list[dict[str, Any]]:
        """Recent trades for one wallet, as the service reconstructed them.

        Not used by the trading path — this is the independent second
        opinion the tracker is reconciled against, so a missed copy shows
        up as a discrepancy rather than as silence.
        """
        body = self._http.get(f"/wallet/{wallet}/trades") or {}
        trades = body.get("trades")
        return list(trades)[:limit] if isinstance(trades, list) else []

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
        averages = item.get("averages") or {}
        last_trade_ms = (item.get("timing") or {}).get("lastTrade")
        return {
            "address": address,
            "win_rate": (win_rate / 100.0
                         if isinstance(win_rate, (int, float)) else None),
            "pnl_usd": period.get("realized"),
            "trade_count": trades,
            "trades_per_day": (trades / active_days
                               if trades and active_days else None),
            # The trader's typical BUY in dollars. This is the closest
            # thing the board gives us to pool depth: a wallet that
            # routinely puts $500 into a token is working in pools that
            # absorb $500, while one averaging $14 is in dust. Our own
            # order has to fit the same pools.
            "avg_buy_usd": (float(averages["buy"])
                            if isinstance(averages.get("buy"), (int, float))
                            else None),
            "avg_sell_usd": (float(averages["sell"])
                             if isinstance(averages.get("sell"), (int, float))
                             else None),
            "tokens_traded": counts.get("tokensTraded"),
            # Capital actually deployed over the window. A trader who
            # makes money on dust CANNOT show real volume — the pools
            # will not take it — so volume separates "found a $2k
            # rugpull" from "works in tokens that can absorb size".
            "volume_usd": (float(item["invested"])
                           if isinstance(item.get("invested"), (int, float))
                           else None),
            # Closed round trips. Without at least one the service
            # cannot compute a win rate and its `realized` figure is
            # unverifiable — MEASURED: wallets reporting $2.3M realized
            # on $71 invested all have closed == 0.
            "closed_trades": ((item.get("tokens") or {}).get("closed")),
            # Milliseconds on the wire; seconds everywhere in our code.
            "last_trade_at": (last_trade_ms / 1000.0
                              if isinstance(last_trade_ms, (int, float))
                              else None),
        }
