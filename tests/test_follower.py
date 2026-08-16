from olala.domain.models import TraderProfile, TraderStatus
from olala.services.traders import TraderRegistry
from olala.trading.follower import FollowDaemon

from fakes import FakeProvider, make_swap_tx

TRADER = "TraderAAAA1111111111111111111111111111111111"
MINT = "MintAAAA111111111111111111111111111111111111"


class RecordingEngine:
    def __init__(self):
        self.signals = []

    def handle_signal(self, signal):
        self.signals.append(signal)


def make_world(db, bus, config_store):
    provider = FakeProvider()
    registry = TraderRegistry(db, bus)
    registry.update(TraderProfile(address=TRADER,
                                  status=TraderStatus.FOLLOWED,
                                  assigned_wallet_id="w1"))
    engine = RecordingEngine()
    daemon = FollowDaemon(config_store, provider, registry, engine)
    return provider, registry, engine, daemon


def sig_entry(name, err=None):
    return {"signature": name, "err": err, "blockTime": 1_755_000_000}


def test_first_contact_arms_cursor_without_replaying(db, bus, config_store):
    provider, registry, engine, daemon = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("old2"), sig_entry("old1")]
    daemon.tick()
    assert engine.signals == []
    assert registry.follow_cursor(TRADER) == "old2"


def test_new_swaps_emit_signals_oldest_first(db, bus, config_store):
    provider, registry, engine, daemon = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0")]
    daemon.tick()  # arms cursor at s0

    provider.signatures[TRADER] = [
        sig_entry("s2"), sig_entry("s1"), sig_entry("s0")]
    provider.transactions["s1"] = make_swap_tx(
        TRADER, -2_000_005_000, MINT, 400.0)
    provider.transactions["s2"] = make_swap_tx(
        TRADER, 1_000_000_000, MINT, -200.0)
    daemon.tick()

    assert [s.observed.signature for s in engine.signals] == ["s1", "s2"]
    assert engine.signals[0].side.value == "buy"
    assert engine.signals[1].side.value == "sell"
    assert registry.follow_cursor(TRADER) == "s2"


def test_failed_and_non_swap_txs_skipped(db, bus, config_store):
    provider, registry, engine, daemon = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0")]
    daemon.tick()

    provider.signatures[TRADER] = [
        sig_entry("s2", err={"x": 1}), sig_entry("s1"), sig_entry("s0")]
    provider.transactions["s1"] = {"meta": {"err": None}, "transaction":
                                   {"message": {"accountKeys": []}}}
    daemon.tick()
    assert engine.signals == []
    assert registry.follow_cursor(TRADER) == "s2"


def test_unfollowed_trader_not_polled(db, bus, config_store):
    provider, registry, engine, daemon = make_world(db, bus, config_store)
    profile = registry.get(TRADER)
    profile.status = TraderStatus.RETIRED
    registry.update(profile)
    provider.signatures[TRADER] = [sig_entry("s0")]
    daemon.tick()
    assert registry.follow_cursor(TRADER) == ""


def test_poll_now_triggers_immediate_poll(db, bus, config_store):
    from fakes import make_swap_tx
    provider, registry, engine, daemon = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0")]
    daemon.tick()  # arm cursor

    provider.signatures[TRADER] = [sig_entry("s1"), sig_entry("s0")]
    provider.transactions["s1"] = make_swap_tx(
        TRADER, -2_000_005_000, MINT, 400.0)
    daemon.poll_now(TRADER)  # push notification path, no tick needed
    assert [s.observed.signature for s in engine.signals] == ["s1"]
    daemon.poll_now("UnknownTrader111")  # unknown address is a no-op
    assert len(engine.signals) == 1
