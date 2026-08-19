"""SignalQueue: per-trader ordering, overload policy, isolation.

Two properties here are money-safety, not throughput:

* a trader's BUY must finish before their SELL is attempted, or the sell
  finds no position and the copy desynchronises;
* under overload an ENTRY is dropped, never an EXIT — missing an entry
  costs an opportunity, missing an exit abandons real money in a
  position nobody is watching.
"""

import threading
import time

from olala.domain.models import CopySignal, ObservedTrade, TradeSide
from olala.trading.signals import SignalQueue


def signal(trader, side=TradeSide.BUY, mint="MintA"):
    observed = ObservedTrade(
        trader=trader, signature=f"{trader}-{side.value}-{mint}",
        side=side, mint=mint, token_amount=1.0, sol_amount=1.0,
        price_sol=1.0, block_time=time.time())
    return CopySignal(trader=trader, side=side, mint=mint,
                      trader_sol_amount=1.0, observed=observed)


def drain(queue, expected, timeout=3.0):
    """Wait until `expected` signals have been handled."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if queue.handled + queue.failed >= expected:
            return True
        time.sleep(0.01)
    return False


def test_signals_are_handed_to_the_handler():
    seen = []
    queue = SignalQueue(seen.append, workers=2)
    queue.start()
    try:
        queue.submit(signal("T1"))
        assert drain(queue, 1)
        assert len(seen) == 1
    finally:
        queue.stop()


def test_one_trader_is_never_processed_concurrently():
    """A BUY must complete before its SELL is attempted."""
    overlaps = []
    active = set()
    lock = threading.Lock()

    def handler(sig):
        with lock:
            overlaps.append(sig.trader in active)
            active.add(sig.trader)
        time.sleep(0.02)
        with lock:
            active.discard(sig.trader)

    queue = SignalQueue(handler, workers=4)
    queue.start()
    try:
        for _ in range(8):
            queue.submit(signal("T1"))
        assert drain(queue, 8)
        assert not any(overlaps)
    finally:
        queue.stop()


def test_a_traders_signals_keep_their_order():
    seen = []
    queue = SignalQueue(lambda s: seen.append(s.mint), workers=4)
    queue.start()
    try:
        for index in range(6):
            queue.submit(signal("T1", mint=f"m{index}"))
        assert drain(queue, 6)
        assert seen == [f"m{i}" for i in range(6)]
    finally:
        queue.stop()


def test_different_traders_run_concurrently():
    """Serializing everything would make one slow copy stall the fleet."""
    started = threading.Barrier(3, timeout=3.0)

    def handler(sig):
        started.wait()

    queue = SignalQueue(handler, workers=4)
    queue.start()
    try:
        for trader in ("T1", "T2", "T3"):
            queue.submit(signal(trader))
        assert drain(queue, 3)
    finally:
        queue.stop()


def test_a_failing_handler_does_not_stop_the_queue():
    seen = []

    def handler(sig):
        if sig.mint == "boom":
            raise RuntimeError("execution blew up")
        seen.append(sig.mint)

    queue = SignalQueue(handler, workers=2)
    queue.start()
    try:
        queue.submit(signal("T1", mint="boom"))
        queue.submit(signal("T1", mint="fine"))
        assert drain(queue, 2)
        assert seen == ["fine"]
        assert queue.failed == 1
    finally:
        queue.stop()


# -- overload policy -------------------------------------------------------

def test_overflow_drops_an_entry_and_never_an_exit():
    queue = SignalQueue(lambda s: None, workers=1, max_pending=3)
    # Not started: nothing drains, so the queue genuinely fills.
    assert queue.submit(signal("T1", TradeSide.BUY))
    assert queue.submit(signal("T2", TradeSide.SELL))
    assert queue.submit(signal("T3", TradeSide.BUY))

    assert queue.submit(signal("T4", TradeSide.SELL))     # room was made
    assert queue.dropped_entries == 1

    remaining = [s.side for q in queue._pending.values() for s in q]
    assert TradeSide.SELL in remaining
    assert remaining.count(TradeSide.SELL) == 2


def test_an_all_exit_queue_refuses_rather_than_dropping_an_exit():
    queue = SignalQueue(lambda s: None, workers=1, max_pending=2)
    assert queue.submit(signal("T1", TradeSide.SELL))
    assert queue.submit(signal("T2", TradeSide.SELL))

    # Nothing may be evicted, so the newcomer is refused — loudly, and
    # visibly, rather than silently displacing an exit.
    assert queue.submit(signal("T3", TradeSide.BUY)) is False
    assert queue.depth == 2


def test_stats_report_depth_and_drops():
    queue = SignalQueue(lambda s: None, workers=1, max_pending=1)
    queue.submit(signal("T1", TradeSide.SELL))
    queue.submit(signal("T2", TradeSide.BUY))
    stats = queue.stats()
    assert stats["depth"] == 1
    assert stats["dropped_entries"] == 1


def test_stop_is_safe_before_start_and_twice():
    queue = SignalQueue(lambda s: None)
    queue.stop()
    queue.start()
    queue.stop()
    queue.stop()
