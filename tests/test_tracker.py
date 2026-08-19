"""WalletTracker: gears, cursor protocol, push path, staleness gate.

Ported from the follow daemon's tests, which pinned the two properties
that matter most — never skip a trade, never copy one twice — and
extended for the behaviour the rework adds: two gears, batched sweeps,
a derived interval, and a push path that must not move the watermark.
"""

import time

import pytest

from olala.chain.errors import ChainError
from olala.chain.sources.base import BatchItem
from olala.domain.models import TraderProfile, TraderStatus
from olala.services.traders import TraderRegistry
from olala.trading.tracker import Gear, WalletTracker

from fakes import FakeProvider, make_swap_tx

TRADER = "TraderAAAA1111111111111111111111111111111111"
OTHER = "TraderBBBB2222222222222222222222222222222222"
MINT = "MintAAAA111111111111111111111111111111111111"


class RecordingQueue:
    """Stands in for SignalQueue — synchronous, so tests stay ordered."""

    def __init__(self):
        self.signals = []

    def submit(self, signal):
        self.signals.append(signal)
        return True


class FakeRouter:
    """Batches by fanning out to a FakeProvider, as a real one would."""

    def __init__(self, provider, batch_capable="publicnode"):
        self._provider = provider
        self._batch_capable = batch_capable
        self.batch_calls = []

    def batch_capable(self, policy):
        return self._batch_capable

    def batch(self, policy, items, timeout=None):
        self.batch_calls.append(list(items))
        results = []
        for item in items:
            address = item.params[0]
            options = item.params[1] if len(item.params) > 1 else {}
            results.append(self._provider.get_signatures(
                address, limit=options.get("limit", 30)))
        return results


def make_world(db, bus, config_store, *, followed=(TRADER,),
               batch_capable="publicnode"):
    provider = FakeProvider()
    provider.router = FakeRouter(provider, batch_capable)
    registry = TraderRegistry(db, bus)
    for address in followed:
        registry.update(TraderProfile(address=address,
                                      status=TraderStatus.FOLLOWED,
                                      assigned_wallet_id="w1"))
    queue = RecordingQueue()
    tracker = WalletTracker(config_store, provider, registry, db, queue)
    return provider, registry, queue, tracker


def sig_entry(name, slot, err=None, age_sec=0.0):
    return {"signature": name, "slot": slot, "err": err,
            "blockTime": time.time() - age_sec}


def swap_for(provider, name, buy=True, age_sec=0.0):
    provider.transactions[name] = make_swap_tx(
        TRADER, -2_000_005_000 if buy else 1_000_000_000,
        MINT, 400.0 if buy else -200.0)
    provider.transactions[name]["blockTime"] = time.time() - age_sec


def sweep(tracker):
    """Let the next scheduled batch sweep happen right now.

    The tracker paces sweeps by wall clock — that is the point of the
    derived interval — but a test cares what a sweep DOES, not about
    waiting five seconds to watch one.
    """
    tracker._next_batch_at = 0.0
    tracker.tick()


def names(queue):
    return [s.observed.signature for s in queue.signals]


# -- the cursor protocol ---------------------------------------------------

def test_first_contact_arms_without_replaying_history(db, bus, config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("old2", 200),
                                   sig_entry("old1", 100)]
    tracker.tick()
    assert queue.signals == []
    assert db.load_watermarks()[TRADER] == (200, "old2")


def test_new_swaps_are_emitted_oldest_first(db, bus, config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.tick()

    provider.signatures[TRADER] = [sig_entry("s2", 300), sig_entry("s1", 200),
                                   sig_entry("s0", 100)]
    swap_for(provider, "s1", buy=True)
    swap_for(provider, "s2", buy=False)
    sweep(tracker)

    assert names(queue) == ["s1", "s2"]
    assert queue.signals[0].side.value == "buy"
    assert queue.signals[1].side.value == "sell"
    assert db.load_watermarks()[TRADER] == (300, "s2")


def test_failed_and_non_swap_transactions_are_skipped_but_passed(
        db, bus, config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.tick()

    provider.signatures[TRADER] = [sig_entry("s2", 300, err={"x": 1}),
                                   sig_entry("s1", 200),
                                   sig_entry("s0", 100)]
    provider.transactions["s1"] = {"meta": {"err": None}, "transaction":
                                   {"message": {"accountKeys": []}}}
    sweep(tracker)
    assert queue.signals == []
    assert db.load_watermarks()[TRADER][1] == "s2"


def test_unfollowed_traders_are_never_polled(db, bus, config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    profile = registry.get(TRADER)
    profile.status = TraderStatus.RETIRED
    registry.update(profile)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.tick()
    assert db.load_watermarks()[TRADER] == (0, "")


# -- H1/H2: never skip, never replay --------------------------------------

def test_burst_larger_than_the_budget_carries_over(db, bus, config_store):
    """The per-cycle fetch budget must defer work, never discard it."""
    config_store.update({"tracking": {"max_transactions_per_cycle": 10}})
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s00", 100)]
    tracker.tick()

    entries = [sig_entry(f"s{i:02d}", 100 + i) for i in range(14, 0, -1)]
    provider.signatures[TRADER] = entries + [sig_entry("s00", 100)]
    for entry in entries:
        swap_for(provider, entry["signature"])

    sweep(tracker)
    assert names(queue) == [f"s{i:02d}" for i in range(1, 11)]
    sweep(tracker)
    assert names(queue) == [f"s{i:02d}" for i in range(1, 15)]


def test_rpc_failure_mid_cycle_never_duplicates(db, bus, config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.tick()

    provider.signatures[TRADER] = [sig_entry("s2", 300), sig_entry("s1", 200),
                                   sig_entry("s0", 100)]
    swap_for(provider, "s1")
    swap_for(provider, "s2")
    provider.fail_transactions.add("s2")
    sweep(tracker)                       # s1 copies, s2 fetch fails
    assert names(queue) == ["s1"]

    provider.fail_transactions.clear()
    sweep(tracker)                       # resumes at s2, must not replay s1
    assert names(queue) == ["s1", "s2"]


def test_a_window_that_misses_the_watermark_does_not_replay(
        db, bus, config_store):
    """THE regression. A bare-signature cursor absent from the window made
    the follower treat all 30 entries as fresh and re-copy every one."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s1", 100)]
    tracker.tick()                       # armed at (100, s1)
    swap_for(provider, "s1")

    # The node now returns a window that does not reach back to s1 and
    # cannot page to it either.
    provider.signatures[TRADER] = [sig_entry(f"n{i}", 900 + i)
                                   for i in range(30, 0, -1)]
    for entry in provider.signatures[TRADER]:
        swap_for(provider, entry["signature"])

    sweep(tracker)

    assert tracker.status.gaps_detected == 1
    assert queue.signals == []           # nothing dispatched over the gap
    # The watermark did NOT move: losing sight of trades is recoverable,
    # copying them twice is not.
    assert db.load_watermarks()[TRADER] == (100, "s1")


def test_processed_signatures_survive_a_restart(db, bus, config_store):
    """The old processed set was an in-memory LRU, so a restart mid-window
    replayed whatever had not yet moved the cursor."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.tick()
    provider.signatures[TRADER] = [sig_entry("s1", 200), sig_entry("s0", 100)]
    swap_for(provider, "s1")
    sweep(tracker)
    assert names(queue) == ["s1"]

    assert db.load_processed(TRADER) == {"s1": 200}
    # A fresh tracker over the same database must not re-copy s1.
    second_queue = RecordingQueue()
    restarted = WalletTracker(config_store, provider, registry, db,
                              second_queue)
    sweep(restarted)
    assert second_queue.signals == []


# -- gears -----------------------------------------------------------------

def test_startup_uses_the_batch_gear(db, bus, config_store):
    """Never begin on the cheap gear: the stream has proven nothing yet."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.tick()
    assert tracker.status.gear == Gear.BATCH.value
    assert provider.router.batch_calls


def test_a_proven_stream_downshifts_to_round_robin(db, bus, config_store):
    """The cheap gear costs ~1 call/s whatever the roster size."""
    provider, registry, queue, tracker = make_world(
        db, bus, config_store, followed=(TRADER, OTHER))
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    provider.signatures[OTHER] = [sig_entry("t0", 100)]

    tracker.note_stream_alive()
    tracker.note_stream_alive()
    tracker.tick()

    assert tracker.status.gear == Gear.ROUND_ROBIN.value
    assert provider.router.batch_calls == []


def test_round_robin_covers_the_whole_roster_one_wallet_at_a_time(
        db, bus, config_store):
    provider, registry, queue, tracker = make_world(
        db, bus, config_store, followed=(TRADER, OTHER))
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    provider.signatures[OTHER] = [sig_entry("t0", 100)]
    tracker.note_stream_alive()
    tracker.note_stream_alive()

    tracker.tick()
    tracker.tick()

    marks = db.load_watermarks()
    assert marks[TRADER][1] == "s0"
    assert marks[OTHER][1] == "t0"


def test_a_quiet_stream_does_not_upshift(db, bus, config_store):
    """Silence is not failure. Treating a quiet market as a broken
    stream flapped the tracker onto the expensive gear every time
    trading went still for a minute — ten times the budget to learn
    nothing."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.note_stream_alive()
    tracker.note_stream_alive()
    tracker.tick()
    assert tracker.status.gear == Gear.ROUND_ROBIN.value

    tracker._last_stream_proof = time.time() - 86_400   # a day of quiet
    tracker.tick()
    assert tracker.status.gear == Gear.ROUND_ROBIN.value


def test_a_dropped_socket_upshifts_immediately(db, bus, config_store):
    """A disconnect needs no inference."""
    provider = FakeProvider()
    provider.router = FakeRouter(provider)
    registry = TraderRegistry(db, bus)
    registry.update(TraderProfile(address=TRADER,
                                  status=TraderStatus.FOLLOWED,
                                  assigned_wallet_id="w1"))
    connected = {"ok": True}
    tracker = WalletTracker(config_store, provider, registry, db,
                            RecordingQueue(),
                            stream_health=lambda: connected["ok"])
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.note_stream_alive()
    tracker.note_stream_alive()
    tracker.tick()
    assert tracker.status.gear == Gear.ROUND_ROBIN.value

    connected["ok"] = False
    tracker.tick()
    assert tracker.status.gear == Gear.BATCH.value


def test_a_trade_the_stream_missed_upshifts_and_is_counted(db, bus,
                                                           config_store):
    """The real proof. A stream that has gone silently dead is caught by
    the poll finding a trade it should have delivered."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    sweep(tracker)
    tracker.note_stream_alive()
    tracker.note_stream_alive()
    assert tracker._stream_is_proven() is True
    # The stream has been answerable for a while — so a trade landing
    # now IS its responsibility.
    tracker._stream_trusted_since = time.time() - 300

    # A trade well past the grace window that the push never reported.
    old = config_store.config.tracking.stream_miss_grace_sec + 30
    swap_for(provider, "missed", age_sec=old)
    provider.signatures[TRADER] = [sig_entry("missed", 200, age_sec=old),
                                   sig_entry("s0", 100)]
    sweep(tracker)

    assert tracker.status.stream_misses == 1
    assert tracker._stream_is_proven() is False
    tracker.tick()
    assert tracker.status.gear == Gear.BATCH.value


def test_a_trade_inside_the_grace_window_is_not_a_miss(db, bus,
                                                       config_store):
    """An ordinary race between a notification and a sweep must not be
    mistaken for a broken subscription."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    sweep(tracker)
    tracker.note_stream_alive()
    tracker.note_stream_alive()

    swap_for(provider, "fresh", age_sec=1.0)
    provider.signatures[TRADER] = [sig_entry("fresh", 200, age_sec=1.0),
                                   sig_entry("s0", 100)]
    sweep(tracker)

    assert tracker.status.stream_misses == 0
    assert tracker._stream_is_proven() is True


def test_the_push_path_never_reports_itself_as_a_miss(db, bus,
                                                      config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    sweep(tracker)
    tracker.note_stream_alive()
    tracker.note_stream_alive()

    old = config_store.config.tracking.stream_miss_grace_sec + 30
    swap_for(provider, "viapush", age_sec=old)
    tracker.note_activity(TRADER, "viapush", 300)

    assert tracker.status.stream_misses == 0


def test_no_batch_capable_source_degrades_to_round_robin_not_blindness(
        db, bus, config_store):
    """mainnet-beta cannot batch and Helius refuses roster-sized batches;
    one call per tick is the floor every source can serve."""
    provider, registry, queue, tracker = make_world(
        db, bus, config_store, batch_capable=None)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.tick()
    assert tracker.status.gear == Gear.ROUND_ROBIN.value
    assert db.load_watermarks()[TRADER][1] == "s0"


def test_batch_sweep_asks_for_every_followed_wallet_in_one_request(
        db, bus, config_store):
    provider, registry, queue, tracker = make_world(
        db, bus, config_store, followed=(TRADER, OTHER))
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    provider.signatures[OTHER] = [sig_entry("t0", 100)]
    tracker.tick()
    assert len(provider.router.batch_calls) == 1
    assert len(provider.router.batch_calls[0]) == 2
    assert all(isinstance(i, BatchItem) for i in provider.router.batch_calls[0])


def test_one_wallet_failing_in_a_batch_does_not_stop_the_others(
        db, bus, config_store):
    provider, registry, queue, tracker = make_world(
        db, bus, config_store, followed=(TRADER, OTHER))

    class PartialRouter(FakeRouter):
        def batch(self, policy, items, timeout=None):
            self.batch_calls.append(list(items))
            return [RuntimeError("that address is malformed"),
                    [sig_entry("t0", 100)]]

    provider.router = PartialRouter(provider)
    tracker._router = provider.router
    tracker.tick()

    assert db.load_watermarks()[OTHER][1] == "t0"
    assert tracker.status.errors


# -- derived interval ------------------------------------------------------

def test_interval_is_derived_from_roster_against_the_measured_ceiling(
        db, bus, config_store):
    """Cost is wallets ÷ interval, so a bigger roster must widen the
    cadence rather than silently produce 429s."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    # publicnode measured at 10 wallet-calls/s, floor 5s.
    assert tracker._derived_interval(42, "publicnode") == 5.0
    assert tracker._derived_interval(100, "publicnode") == 10
    assert tracker._derived_interval(1, "publicnode") == 5.0


def test_unknown_source_falls_back_to_the_configured_floor(
        db, bus, config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    floor = config_store.config.tracking.min_interval_sec
    assert tracker._derived_interval(42, None) == floor


# -- push path -------------------------------------------------------------

def test_push_path_copies_without_a_signature_lookup(db, bus, config_store):
    """The notification already names the signature; the old code threw
    that away and spent a call rediscovering it."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.tick()
    reads_before = provider.signature_reads_for(TRADER)

    swap_for(provider, "push1")
    tracker.note_activity(TRADER, "push1", 250)

    assert names(queue) == ["push1"]
    assert provider.signature_reads_for(TRADER) == reads_before


def test_push_never_advances_the_watermark(db, bus, config_store):
    """Notifications arrive at 'confirmed' while the sweep reconciles
    below it; moving the marker on a push would jump the sweep over
    transactions it has not yet seen."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.tick()
    before = db.load_watermarks()[TRADER]

    swap_for(provider, "push1")
    tracker.note_activity(TRADER, "push1", 900)

    assert db.load_watermarks()[TRADER] == before


def test_a_pushed_signature_is_not_copied_again_by_the_sweep(
        db, bus, config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.tick()

    swap_for(provider, "s1")
    tracker.note_activity(TRADER, "s1", 200)
    assert names(queue) == ["s1"]

    provider.signatures[TRADER] = [sig_entry("s1", 200), sig_entry("s0", 100)]
    sweep(tracker)
    assert names(queue) == ["s1"]        # exactly once


def test_push_for_an_unfollowed_address_is_ignored(db, bus, config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    tracker.note_activity("UnknownTrader111", "sigX", 1)
    assert queue.signals == []


def test_push_reports_stream_liveness_even_with_no_signature(
        db, bus, config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    assert tracker._stream_is_proven() is False
    tracker.note_activity(TRADER)
    tracker.note_activity(TRADER)
    assert tracker._stream_is_proven() is True


# -- staleness gate --------------------------------------------------------

def test_stale_entries_are_blocked_but_exits_are_not(db, bus, config_store):
    """After an outage the backlog would otherwise buy into positions the
    trader has already exited. A late SELL is still the right sell."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.tick()

    old = config_store.config.tracking.max_signal_age_sec + 600
    swap_for(provider, "oldbuy", buy=True, age_sec=old)
    swap_for(provider, "oldsell", buy=False, age_sec=old)
    provider.signatures[TRADER] = [sig_entry("oldsell", 300),
                                   sig_entry("oldbuy", 200),
                                   sig_entry("s0", 100)]
    sweep(tracker)

    assert names(queue) == ["oldsell"]
    assert tracker.status.stale_entries_blocked == 1


def test_fresh_entries_pass_the_staleness_gate(db, bus, config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.tick()
    swap_for(provider, "fresh", buy=True, age_sec=1.0)
    provider.signatures[TRADER] = [sig_entry("fresh", 200),
                                   sig_entry("s0", 100)]
    sweep(tracker)
    assert names(queue) == ["fresh"]


# -- housekeeping ----------------------------------------------------------

def test_processed_ledger_is_pruned_below_the_watermark(db, bus,
                                                        config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 1_000_000)]
    tracker.tick()
    db.record_processed([(TRADER, "ancient", 1)])

    swap_for(provider, "s1")
    provider.signatures[TRADER] = [sig_entry("s1", 2_000_000),
                                   sig_entry("s0", 1_000_000)]
    sweep(tracker)

    remaining = db.load_processed(TRADER)
    assert "ancient" not in remaining
    assert "s1" in remaining


def test_empty_roster_is_a_no_op(db, bus, config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store,
                                                    followed=())
    tracker.tick()
    assert tracker.status.roster == 0
    assert provider.router.batch_calls == []


# -- migration from the pre-slot cursor ------------------------------------

def test_a_legacy_slotless_cursor_is_re_armed_not_wedged(db, bus,
                                                         config_store):
    """Rows written before slots existed carry a signature and slot 0.
    Such a marker cannot be located or compared, so without explicit
    handling every sweep would raise an unbridgeable gap forever."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    # Exactly what an upgraded database looks like.
    db.update_watermarks([(TRADER, 0, "cursor-from-the-old-schema")])
    tracker._watermarks.clear()
    tracker._load_watermarks()

    provider.signatures[TRADER] = [sig_entry(f"n{i}", 900 + i)
                                   for i in range(30, 0, -1)]
    for entry in provider.signatures[TRADER]:
        swap_for(provider, entry["signature"])

    sweep(tracker)

    slot, signature = db.load_watermarks()[TRADER]
    assert slot == 930 and signature == "n30"
    assert tracker.status.legacy_rearmed == 1
    # Re-arming SKIPS the gap rather than replaying it: nothing dispatched.
    assert queue.signals == []
    assert tracker.status.gaps_detected == 0


def test_a_legacy_cursor_still_visible_is_walked_normally(db, bus,
                                                          config_store):
    """When the old signature is still in view, no history is lost — the
    walk proceeds and the watermark simply gains its slot."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    db.update_watermarks([(TRADER, 0, "s0")])
    tracker._watermarks.clear()
    tracker._load_watermarks()

    provider.signatures[TRADER] = [sig_entry("s1", 200), sig_entry("s0", 100)]
    swap_for(provider, "s1")
    sweep(tracker)

    assert names(queue) == ["s1"]
    assert db.load_watermarks()[TRADER] == (200, "s1")
    # Upgraded in place, NOT re-armed: no history was skipped.
    assert tracker.status.legacy_upgraded == 1
    assert tracker.status.legacy_rearmed == 0


# -- concurrency -----------------------------------------------------------

def test_the_push_path_and_the_sweep_cannot_copy_the_same_trade(
        db, bus, config_store):
    """The push runs on the subscriber's dispatch thread while the sweep
    runs on the tracker thread. Both used to CHECK whether a signature
    had been seen and only mark it afterwards — leaving a ~100ms window,
    the length of a transaction fetch, in which both would dispatch it.
    That is a duplicate buy."""
    import threading

    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.tick()
    swap_for(provider, "racy")

    barrier = threading.Barrier(2, timeout=5)
    original = provider.get_transaction

    def slow_fetch(signature):
        # Force both threads to be inside the fetch at the same time.
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return original(signature)

    provider.get_transaction = slow_fetch
    threads = [threading.Thread(target=tracker.note_activity,
                               args=(TRADER, "racy", 500))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert names(queue).count("racy") == 1


def test_a_claimed_signature_is_persisted_for_the_next_restart(
        db, bus, config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.tick()
    swap_for(provider, "p1")
    tracker.note_activity(TRADER, "p1", 700)
    assert db.load_processed(TRADER).get("p1") == 700


def test_a_failed_attempt_releases_its_claim_for_the_next_cycle(
        db, bus, config_store):
    """Claiming before the fetch prevents a duplicate, but a claim that
    outlives a FAILED attempt loses the trade entirely: the cursor never
    passed it and nothing would pick it up again."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.tick()

    swap_for(provider, "flaky")
    provider.fail_transactions.add("flaky")
    tracker.note_activity(TRADER, "flaky", 400)
    assert queue.signals == []
    assert "flaky" not in db.load_processed(TRADER)

    provider.fail_transactions.clear()
    tracker.note_activity(TRADER, "flaky", 400)
    assert names(queue) == ["flaky"]


def test_state_for_unfollowed_traders_is_dropped_from_memory(db, bus,
                                                             config_store):
    """The roster turns over as better traders replace weaker ones; the
    processed ledger is the heavy part of that state."""
    provider, registry, queue, tracker = make_world(
        db, bus, config_store, followed=(TRADER, OTHER))
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    provider.signatures[OTHER] = [sig_entry("t0", 100)]
    tracker.tick()
    assert OTHER in tracker._processed or OTHER in tracker._last_seen

    profile = registry.get(OTHER)
    profile.status = TraderStatus.RETIRED
    registry.update(profile)
    sweep(tracker)

    assert OTHER not in tracker._processed
    assert OTHER not in tracker._last_seen
    # The PERSISTED watermark survives: re-following must not replay
    # their history as live trades.
    assert db.load_watermarks()[OTHER][1] == "t0"


# -- the refresh bar's data ------------------------------------------------
#
# The bar answers one question — "when will every followed wallet have
# fresh data, and did the last pull work?" — so the status must report a
# coverage WINDOW rather than a bare countdown. That lets the UI
# interpolate smoothly between 1s updates, and makes both gears answer
# the same question the same way.

def test_a_batch_sweep_opens_its_window_on_completion_not_on_start(
        db, bus, config_store):
    """Opening it at the start left the window already expired by the
    time a multi-second sweep finished: a bar pinned at 100% that never
    told the operator anything."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    sweep(tracker)

    s = tracker.status
    now = time.time()
    assert s.coverage_started_at <= now + 0.5
    # The window spans the derived interval and lies in the FUTURE.
    assert s.coverage_complete_at > now
    assert (s.coverage_complete_at - s.coverage_started_at) == \
        pytest.approx(s.configured_interval_sec, abs=0.5)


def test_a_batch_sweep_reports_every_wallet_it_refreshed(db, bus,
                                                         config_store):
    provider, registry, queue, tracker = make_world(
        db, bus, config_store, followed=(TRADER, OTHER))
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    provider.signatures[OTHER] = [sig_entry("t0", 100)]
    sweep(tracker)

    assert tracker.status.last_poll_ok is True
    assert tracker.status.pass_position == 2
    assert "2/2 wallets refreshed" in tracker.status.last_poll_detail
    assert tracker.status.polls_ok == 1


def test_a_partly_failed_sweep_is_not_reported_as_success(db, bus,
                                                          config_store):
    """Calling it 'ok' would hide exactly the wallets going stale."""
    provider, registry, queue, tracker = make_world(
        db, bus, config_store, followed=(TRADER, OTHER))

    class PartialRouter(FakeRouter):
        def batch(self, policy, items, timeout=None):
            self.batch_calls.append(list(items))
            return [RuntimeError("that address is malformed"),
                    [sig_entry("t0", 100)]]

    provider.router = PartialRouter(provider)
    tracker._router = provider.router
    sweep(tracker)

    assert tracker.status.last_poll_ok is False
    assert "1 failed" in tracker.status.last_poll_detail
    assert tracker.status.polls_failed == 1


def test_round_robin_reports_progress_across_the_whole_pass(db, bus,
                                                            config_store):
    """'Next pull in 1s' is true but useless; what matters is when the
    whole roster has been seen."""
    provider, registry, queue, tracker = make_world(
        db, bus, config_store, followed=(TRADER, OTHER))
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    provider.signatures[OTHER] = [sig_entry("t0", 100)]
    tracker.note_stream_alive()
    tracker.note_stream_alive()

    tracker.tick()
    assert tracker.status.pass_position == 1
    span = (tracker.status.coverage_complete_at
            - tracker.status.coverage_started_at)
    # The window covers the whole roster, not one tick.
    assert span == pytest.approx(2 * tracker.interval_sec(), abs=0.5)

    tracker.tick()
    assert tracker.status.pass_position == 2


def test_a_new_pass_reopens_the_window(db, bus, config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.note_stream_alive()
    tracker.note_stream_alive()

    tracker.tick()
    first = tracker.status.coverage_started_at
    tracker.tick()               # roster of 1: the pass wraps immediately
    assert tracker.status.coverage_complete_at > first


def test_a_failed_round_robin_poll_is_reported(db, bus, config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    sweep(tracker)
    tracker.note_stream_alive()
    tracker.note_stream_alive()

    def broken(address, limit=100, before=None):
        raise ChainError("node refused")

    provider.get_signatures = broken
    tracker.tick()

    assert tracker.status.last_poll_ok is False
    assert tracker.status.polls_failed == 1


def test_an_unexpected_error_never_leaves_the_bar_claiming_success(
        db, bus, config_store):
    """A bar that keeps reporting the last success while the tracker is
    broken is worse than no bar at all."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    sweep(tracker)
    assert tracker.status.last_poll_ok is True

    class Exploding:
        def batch_capable(self, policy):
            return "publicnode"

        def batch(self, policy, items, timeout=None):
            raise RuntimeError("something nobody anticipated")

    tracker._router = Exploding()
    tracker._stream_proofs = 0          # force the batch gear
    sweep(tracker)

    assert tracker.status.last_poll_ok is False
    assert "something nobody anticipated" in tracker.status.last_poll_detail


def test_an_empty_roster_reports_no_window_so_the_bar_hides(db, bus,
                                                            config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store,
                                                    followed=())
    tracker.tick()
    assert tracker.status.coverage_complete_at == 0.0


def test_changing_gear_expires_the_old_coverage_window(db, bus,
                                                       config_store):
    """The gears measure coverage differently, so a window opened by one
    describes nothing under the other — and counting down to a moment
    that no longer means anything is exactly what this bar must not do."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.note_stream_alive()
    tracker.note_stream_alive()
    tracker.tick()                       # round-robin opens a window
    assert tracker.status.gear == Gear.ROUND_ROBIN.value
    assert tracker.status.coverage_complete_at > time.time()

    tracker._stream_proofs = 0                # the stream loses trust
    tracker._next_batch_at = float("inf")     # no sweep this tick
    tracker.tick()

    assert tracker.status.gear == Gear.BATCH.value
    # Expired: the bar reads "pulling", not a stale round-robin countdown.
    assert tracker.status.coverage_complete_at <= time.time()


def test_a_trade_predating_the_subscription_is_not_the_streams_fault(
        db, bus, config_store):
    """Found live: on startup the first sweep picks up trades from while
    the app was OFF. Blaming the stream for those pinned the tracker to
    the expensive gear on every single restart."""
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    sweep(tracker)
    tracker.note_stream_alive()
    tracker.note_stream_alive()          # trust starts NOW

    # A trade from before the subscription existed.
    old = config_store.config.tracking.stream_miss_grace_sec + 600
    swap_for(provider, "from-downtime", age_sec=old)
    provider.signatures[TRADER] = [sig_entry("from-downtime", 200,
                                             age_sec=old),
                                   sig_entry("s0", 100)]
    sweep(tracker)

    assert tracker.status.stream_misses == 0
    assert tracker._stream_is_proven() is True


def test_a_reconnect_restarts_the_streams_responsibility(db, bus,
                                                         config_store):
    """Trades that land while the socket is down are not evidence
    against the subscription that comes back up."""
    connected = {"ok": True}
    provider = FakeProvider()
    provider.router = FakeRouter(provider)
    registry = TraderRegistry(db, bus)
    registry.update(TraderProfile(address=TRADER,
                                  status=TraderStatus.FOLLOWED,
                                  assigned_wallet_id="w1"))
    tracker = WalletTracker(config_store, provider, registry, db,
                            RecordingQueue(),
                            stream_health=lambda: connected["ok"])
    tracker.note_stream_alive()
    tracker.note_stream_alive()
    tracker._stream_trusted_since = time.time() - 300

    connected["ok"] = False
    assert tracker._stream_is_proven() is False
    assert tracker._stream_trusted_since == 0.0     # responsibility ended
