"""WalletTracker: gears, cursor protocol, push path, staleness gate.

Ported from the follow daemon's tests, which pinned the two properties
that matter most — never skip a trade, never copy one twice — and
extended for the behaviour the rework adds: two gears, batched sweeps,
a derived interval, and a push path that must not move the watermark.
"""

import time

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


def test_a_stale_stream_proof_upshifts_back_to_batch(db, bus, config_store):
    provider, registry, queue, tracker = make_world(db, bus, config_store)
    provider.signatures[TRADER] = [sig_entry("s0", 100)]
    tracker.note_stream_alive()
    tracker.note_stream_alive()
    tracker._last_stream_proof = time.time() - 3600     # long silent
    tracker.tick()
    assert tracker.status.gear == Gear.BATCH.value


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
