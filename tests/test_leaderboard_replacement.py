"""Leaderboard sourcing (Solana Tracker → on-chain fall-through) and
automatic replacement of the weakest followed trader."""

import time

from olala.config import ConfigStore
from olala.discovery.scanner import RpcBudget
from olala.domain.models import (ObservedTrade, TraderProfile, TraderStatus,
                                 TradeSide)

from fakes import FakeMarketData, FakeProvider, FakeTracker
from test_discovery_v2 import make_daemon

NOMINEE = "NomineeAAAA111111111111111111111111111111111"


def dev_store(tmp_path):
    path = tmp_path / "dev.yaml"
    path.write_text("dev_mode: true\n")
    return ConfigStore(path=path)


def budget_for(store):
    return RpcBudget(store.config.discovery.rpc_calls_per_scan)


# -- sourcing and fall-through --------------------------------------------

def test_tracker_nominates_candidates(tmp_path, db, bus):
    store = dev_store(tmp_path)
    tracker = FakeTracker(traders=[
        {"address": NOMINEE, "win_rate": 0.71, "pnl_usd": 1000.0,
         "trade_count": 120}])
    _, registry, daemon = make_daemon(db, bus, store, tracker=tracker)
    events = bus.subscribe()

    daemon._harvest_candidates(store.config, budget_for(store))

    assert tracker.calls == 1
    profile = registry.get(NOMINEE)
    assert profile is not None
    assert profile.status is TraderStatus.CANDIDATE
    assert daemon._service_rank[NOMINEE] == 1.0  # leaderboard position
    kinds = []
    while not events.empty():
        kinds.append(events.get_nowait())
    scans = [e for e in kinds if e["type"] == "discovery_scan"]
    assert scans and "Solana Tracker" in scans[0]["data"]["source"]


def test_tracker_failure_falls_through_to_winners(tmp_path, db, bus):
    class RecordingMarket(FakeMarketData):
        def __init__(self):
            super().__init__()
            self.winner_searches = 0

        def search_winners(self, min_liquidity_usd, min_change_pct,
                           limit=8):
            self.winner_searches += 1
            return []

    store = dev_store(tmp_path)
    market = RecordingMarket()
    _, _, daemon = make_daemon(db, bus, store, tracker=FakeTracker(fail=True),
                               market=market)

    daemon._harvest_candidates(store.config, budget_for(store))

    # The service blew up (rate limit, outage — same path) and the sweep
    # still reached the on-chain winners' holders source.
    assert market.winner_searches == 1


def test_leaderboard_polls_are_throttled(tmp_path, db, bus):
    store = dev_store(tmp_path)
    tracker = FakeTracker(traders=[])
    _, _, daemon = make_daemon(db, bus, store, tracker=tracker)

    daemon._harvest_candidates(store.config, budget_for(store))
    daemon._harvest_candidates(store.config, budget_for(store))

    # Second sweep lands inside leaderboard_interval_sec: no second call —
    # this is what keeps a free API tier alive for the whole month.
    assert tracker.calls == 1


def test_failed_service_is_not_hammered(tmp_path, db, bus):
    store = dev_store(tmp_path)
    tracker = FakeTracker(fail=True)
    _, _, daemon = make_daemon(db, bus, store, tracker=tracker)

    daemon._harvest_candidates(store.config, budget_for(store))
    daemon._harvest_candidates(store.config, budget_for(store))

    # A rate-limited/failing service waits out the full interval too.
    assert tracker.calls == 1


def test_no_services_means_pure_onchain(tmp_path, db, bus):
    store = dev_store(tmp_path)
    _, _, daemon = make_daemon(db, bus, store)
    assert daemon._leaderboard_due(store.config) is False


# -- automatic replacement of the weakest followed trader ------------------

def winning_trades(address, mint="MintW111"):
    now = time.time()
    return [
        ObservedTrade(trader=address, signature=f"{address[:6]}-buy",
                      side=TradeSide.BUY, mint=mint, token_amount=100.0,
                      sol_amount=1.0, price_sol=0.01,
                      block_time=now - 7200),
        ObservedTrade(trader=address, signature=f"{address[:6]}-sell",
                      side=TradeSide.SELL, mint=mint, token_amount=100.0,
                      sol_amount=2.0, price_sol=0.02,
                      block_time=now - 3600),
    ]


def full_roster(registry, count, score):
    for index in range(count):
        registry.update(TraderProfile(
            address=f"Followed{index:02d}11111111111111111111111111111",
            status=TraderStatus.FOLLOWED, score=score,
            assigned_wallet_id="w1"))


def finalize(tmp_path, db, bus, incumbent_score):
    """Roster at capacity with the given incumbent score; NOMINEE (one
    winning round trip in window → score ≈ 0.80) gets finalized."""
    store = dev_store(tmp_path)
    provider = FakeProvider()
    provider.signatures[NOMINEE] = [{"signature": "fresh", "err": None,
                                     "blockTime": time.time()}]
    provider, registry, daemon = make_daemon(db, bus, store,
                                             provider=provider)
    full_roster(registry, store.config.discovery.max_followed_traders,
                incumbent_score)
    registry.add_candidate(NOMINEE)
    db.insert_observed_trades(winning_trades(NOMINEE))
    events = bus.subscribe()
    daemon._finalize_candidate(store.config, NOMINEE, budget_for(store))
    kinds = []
    while not events.empty():
        kinds.append(events.get_nowait()["type"])
    return registry, kinds


def test_stronger_candidate_replaces_weakest(tmp_path, db, bus):
    registry, kinds = finalize(tmp_path, db, bus, incumbent_score=0.5)

    nominee = registry.get(NOMINEE)
    assert nominee.status is TraderStatus.FOLLOWED
    assert "trader_admitted" in kinds
    assert "trader_retired" in kinds
    retired = [p for p in registry.all()
               if p.status is TraderStatus.RETIRED]
    assert len(retired) == 1
    assert "replaced by" in retired[0].rejection_reason
    # The seat count is conserved: one out, one in.
    followed = registry.followed()
    assert len(followed) == 10
    assert nominee in followed


def test_weaker_candidate_does_not_churn_roster(tmp_path, db, bus):
    registry, kinds = finalize(tmp_path, db, bus, incumbent_score=0.9)

    nominee = registry.get(NOMINEE)
    assert nominee.status is TraderStatus.REJECTED
    assert "does not beat" in nominee.rejection_reason
    assert "trader_retired" not in kinds
    assert len(registry.followed()) == 10


def test_replace_margin_blocks_noise_churn(tmp_path, db, bus):
    # Incumbent within the margin of the nominee's ≈0.80: no eviction.
    registry, kinds = finalize(tmp_path, db, bus, incumbent_score=0.79)

    assert registry.get(NOMINEE).status is TraderStatus.REJECTED
    assert "trader_retired" not in kinds


def test_full_roster_keeps_sweeping(tmp_path, db, bus):
    store = dev_store(tmp_path)
    _, registry, daemon = make_daemon(db, bus, store)
    full_roster(registry, store.config.discovery.max_followed_traders, 0.9)

    daemon.tick()

    # The old behavior parked discovery when the roster was full; now the
    # sweep must keep hunting so upgrades can be found.
    assert daemon.last_status["phase"] != "roster_full"
    assert "hunting" in daemon.last_status.get("detail", "").lower() \
        or daemon.last_status["phase"] == "sweep_done"


def test_keyless_hunt_is_ongoing_every_tick(db, bus, config_store):
    """No API keys, dev mode off, roster full: every single tick must
    still run the on-chain census AND the winners' hunt — discovery is
    never one-shot and never idle."""
    class CountingProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.signature_reads = 0

        def get_signatures(self, address, limit=100, before=None):
            self.signature_reads += 1
            return super().get_signatures(address, limit, before)

    class RecordingMarket(FakeMarketData):
        def __init__(self):
            super().__init__()
            self.winner_searches = 0

        def search_winners(self, min_liquidity_usd, min_change_pct,
                           limit=8):
            self.winner_searches += 1
            return []

    provider = CountingProvider()
    market = RecordingMarket()
    provider_, registry, daemon = make_daemon(
        db, bus, config_store, provider=provider, market=market)
    full_roster(registry, config_store.config.discovery.max_followed_traders,
                0.9)

    daemon.tick()
    after_first = (provider.signature_reads, market.winner_searches)
    daemon.tick()
    after_second = (provider.signature_reads, market.winner_searches)

    assert after_first[0] > 0 and after_first[1] == 1
    # The second sweep hunts again — nothing throttles the on-chain path.
    assert after_second[0] > after_first[0]
    assert after_second[1] == 2


# -- nomination quality: push our floors up, cap activity locally ---------

class RecordingTracker(FakeTracker):
    """Captures the parameters the scanner sends to the service."""

    def __init__(self, traders=None):
        super().__init__(traders=traders)
        self.params = {}

    def top_traders(self, window_days=90, limit=100, min_trades=20,
                    min_active_days=0, sort="win_percentage",
                    max_trades_per_day=None, max_pages=1):
        self.params = {"window_days": window_days, "limit": limit,
                       "min_trades": min_trades,
                       "min_active_days": min_active_days, "sort": sort,
                       "max_trades_per_day": max_trades_per_day,
                       "max_pages": max_pages}
        return super().top_traders(window_days, limit, min_trades,
                                   min_active_days, sort,
                                   max_trades_per_day, max_pages)


def test_our_floors_are_pushed_to_the_service(tmp_path, db, bus):
    store = dev_store(tmp_path)
    tracker = RecordingTracker(traders=[])
    _, _, daemon = make_daemon(db, bus, store, tracker=tracker)

    daemon._harvest_candidates(store.config, budget_for(store))

    # The service's own defaults (20 trades, 3 active days) would
    # nominate wallets our filters reject on sight.
    assert tracker.params["min_trades"] == store.config.filters.min_trades
    assert tracker.params["min_active_days"] == \
        store.config.discovery.leaderboard_min_active_days
    assert tracker.params["window_days"] == \
        store.config.discovery.skill_window_days


def test_machine_cadence_nominees_dropped_before_any_rpc(tmp_path, db, bus):
    store = dev_store(tmp_path)
    ceiling = store.config.filters.max_trades_per_day
    tracker = FakeTracker(traders=[
        {"address": "FastBotAAAA1111111111111111111111111111111",
         "win_rate": 0.99, "trades_per_day": ceiling * 10},
        {"address": NOMINEE, "win_rate": 0.62,
         "trades_per_day": ceiling / 4},
    ])
    provider, registry, daemon = make_daemon(db, bus, store, tracker=tracker)

    daemon._harvest_candidates(store.config, budget_for(store))

    # The bot is filtered inside the client (payload data, zero RPC):
    # it never becomes a candidate and never costs a signature call.
    assert registry.get("FastBotAAAA1111111111111111111111111111111") is None
    assert provider.signature_reads_for("FastBotAAAA1111111111111111111111111111111") == 0
    # The human-cadence wallet went through as normal.
    assert registry.get(NOMINEE) is not None


def test_missing_rate_is_not_treated_as_a_bot(tmp_path, db, bus):
    store = dev_store(tmp_path)
    tracker = FakeTracker(traders=[
        {"address": NOMINEE, "win_rate": 0.6, "trades_per_day": None}])
    _, registry, daemon = make_daemon(db, bus, store, tracker=tracker)

    daemon._harvest_candidates(store.config, budget_for(store))

    assert registry.get(NOMINEE) is not None


def test_configured_sort_reaches_the_service(tmp_path, db, bus):
    path = tmp_path / "hf.yaml"
    path.write_text("dev_mode: true\ndiscovery:\n  leaderboard_sort: realized\n")
    store = ConfigStore(path=path)
    tracker = RecordingTracker(traders=[])
    _, _, daemon = make_daemon(db, bus, store, tracker=tracker)

    daemon._harvest_candidates(store.config, budget_for(store))

    assert tracker.params["sort"] == "realized"


def test_scan_queue_follows_leaderboard_order(tmp_path, db, bus):
    store = dev_store(tmp_path)
    first = "FirstAAAA1111111111111111111111111111111111"
    second = "SecondBBB1111111111111111111111111111111111"
    tracker = FakeTracker(traders=[
        {"address": first, "win_rate": 0.10},   # top of the board
        {"address": second, "win_rate": 0.99},  # better win rate, ranked lower
    ])
    _, _, daemon = make_daemon(db, bus, store, tracker=tracker)

    daemon._harvest_candidates(store.config, budget_for(store))

    # Queue priority follows the SERVICE ordering (whatever sort the
    # operator configured), not the win-rate field.
    assert daemon._service_rank[first] > daemon._service_rank[second]


def test_invalid_leaderboard_sort_rejected(tmp_path):
    import pytest

    path = tmp_path / "bad.yaml"
    path.write_text("discovery:\n  leaderboard_sort: vibes\n")
    with pytest.raises(ValueError, match="leaderboard_sort"):
        ConfigStore(path=path)

    good = ConfigStore(path=tmp_path / "good.yaml")
    with pytest.raises(ValueError, match="leaderboard_sort"):
        good.update({"discovery": {"leaderboard_sort": "vibes"}})
    # The rejected update must not leave a half-mutated config behind.
    assert good.config.discovery.leaderboard_sort == "win_percentage"
