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


def nominee(i=0, win_rate=0.7, trades=400, tpd=20.0, avg_buy_usd=500.0,
            last_trade_at=None, volume_usd=250_000.0, closed_trades=200,
            tokens_per_day=2.0, trading_days=30, profitable_days=20):
    """A board entry. Defaults are QUALIFIED and TRADABLE — real volume,
    closed round trips, a recent last trade, low token churn and
    consistent green days — so a test about seating is not silently
    emptied by the clean-active gates."""
    return {"address": f"Trusted{i:02d}111111111111111111111111111111",
            "win_rate": win_rate, "pnl_usd": 100000.0 - i,
            "trade_count": trades, "trades_per_day": tpd,
            "avg_buy_usd": avg_buy_usd, "volume_usd": volume_usd,
            "closed_trades": closed_trades,
            "tokens_per_day": tokens_per_day, "trading_days": trading_days,
            "profitable_days": profitable_days,
            "last_trade_at": (time.time() if last_trade_at is None
                              else last_trade_at)}


class RecordingTracker(FakeTracker):
    """Captures the parameters the leaderboard stream sends."""

    def __init__(self, traders=None):
        super().__init__(traders=traders)
        self.params = {}

    def top_traders(self, window_days=90, limit=100, min_trades=20,
                    min_active_days=0, sort="win_percentage",
                    max_trades_per_day=None, max_pages=1,
                    min_roi_pct=0.0, min_win_rate=0.0,
                    page_size=500, min_avg_buy_usd=0.0,
                    max_last_trade_age_sec=0.0, min_volume_usd=0.0,
                    require_closed_trades=False,
                    min_trades_per_day=0.0, max_tokens_per_day=0.0,
                    max_win_rate=1.0, min_profitable_days_ratio=0.0):
        self.params = {"window_days": window_days, "min_trades": min_trades,
                       "min_active_days": min_active_days, "sort": sort,
                       "max_trades_per_day": max_trades_per_day,
                       "max_pages": max_pages, "min_roi_pct": min_roi_pct,
                       "min_win_rate": min_win_rate, "limit": limit,
                       "page_size": page_size,
                       "min_avg_buy_usd": min_avg_buy_usd,
                       "max_last_trade_age_sec": max_last_trade_age_sec,
                       "min_volume_usd": min_volume_usd,
                       "require_closed_trades": require_closed_trades,
                       "min_trades_per_day": min_trades_per_day,
                       "max_tokens_per_day": max_tokens_per_day,
                       "max_win_rate": max_win_rate,
                       "min_profitable_days_ratio": min_profitable_days_ratio}
        return super().top_traders(
            window_days, limit, min_trades, min_active_days, sort,
            max_trades_per_day, max_pages, min_roi_pct, min_win_rate,
            page_size, min_avg_buy_usd, max_last_trade_age_sec,
            min_volume_usd, require_closed_trades, min_trades_per_day,
            max_tokens_per_day, max_win_rate, min_profitable_days_ratio)


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


def test_seated_stats_carry_the_boards_real_last_trade(tmp_path, db, bus):
    """Stamping last_trade_at to now made every seated trader read
    `inactive_hours: 0` forever — the one number that would show a quiet
    roster, hardcoded to lie. It must carry the board's real value."""
    store = store_with(tmp_path, "discovery:\n  max_followed_traders: 3\n")
    six_hours_ago = time.time() - 6 * 3600
    tracker = FakeTracker(traders=[nominee(i, last_trade_at=six_hours_ago)
                                   for i in range(3)])
    _, registry, daemon = make_daemon(db, bus, store, tracker=tracker)

    daemon._harvest_candidates(store.config, budget_for(store))

    for profile in registry.followed():
        assert profile.stats.last_trade_at == pytest.approx(six_hours_ago)
        # ~6 hours idle, not a fabricated zero.
        assert 5.5 < profile.stats.inactive_hours < 6.5
        # first_trade_at anchors to the board window, so trades/day is a
        # real figure rather than a division by a zero first-trade.
        assert profile.stats.trades_per_day > 0


def test_freshness_updates_when_a_seated_trader_trades_again(tmp_path, db, bus):
    """A later sweep reporting a NEWER last-trade must refresh the stored
    stats even when the board rank is unchanged, so the freshness display
    tracks reality instead of freezing at the admission-day value."""
    store = store_with(tmp_path, "discovery:\n  max_followed_traders: 1\n")
    entry = nominee(0, last_trade_at=time.time() - 6 * 3600)
    tracker = FakeTracker(traders=[entry])
    _, registry, daemon = make_daemon(db, bus, store, tracker=tracker)
    daemon._harvest_candidates(store.config, budget_for(store))
    assert registry.get(entry["address"]).stats.inactive_hours > 5

    # Same trader, same rank, but it just traded — and the throttle is
    # cleared so the next sweep re-polls.
    entry["last_trade_at"] = time.time() - 60
    daemon.leaderboard._last_poll_at = 0.0
    daemon._harvest_candidates(store.config, budget_for(store))

    assert registry.get(entry["address"]).stats.inactive_hours < 1


def test_composite_score_favours_the_better_trader(tmp_path, db, bus):
    """Score is a COMPOSITE of win rate, consistency and size — not raw
    board position. Once the clean-active gates have removed bots and
    rugs, the seats should go to the strongest of what remains."""
    store = store_with(tmp_path, "discovery:\n  max_followed_traders: 5\n")
    # Same size and activity; the better trader wins more often and more
    # consistently. It must score higher regardless of board order.
    better = nominee(0, win_rate=0.90, profitable_days=27)
    worse = nominee(1, win_rate=0.62, profitable_days=12)
    _, registry, daemon = make_daemon(db, bus, store,
                                      tracker=FakeTracker([worse, better]))

    daemon._harvest_candidates(store.config, budget_for(store))

    by_addr = {p.address: p for p in registry.followed()}
    assert by_addr[better["address"]].score > by_addr[worse["address"]].score


def test_a_never_sell_losers_wallet_is_excluded_by_the_win_cap(tmp_path,
                                                               db, bus):
    """A ~100% win rate is the 'never realise a loser' tell. With the
    upper cap on, that wallet must not be seated."""
    store = store_with(
        tmp_path,
        "discovery:\n  max_followed_traders: 5\n"
        "filters_solanatracker:\n  max_win_rate: 0.97\n")
    fake = nominee(0, win_rate=1.00)
    real = nominee(1, win_rate=0.85)
    _, registry, daemon = make_daemon(db, bus, store,
                                      tracker=FakeTracker([fake, real]))

    daemon._harvest_candidates(store.config, budget_for(store))

    seated = {p.address for p in registry.followed()}
    assert real["address"] in seated
    assert fake["address"] not in seated


def test_a_sniper_is_excluded_by_the_tokens_per_day_cap(tmp_path, db, bus):
    """A snipe-and-dump wallet churns many distinct tokens per day; a real
    trader concentrates. The tokens/day cap separates them."""
    store = store_with(
        tmp_path,
        "discovery:\n  max_followed_traders: 5\n"
        "filters_solanatracker:\n  max_tokens_per_day: 8.0\n")
    sniper = nominee(0, tokens_per_day=40.0)
    real = nominee(1, tokens_per_day=2.0)
    _, registry, daemon = make_daemon(db, bus, store,
                                      tracker=FakeTracker([sniper, real]))

    daemon._harvest_candidates(store.config, budget_for(store))

    seated = {p.address for p in registry.followed()}
    assert real["address"] in seated
    assert sniper["address"] not in seated


def test_an_inconsistent_wallet_is_excluded_by_the_profitable_days_gate(
        tmp_path, db, bus):
    """One lucky pump is not an edge. Requiring a fraction of green days
    drops the wallet that made its money on a single day."""
    store = store_with(
        tmp_path,
        "discovery:\n  max_followed_traders: 5\n"
        "filters_solanatracker:\n  min_profitable_days_ratio: 0.5\n")
    lucky = nominee(0, trading_days=30, profitable_days=4)   # 13% green
    real = nominee(1, trading_days=30, profitable_days=22)   # 73% green
    _, registry, daemon = make_daemon(db, bus, store,
                                      tracker=FakeTracker([lucky, real]))

    daemon._harvest_candidates(store.config, budget_for(store))

    seated = {p.address for p in registry.followed()}
    assert real["address"] in seated
    assert lucky["address"] not in seated


def test_a_too_slow_wallet_is_excluded_by_the_activity_floor(tmp_path,
                                                             db, bus):
    """An activity FLOOR keeps the roster trading: a wallet under the
    minimum trades/day is too slow to be worth a seat."""
    store = store_with(
        tmp_path,
        "discovery:\n  max_followed_traders: 5\n"
        "filters_solanatracker:\n  min_trades_per_day: 4.0\n")
    slow = nominee(0, tpd=1.0)
    active = nominee(1, tpd=8.0)
    _, registry, daemon = make_daemon(db, bus, store,
                                      tracker=FakeTracker([slow, active]))

    daemon._harvest_candidates(store.config, budget_for(store))

    seated = {p.address for p in registry.followed()}
    assert active["address"] in seated
    assert slow["address"] not in seated


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
        assign_wallet=lambda *_a: "",             # no wallets exist
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


def test_win_rate_is_a_fraction_everywhere(tmp_path, db, bus):
    """0.7 must mean 70%, not 0.7%. Mixed units once seated traders
    winning one trade in five."""
    path = tmp_path / "wr.yaml"
    path.write_text("filters_solanatracker:\n  min_win_rate: 0.7\n")
    store = ConfigStore(path=path)
    tracker = RecordingTracker(traders=[])
    _, _, daemon = make_daemon(db, bus, store, tracker=tracker)

    daemon._harvest_candidates(store.config, budget_for(store))

    assert tracker.params["min_win_rate"] == 0.7      # fraction on our side


def test_a_percent_typo_is_rejected_at_load(tmp_path):
    """55 in a fraction field means 5500% — refuse it with the fix."""
    path = tmp_path / "pct.yaml"
    path.write_text("filters_solanatracker:\n  min_win_rate: 55.0\n")
    with pytest.raises(ValueError, match="did you mean 0.55"):
        ConfigStore(path=path)

    path2 = tmp_path / "pct2.yaml"
    path2.write_text("filters_onchain:\n  min_win_rate: 80\n")
    with pytest.raises(ValueError, match="FRACTION"):
        ConfigStore(path=path2)


def test_a_fraction_typo_in_roi_is_rejected(tmp_path):
    """0.5 in a percent field means half a percent — filters nothing."""
    path = tmp_path / "roi.yaml"
    path.write_text("filters_solanatracker:\n  min_roi_pct: 0.5\n")
    with pytest.raises(ValueError, match="did you mean 50"):
        ConfigStore(path=path)


# -- the watchlist: traders of interest we are not (yet) following --------
#
# Qualifying and being seated are different things. A wallet that clears
# every bar but arrives at a full roster is a trader of INTEREST, and it
# must stay one: the old code skipped any known address outright, so a
# wallet rejected once for a full roster could never be reconsidered
# however much it improved, and the candidate pool could only shrink.

def test_qualified_wallets_without_a_seat_are_kept_as_candidates(
        tmp_path, db, bus):
    store = store_with(tmp_path, "discovery:\n  max_followed_traders: 2\n")
    board = [nominee(i, tpd=10.0) for i in range(6)]
    _, registry, daemon = make_daemon(db, bus, store,
                                      tracker=FakeTracker(board))
    daemon.leaderboard.harvest(store.config)

    assert len(registry.by_status(TraderStatus.FOLLOWED)) == 2
    # Kept as traders of interest, not written off.
    assert len(registry.by_status(TraderStatus.CANDIDATE)) == 4
    assert registry.by_status(TraderStatus.REJECTED) == []


def test_a_watchlisted_wallet_can_take_a_seat_on_a_later_sweep(
        tmp_path, db, bus):
    """The whole point: today's numbers get it reconsidered. Under
    composite scoring, a wallet earns the seat by IMPROVING its record,
    not by moving up a board sorted on something else."""
    store = store_with(tmp_path, "discovery:\n  max_followed_traders: 1\n")
    # `weak` is the better trader at first and takes the only seat.
    weak = nominee(9, win_rate=0.80, profitable_days=20)
    strong_early = nominee(0, win_rate=0.64, profitable_days=13)
    tracker = FakeTracker([weak, strong_early])
    _, registry, daemon = make_daemon(db, bus, store, tracker=tracker)

    daemon.leaderboard.harvest(store.config)
    assert registry.get(weak["address"]).status is TraderStatus.FOLLOWED
    assert registry.get(strong_early["address"]).status \
        is TraderStatus.CANDIDATE

    # Next sweep: the watchlisted wallet's record has clearly improved and
    # must now displace the incumbent.
    tracker.traders = [nominee(0, win_rate=0.96, profitable_days=28), weak]
    daemon.leaderboard._last_poll_at = 0.0
    daemon.leaderboard.harvest(store.config)
    assert registry.get(strong_early["address"]).status \
        is TraderStatus.FOLLOWED


def test_a_watchlisted_wallets_numbers_are_refreshed(tmp_path, db, bus):
    """Tier 2 of the plan — "has anything changed about our traders of
    interest?" — is free: the same sweep already carries their stats, and
    the composite score moves when the record does."""
    store = store_with(tmp_path, "discovery:\n  max_followed_traders: 1\n")
    tracker = FakeTracker([nominee(0), nominee(1)])
    _, registry, daemon = make_daemon(db, bus, store, tracker=tracker)
    daemon.leaderboard.harvest(store.config)
    watched = registry.by_status(TraderStatus.CANDIDATE)[0]
    before = watched.score

    # Both wallets' win rates jump on the next sweep; the watched one's
    # stored score must follow.
    tracker.traders = [nominee(0, win_rate=0.95, profitable_days=28),
                       nominee(1, win_rate=0.95, profitable_days=28)]
    daemon.leaderboard._last_poll_at = 0.0
    daemon.leaderboard.harvest(store.config)
    assert registry.get(watched.address).score != before


# -- the quality gates ----------------------------------------------------

def test_wallets_without_a_closed_round_trip_are_refused(tmp_path, db, bus):
    """MEASURED: every wallet the service reports at 0% win rate has zero
    closed positions — missing data, not a losing trader — and their
    realized PnL is incoherent ($2.3M booked on $71 invested)."""
    store = store_with(
        tmp_path, "filters_solanatracker:\n  require_closed_trades: true\n")
    board = [nominee(0, tpd=10.0, closed_trades=0),
             nominee(1, tpd=10.0, closed_trades=140)]
    _, registry, daemon = make_daemon(db, bus, store,
                                      tracker=FakeTracker(board))
    daemon.leaderboard.harvest(store.config)
    assert registry.get(board[0]["address"]) is None
    assert registry.get(board[1]["address"]) is not None


def test_low_volume_wallets_are_refused(tmp_path, db, bus):
    """A wallet that earns on dust cannot show volume — the pools will
    not absorb it — so volume separates real traders from rugpullers."""
    store = store_with(
        tmp_path, "filters_solanatracker:\n  min_volume_usd: 5000.0\n")
    board = [nominee(0, tpd=10.0, volume_usd=300.0),
             nominee(1, tpd=10.0, volume_usd=250_000.0)]
    _, registry, daemon = make_daemon(db, bus, store,
                                      tracker=FakeTracker(board))
    daemon.leaderboard.harvest(store.config)
    assert registry.get(board[0]["address"]) is None
    assert registry.get(board[1]["address"]) is not None


def test_dormant_wallets_are_refused(tmp_path, db, bus):
    store = store_with(
        tmp_path, "filters_solanatracker:\n  max_last_trade_hours: 168.0\n")
    stale = nominee(0, tpd=10.0, last_trade_at=time.time() - 30 * 86400)
    fresh = nominee(1, tpd=10.0)
    _, registry, daemon = make_daemon(db, bus, store,
                                      tracker=FakeTracker([stale, fresh]))
    daemon.leaderboard.harvest(store.config)
    assert registry.get(stale["address"]) is None
    assert registry.get(fresh["address"]) is not None


def test_a_seat_is_freed_when_a_trader_falls_off_the_board(tmp_path, db, bus):
    """Only wallets ON the board get re-scored, so a seated trader that
    drops off it would otherwise keep its seat and its admission-day
    score forever — over a multi-day run the roster becomes a museum."""
    from olala.discovery.leaderboard import ABSENCES_BEFORE_RETIREMENT

    store = store_with(tmp_path, "discovery:\n  max_followed_traders: 2\n")
    keeper, leaver = nominee(0, tpd=10.0), nominee(1, tpd=10.0)
    tracker = FakeTracker([keeper, leaver])
    _, registry, daemon = make_daemon(db, bus, store, tracker=tracker)
    daemon.leaderboard.harvest(store.config)
    assert len(registry.by_status(TraderStatus.FOLLOWED)) == 2

    # `leaver` stops qualifying — it simply is not on the board any more.
    tracker.traders = [keeper]
    for sweep in range(ABSENCES_BEFORE_RETIREMENT):
        daemon.leaderboard._last_poll_at = 0.0
        daemon.leaderboard.harvest(store.config)
        if sweep < ABSENCES_BEFORE_RETIREMENT - 1:
            # One missing sweep is as likely a service hiccup as a real
            # change, so the seat is not taken away on the first miss.
            assert registry.get(leaver["address"]).status \
                is TraderStatus.FOLLOWED

    assert registry.get(leaver["address"]).status is TraderStatus.RETIRED
    assert registry.get(keeper["address"]).status is TraderStatus.FOLLOWED


def test_a_brief_absence_does_not_cost_a_seat(tmp_path, db, bus):
    """A service hiccup must not churn the roster."""
    store = store_with(tmp_path, "discovery:\n  max_followed_traders: 2\n")
    keeper, blinker = nominee(0, tpd=10.0), nominee(1, tpd=10.0)
    tracker = FakeTracker([keeper, blinker])
    _, registry, daemon = make_daemon(db, bus, store, tracker=tracker)
    daemon.leaderboard.harvest(store.config)

    tracker.traders = [keeper]                    # one bad sweep
    daemon.leaderboard._last_poll_at = 0.0
    daemon.leaderboard.harvest(store.config)

    tracker.traders = [keeper, blinker]           # back again
    daemon.leaderboard._last_poll_at = 0.0
    daemon.leaderboard.harvest(store.config)

    tracker.traders = [keeper]                    # and away once more
    daemon.leaderboard._last_poll_at = 0.0
    daemon.leaderboard.harvest(store.config)

    # The counter reset when it reappeared, so it keeps its seat.
    assert registry.get(blinker["address"]).status is TraderStatus.FOLLOWED
