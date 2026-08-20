"""SolanaTrackerClient: pagination, window widening, client-side caps.
The HTTP layer is stubbed; no network."""

import pytest

from olala.chain.solana_tracker import SolanaTrackerClient, SolanaTrackerError


def wallet(i, trades=100, days=25):
    return {"wallet": f"W{i:03d}" + "x" * 40, "winRate": 60.0,
            "period": {"realized": 1000.0 - i, "tradingDays": days},
            "counts": {"trades": trades}}


class StubHttp:
    """Feeds scripted pages; records every request's params.

    Stubs the shared ``HttpClient``, not ``requests`` — status handling,
    throttle feedback and JSON decoding are that class's job now and are
    covered by ``test_http_client.py``. What matters here is the
    pagination and parameter logic layered on top.
    """

    def __init__(self, pages):
        self.pages = pages          # list of (traders, next_cursor)
        self.requests = []

    def get(self, path, params=None, timeout=None):
        self.requests.append(dict(params))
        index = int(params.get("cursor") or 0)
        traders, next_cursor = self.pages[index]
        return {"traders": traders,
                "pagination": {"nextCursor": next_cursor}}


def client_with(pages):
    client = SolanaTrackerClient("test-key")
    client._http = StubHttp(pages)
    return client, client._http


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
    class FailingSecondPage(StubHttp):
        def get(self, path, params=None, timeout=None):
            if params.get("cursor") is not None:
                raise SolanaTrackerError("boom")
            return super().get(path, params, timeout)

    keep = [wallet(i) for i in range(40)]
    client, _ = client_with([(keep, "1")])
    client._http = FailingSecondPage([(keep, "1")])

    rows = client.top_traders(max_trades_per_day=600, max_pages=3, limit=100)
    assert len(rows) == 40      # partial result, not an exception


def test_quality_floors_are_sent_only_when_set():
    """Win rate is a FRACTION on our side and a PERCENT on the wire —
    the conversion is the whole point (0.7 must not mean 0.7%)."""
    client, session = client_with([([wallet(1)], None)])
    client.top_traders(min_roi_pct=100.0, min_win_rate=0.55)
    sent = session.requests[0]
    assert sent["minRoi"] == 100.0
    assert sent["minWinRate"] == pytest.approx(55.0)

    client, session = client_with([([wallet(1)], None)])
    client.top_traders()          # zero = disabled, must not be sent
    assert "minRoi" not in session.requests[0]
    assert "minWinRate" not in session.requests[0]


# -- tradability: can we actually fill alongside this wallet? --------------
#
# The board ranks by PnL, which says nothing about whether OUR order can
# be filled. Two real buys were refused by the risk engine because the
# pool held $0 — the trader could work there, we could not. These
# filters use payload data the board already sends, at zero extra cost.

import time as _time


def board_entry(i, *, tpd=20.0, avg_buy=500.0, last_trade_age_h=1.0):
    return {"wallet": f"W{i:03d}" + "x" * 40, "winRate": 90.0,
            "period": {"realized": 1000.0 - i, "tradingDays": 25},
            "counts": {"trades": int(tpd * 25), "tokensTraded": 40},
            "averages": {"buy": avg_buy, "sell": avg_buy * 1.2},
            "timing": {"lastTrade":
                       (_time.time() - last_trade_age_h * 3600) * 1000}}


def test_average_buy_and_last_trade_are_parsed():
    client, session = client_with([([board_entry(1)], None)])
    row = client.top_traders()[0]
    assert row["avg_buy_usd"] == 500.0
    assert row["avg_sell_usd"] == 600.0
    assert row["tokens_traded"] == 40
    assert _time.time() - row["last_trade_at"] < 4000


def test_wallets_trading_below_our_size_are_dropped():
    """A wallet averaging $14 a buy works in pools that cannot absorb
    our order — measured, that is the 10th percentile of this board."""
    entries = [board_entry(1, avg_buy=14.0), board_entry(2, avg_buy=800.0)]
    client, _ = client_with([(entries, None)])
    rows = client.top_traders(min_avg_buy_usd=100.0)
    assert [r["avg_buy_usd"] for r in rows] == [800.0]


def test_an_unknown_average_buy_is_not_treated_as_a_big_one():
    entries = [{"wallet": "W" + "y" * 43, "winRate": 90.0,
                "period": {"realized": 1.0, "tradingDays": 10},
                "counts": {"trades": 100}}]
    client, _ = client_with([(entries, None)])
    assert client.top_traders(min_avg_buy_usd=100.0) == []


def test_dormant_wallets_are_dropped():
    """A 30-day PnL board happily returns wallets that stopped trading a
    week ago; a dormant trader holds a seat and copies nothing."""
    entries = [board_entry(1, last_trade_age_h=200.0),
               board_entry(2, last_trade_age_h=0.5)]
    client, _ = client_with([(entries, None)])
    rows = client.top_traders(max_last_trade_age_sec=24 * 3600)
    assert [r["address"][:4] for r in rows] == ["W002"]


def test_zero_disables_each_tradability_gate():
    entries = [board_entry(1, avg_buy=1.0, last_trade_age_h=1000.0)]
    client, _ = client_with([(entries, None)])
    assert len(client.top_traders(min_avg_buy_usd=0.0,
                                  max_last_trade_age_sec=0.0)) == 1


def test_page_size_is_sent_and_capped_at_the_measured_maximum():
    """MEASURED: the service serves up to 500 and caps there. Asking for
    100 spent five requests per 500 wallets against a 10k allowance."""
    from olala.chain.solana_tracker import MAX_PAGE_SIZE

    client, session = client_with([([board_entry(1)], None)])
    client.top_traders(page_size=500)
    assert session.requests[0]["limit"] == 500

    client, session = client_with([([board_entry(1)], None)])
    client.top_traders(page_size=5000)
    assert session.requests[0]["limit"] == MAX_PAGE_SIZE
