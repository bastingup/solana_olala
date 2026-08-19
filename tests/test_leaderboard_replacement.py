"""Roster seat competition: a stronger candidate replaces the weakest
followed trader, and hysteresis keeps noise from churning the roster.

Stream sourcing and separation live in test_streams.py."""

import time

from olala.config import ConfigStore
from olala.discovery.scanner import RpcBudget
from olala.domain.models import (ObservedTrade, TraderProfile, TraderStatus,
                                 TradeSide)

from fakes import FakeMarketData, FakeProvider, FakeTracker
from test_discovery_v2 import make_daemon

NOMINEE = "NomineeAAAA111111111111111111111111111111111"


def dev_store(tmp_path):
    """Filters off (dev_mode: false) — these tests exercise seat
    competition, not the admission gates."""
    path = tmp_path / "dev.yaml"
    path.write_text("dev_mode: false\n")
    return ConfigStore(path=path)


def budget_for(store):
    return RpcBudget(store.config.discovery.rpc_calls_per_scan)


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
    still run the winners' hunt — discovery is never one-shot and never
    idle."""
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

    # The hunt runs on every sweep — nothing throttles the on-chain path
    # and a full roster does not stop the search for something better.
    assert after_first[1] == 1
    assert after_second[1] == 2
