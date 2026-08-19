"""SolanaTrackerClient: pagination, window widening, client-side caps.
The HTTP layer is stubbed; no network."""

import pytest

from olala.chain.solana_tracker import SolanaTrackerClient, SolanaTrackerError


def wallet(i, trades=100, days=25):
    return {"wallet": f"W{i:03d}" + "x" * 40, "winRate": 60.0,
            "period": {"realized": 1000.0 - i, "tradingDays": days},
            "counts": {"trades": trades}}


class StubSession:
    """Feeds scripted pages; records every request's params."""

    def __init__(self, pages):
        self.pages = pages          # list of (traders, next_cursor)
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append(dict(params))
        index = 0
        cursor = params.get("cursor")
        if cursor is not None:
            index = int(cursor)
        traders, next_cursor = self.pages[index]

        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self_inner):
                return {"traders": traders,
                        "pagination": {"nextCursor": next_cursor}}
        return R()


def client_with(pages):
    client = SolanaTrackerClient("test-key")
    client._session = StubSession(pages)
    client._limiter.acquire = lambda: None
    return client, client._session


def test_paginates_until_enough_keepers():
    # Page 0: all too fast. Page 1: 100 keepers.
    fast = [wallet(i, trades=25000) for i in range(100)]     # 1000/day
    slow = [wallet(100 + i, trades=100) for i in range(100)]  # 4/day
    client, session = client_with([(fast, "1"), (slow, None)])

    rows = client.top_traders(window_days=7, min_active_days=30,
                              max_trades_per_day=600, max_pages=5)

    assert len(session.requests) == 2          # stopped once satisfied
    assert len(rows) == 100
    assert all(r["trades_per_day"] <= 600 for r in rows)


def test_page_budget_is_respected():
    fast = [wallet(i, trades=25000) for i in range(100)]
    client, session = client_with([(fast, "1"), (fast, "2"), (fast, None)])

    rows = client.top_traders(max_trades_per_day=600, max_pages=2)

    assert len(session.requests) == 2
    assert rows == []


def test_window_widens_to_fit_min_active_days():
    client, session = client_with([([wallet(1)], None)])
    client.top_traders(window_days=7, min_active_days=30)
    sent = session.requests[0]
    # A 7-day board cannot hold 30 active days: the server returns an
    # empty set for that, so the client must nominate from the 30d board.
    assert sent["days"] == 30
    assert sent["minDays"] == 30


def test_window_not_widened_when_it_already_fits():
    client, session = client_with([([wallet(1)], None)])
    client.top_traders(window_days=90, min_active_days=30)
    assert session.requests[0]["days"] == 90


def test_first_page_failure_raises_later_pages_tolerated():
    class FailingSecondPage(StubSession):
        def get(self, url, params=None, timeout=None):
            if params.get("cursor") is not None:
                raise SolanaTrackerError("boom")
            return super().get(url, params, timeout)

    keep = [wallet(i) for i in range(40)]
    client, _ = client_with([(keep, "1")])
    client._session = FailingSecondPage([(keep, "1")])

    rows = client.top_traders(max_trades_per_day=600, max_pages=3, limit=100)
    assert len(rows) == 40      # partial result, not an exception


def test_quality_floors_are_sent_only_when_set():
    client, session = client_with([([wallet(1)], None)])
    client.top_traders(min_roi_pct=100.0, min_win_rate_pct=55.0)
    sent = session.requests[0]
    assert sent["minRoi"] == 100.0
    assert sent["minWinRate"] == 55.0

    client, session = client_with([([wallet(1)], None)])
    client.top_traders()          # zero = disabled, must not be sent
    assert "minRoi" not in session.requests[0]
    assert "minWinRate" not in session.requests[0]
