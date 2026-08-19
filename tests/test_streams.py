"""The two discovery streams, and the wall between them.

Stream A (leaderboard): a service already ranked and vetted these
wallets, so we follow what it returns. Stream B (on-chain): nobody
vetted these, so they earn a seat through pre-screen + deep scan +
the `filters` gate. `filters` must never reach Stream A.
"""

import time

import pytest

from olala.config import ConfigStore
from olala.discovery.scanner import RpcBudget
from olala.domain.models import TraderProfile, TraderStatus

from fakes import FakeMarketData, FakeProvider, FakeTracker
from test_discovery_v2 import make_daemon

NOMINEE = "NomineeAAAA111111111111111111111111111111111"


def store_with(tmp_path, body="", name="s.yaml"):
    path = tmp_path / name
    path.write_text("dev_mode: false\n" + body)
    return ConfigStore(path=path)


def budget_for(store):
    return RpcBudget(store.config.discovery.rpc_calls_per_scan)


def nominee(i=0, win_rate=0.7, trades=400, tpd=20.0):
    return {"address": f"Trusted{i:02d}111111111111111111111111111111",
            "win_rate": win_rate, "pnl_usd": 100000.0 - i,
            "trade_count": trades, "trades_per_day": tpd}


class RecordingTracker(FakeTracker):
    """Captures the parameters the leaderboard stream sends."""

    def __init__(self, traders=None):
        super().__init__(traders=traders)
        self.params = {}

    def top_traders(self, window_days=90, limit=100, min_trades=20,
                    min_active_days=0, sort="win_percentage",
                    max_trades_per_day=None, max_pages=1,
                    min_roi_pct=0.0, min_win_rate_pct=0.0):
        self.params = {"window_days": window_days, "min_trades": min_trades,
                       "min_active_days": min_active_days, "sort": sort,
                       "max_trades_per_day": max_trades_per_day,
                       "max_pages": max_pages, "min_roi_pct": min_roi_pct,
                       "min_win_rate_pct": min_win_rate_pct}
        return super().top_traders(window_days, limit, min_trades,
                                   min_active_days, sort,
                                   max_trades_per_day, max_pages,
                                   min_roi_pct, min_win_rate_pct)


# -- STREAM SEPARATION ----------------------------------------------------

def test_leaderboard_is_configured_by_its_own_section(tmp_path, db, bus):
    """`filters` governs on-chain admission and must not leak into the
    service request — that coupling was dropping vetted traders."""
    store = store_with(tmp_path,
        "filters_onchain:\n  min_trades: 999\n  max_trades_per_day: 7.0\n"
        "filters_solanatracker:\n  min_trades: 20\n  min_active_days: 12\n"
        "  window_days: 30\n  max_trades_per_day: 2000.0\n"
        "  sort: roi\n  min_roi_pct: 100.0\n  pages: 4\n")
    tracker = RecordingTracker(traders=[])
    _, _, daemon = make_daemon(db, bus, store, tracker=tracker)

    daemon._harvest_candidates(store.config, budget_for(store))

    board = store.config.filters_solanatracker
    assert tracker.params["min_trades"] == board.min_trades == 20
    assert tracker.params["min_active_days"] == board.min_active_days == 12
    assert tracker.params["window_days"] == board.window_days == 30
    assert tracker.params["sort"] == "roi"
    assert tracker.params["min_roi_pct"] == 100.0
    assert tracker.params["max_pages"] == 4
    # No on-chain filter value reached the service.
    assert 999 not in tracker.params.values()
    assert 7.0 not in tracker.params.values()


def test_filters_govern_the_onchain_stream_when_enabled(tmp_path, db, bus):
    """dev_mode: true APPLIES filters_onchain — the same knobs the
    leaderboard stream ignores are binding here."""
    path = tmp_path / "onchain.yaml"
    path.write_text("dev_mode: true\nfilters_onchain:\n  min_trades: 50\n")
    store = ConfigStore(path=path)
    provider, registry, daemon = make_daemon(db, bus, store)
    provider.signatures[NOMINEE] = [
        {"signature": f"s{i}", "err": None, "blockTime": time.time() - i * 60}
        for i in range(10)]      # only 10 signatures, needs 50 trades

    assert daemon.onchain._pre_screen(store.config, NOMINEE,
                                      RpcBudget(5)) is False
    assert registry.get(NOMINEE).status is TraderStatus.REJECTED
    assert "cannot hold" in registry.get(NOMINEE).rejection_reason


def test_filters_are_ignored_when_the_switch_is_off(tmp_path, db, bus):
    """dev_mode: false IGNORES filters_onchain — the same wallet walks
    straight through the pre-screen."""
    path = tmp_path / "off.yaml"
    path.write_text("dev_mode: false\nfilters_onchain:\n  min_trades: 50\n")
    store = ConfigStore(path=path)
    provider, registry, daemon = make_daemon(db, bus, store)
    provider.signatures[NOMINEE] = [
        {"signature": f"s{i}", "err": None, "blockTime": time.time() - i * 60}
        for i in range(10)]

    assert daemon.onchain._pre_screen(store.config, NOMINEE,
                                      RpcBudget(5)) is True
    assert registry.get(NOMINEE) is None      # nothing rejected


# -- STREAM A: take what the service gives --------------------------------

def test_leaderboard_names_are_followed_directly(tmp_path, db, bus):
    store = store_with(tmp_path, "discovery:\n  max_followed_traders: 3\n")
    tracker = FakeTracker(traders=[nominee(i) for i in range(5)])
    provider, registry, daemon = make_daemon(db, bus, store, tracker=tracker)

    daemon._harvest_candidates(store.config, budget_for(store))

    followed = registry.followed()
    assert len(followed) == 3
    for profile in followed:
        assert profile.assigned_wallet_id       # ready to trade
        assert profile.stats.total_trades == 400
        # Not one RPC call was spent qualifying them.
        assert provider.signature_reads_for(profile.address) == 0


def test_board_position_is_the_score(tmp_path, db, bus):
    """Score follows the configured ranking, not the win-rate field —
    a wallet that never sells its losers reports ~100%."""
    store = store_with(tmp_path, "discovery:\n  max_followed_traders: 5\n")
    top = nominee(0, win_rate=0.10)
    lower = nominee(1, win_rate=0.99)
    _, registry, daemon = make_daemon(db, bus, store,
                                      tracker=FakeTracker([top, lower]))

    daemon._harvest_candidates(store.config, budget_for(store))

    by_addr = {p.address: p for p in registry.followed()}
    assert by_addr[top["address"]].score > by_addr[lower["address"]].score


def test_activity_cap_is_mechanical_not_quality(tmp_path, db, bus):
    """We cannot copy — or afford — a wallet trading faster than the cap,
    so it is dropped even though the service vetted it."""
    store = store_with(tmp_path,
        "discovery:\n  max_followed_traders: 5\n"
        "filters_solanatracker:\n  max_trades_per_day: 100.0\n")
    fast = nominee(9, tpd=5000.0)
    _, registry, daemon = make_daemon(
        db, bus, store, tracker=FakeTracker([fast, nominee(1, tpd=20.0)]))

    daemon._harvest_candidates(store.config, budget_for(store))

    assert registry.get(fast["address"]) is None
    assert len(registry.followed()) == 1


def test_no_key_means_no_stream_a(tmp_path, db, bus):
    """The API key is the only switch: no key, no leaderboard stream."""
    store = store_with(tmp_path)
    _, registry, daemon = make_daemon(db, bus, store)   # tracker=None

    daemon._harvest_candidates(store.config, budget_for(store))

    assert daemon.leaderboard.available is False
    assert registry.followed() == []


# -- FALL-THROUGH: stream B runs no matter what ---------------------------

class RecordingMarket(FakeMarketData):
    def __init__(self):
        super().__init__()
        self.winner_searches = 0

    def search_winners(self, min_liquidity_usd, min_change_pct, limit=8):
        self.winner_searches += 1
        return []


@pytest.mark.parametrize("label,tracker,body", [
    ("service raises", FakeTracker(fail=True), ""),
    ("no key at all", None, ""),
    ("stream A throttled hard", FakeTracker(traders=[nominee()]),
     "filters_solanatracker:\n  interval_sec: 604800\n"),
    ("stream A throttled", FakeTracker(traders=[nominee()]),
     "filters_solanatracker:\n  interval_sec: 86400\n"),
])
def test_onchain_runs_regardless_of_stream_a(tmp_path, db, bus, label,
                                             tracker, body):
    store = store_with(tmp_path, body, name=f"{abs(hash(label))}.yaml")
    market = RecordingMarket()
    _, _, daemon = make_daemon(db, bus, store, tracker=tracker,
                               market=market)

    daemon._harvest_candidates(store.config, budget_for(store))
    daemon.leaderboard._last_poll_at = 0.0     # let A retry next sweep
    daemon._harvest_candidates(store.config, budget_for(store))

    # Two sweeps, two on-chain harvests: an external service can slow
    # discovery down, never stop it.
    assert market.winner_searches == 2, label


def test_failing_service_is_not_hammered(tmp_path, db, bus):
    store = store_with(tmp_path)
    tracker = FakeTracker(fail=True)
    _, _, daemon = make_daemon(db, bus, store, tracker=tracker)

    daemon._harvest_candidates(store.config, budget_for(store))
    daemon._harvest_candidates(store.config, budget_for(store))

    # A rate-limited service waits out its full interval, like a healthy
    # one — the poll window is marked before the request, not after.
    assert tracker.calls == 1


def test_polls_are_throttled(tmp_path, db, bus):
    store = store_with(tmp_path)
    tracker = FakeTracker(traders=[])
    _, _, daemon = make_daemon(db, bus, store, tracker=tracker)

    daemon._harvest_candidates(store.config, budget_for(store))
    daemon._harvest_candidates(store.config, budget_for(store))

    assert tracker.calls == 1


def test_no_key_means_stream_a_is_never_due(tmp_path, db, bus):
    store = store_with(tmp_path)
    _, _, daemon = make_daemon(db, bus, store)
    assert daemon.leaderboard.available is False
    assert daemon.leaderboard.due(store.config) is False


# -- config validation ----------------------------------------------------

def test_invalid_sort_is_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("filters_solanatracker:\n  sort: vibes\n")
    with pytest.raises(ValueError, match="filters_solanatracker.sort"):
        ConfigStore(path=path)

    good = ConfigStore(path=tmp_path / "good.yaml")
    with pytest.raises(ValueError, match="filters_solanatracker.sort"):
        good.update({"filters_solanatracker": {"sort": "vibes"}})
    assert good.config.filters_solanatracker.sort == "roi"   # unchanged


# -- bugs found in the post-refactor scan ---------------------------------

def test_zero_seat_roster_does_not_crash(tmp_path, db, bus):
    """`max_followed_traders: 0` used to raise on min() of an empty
    roster — a config value must never crash a sweep."""
    store = store_with(tmp_path, "discovery:\n  max_followed_traders: 0\n",
                       name="zero.yaml")
    _, registry, daemon = make_daemon(
        db, bus, store, tracker=FakeTracker([nominee(0)]))

    daemon._harvest_candidates(store.config, budget_for(store))

    assert registry.followed() == []


def test_a_failed_seat_never_shrinks_the_roster(tmp_path, db, bus):
    """claim_seat EVICTS to make room, so the caller must already be
    able to fill the seat — otherwise an eviction leaves it empty."""
    store = store_with(tmp_path, "discovery:\n  max_followed_traders: 2\n",
                       name="shrink.yaml")
    weak = [nominee(0, win_rate=0.1), nominee(1, win_rate=0.1)]
    _, registry, daemon = make_daemon(db, bus, store,
                                      tracker=FakeTracker(weak))
    daemon._harvest_candidates(store.config, budget_for(store))
    assert len(registry.followed()) == 2

    # A fresh board: every seat change must conserve the seat count.
    daemon.leaderboard._last_poll_at = 0.0
    daemon.leaderboard._tracker.traders = weak + [nominee(2, win_rate=0.99)]
    daemon._harvest_candidates(store.config, budget_for(store))

    assert len(registry.followed()) == 2


def test_seat_without_a_wallet_is_reported(tmp_path, db, bus, caplog):
    """A trader followed with no wallet can never trade; it must not
    fill a seat silently."""
    import logging
    store = store_with(tmp_path, name="nowallet.yaml")
    provider = FakeProvider()
    from olala.services.traders import TraderRegistry
    from olala.discovery.scanner import TraderDiscoveryDaemon
    from olala.chain.market_data import MarketDataService
    registry = TraderRegistry(db, bus)
    daemon = TraderDiscoveryDaemon(
        store, provider, FakeMarketData(), registry, db, bus,
        assign_wallet=lambda: "",                 # no wallets exist
        tracker=FakeTracker([nominee(0)]))

    with caplog.at_level(logging.WARNING):
        daemon._harvest_candidates(store.config, budget_for(store))

    assert any("cannot trade until" in r.message for r in caplog.records)


def test_progress_target_matches_the_real_depth_requirement(tmp_path, db,
                                                            bus):
    """The bar used to target min_history_days while the scan actually
    needed max(min_history_days, skill_window) * 1.1 — it read 100%
    while still running."""
    path = tmp_path / "depth.yaml"
    path.write_text("dev_mode: true\nfilters_onchain:\n  min_history_days: 7\n"
                    "discovery:\n  skill_window_days: 30\n")
    store = ConfigStore(path=path)
    _, registry, daemon = make_daemon(db, bus, store)
    registry.add_candidate(NOMINEE)
    events = bus.subscribe()

    daemon._publish_progress(store.config, NOMINEE, complete=False)

    payload = [events.get_nowait() for _ in range(events.qsize())]
    progress = [e for e in payload if e["type"] == "candidate_progress"][0]
    assert progress["data"]["target_days"] == pytest.approx(33.0)
