"""Bounded dispatch of copy signals, serialized per trader.

Detection and execution run at wildly different speeds. A tracking tick
is one second; a live swap can spend a hundred seconds polling for
confirmation. Calling the engine straight from the tracking thread means
one confirming swap stops every other wallet from being watched — which
is exactly how you miss the exit you were copying.

So detection enqueues and workers execute. Two rules make that safe:

**One trader at a time.** A trader's BUY must complete before their SELL
is attempted, or the sell finds no position and the copy desynchronises.
Signals for *different* traders run concurrently; signals for the same
trader never do.

**Exits outrank entries.** When the queue is full, the oldest BUY is
dropped — never a SELL. Missing an entry costs an opportunity. Missing an
exit leaves real money in a position nobody is watching.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from typing import Callable

from ..domain.models import CopySignal, TradeSide

logger = logging.getLogger(__name__)

DEFAULT_WORKERS = 4
DEFAULT_MAX_PENDING = 256


class SignalQueue:
    """Hands copy signals to the engine off the tracking thread."""

    def __init__(self, handler: Callable[[CopySignal], None], *,
                 workers: int = DEFAULT_WORKERS,
                 max_pending: int = DEFAULT_MAX_PENDING) -> None:
        self._handler = handler
        self._worker_count = max(1, workers)
        self._max_pending = max(1, max_pending)
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._pending: dict[str, deque[CopySignal]] = defaultdict(deque)
        self._ready: deque[str] = deque()
        self._inflight: set[str] = set()
        self._threads: list[threading.Thread] = []
        self._running = False
        self.dropped_entries = 0
        self.handled = 0
        self.failed = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        for index in range(self._worker_count):
            thread = threading.Thread(target=self._work, daemon=True,
                                      name=f"signal-worker-{index}")
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 2.0) -> None:
        with self._wake:
            self._running = False
            self._wake.notify_all()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()

    # -- producing ---------------------------------------------------------

    @property
    def depth(self) -> int:
        with self._lock:
            return sum(len(q) for q in self._pending.values())

    def submit(self, signal: CopySignal) -> bool:
        """Queue a signal. False means it was dropped under overload."""
        with self._wake:
            total = sum(len(q) for q in self._pending.values())
            if total >= self._max_pending and not self._make_room_locked():
                # Everything queued is an exit; refusing the newcomer is
                # the only option that does not abandon a position.
                self.dropped_entries += 1
                logger.error(
                    "signal queue full (%d pending, all exits) — dropping "
                    "%s %s for %s", total, signal.side.value, signal.mint,
                    signal.trader)
                return False
            queue = self._pending[signal.trader]
            queue.append(signal)
            if (signal.trader not in self._inflight
                    and signal.trader not in self._ready):
                self._ready.append(signal.trader)
            self._wake.notify()
            return True

    def _make_room_locked(self) -> bool:
        """Evict the oldest ENTRY, never an exit. False if none exists."""
        for trader, queue in self._pending.items():
            for index, queued in enumerate(queue):
                if queued.side is TradeSide.BUY:
                    del queue[index]
                    self.dropped_entries += 1
                    logger.warning(
                        "signal queue full — dropped a queued BUY for %s to "
                        "make room; exits are never dropped", trader)
                    if not queue:
                        self._pending.pop(trader, None)
                    return True
        return False

    # -- consuming ---------------------------------------------------------

    def _work(self) -> None:
        while True:
            with self._wake:
                while self._running and not self._ready:
                    self._wake.wait(timeout=0.5)
                if not self._running:
                    return
                trader = self._ready.popleft()
                queue = self._pending.get(trader)
                if not queue:
                    self._pending.pop(trader, None)
                    continue
                signal = queue.popleft()
                # Claiming the trader is what serializes them: no other
                # worker may touch this trader until we are done.
                self._inflight.add(trader)

            try:
                self._handler(signal)
                self.handled += 1
            except Exception:                              # noqa: BLE001
                # A failed copy must never take the dispatcher down with
                # it — the next signal for this trader still has to run.
                self.failed += 1
                logger.exception("copy signal failed for %s (%s %s)",
                                 signal.trader, signal.side.value,
                                 signal.mint)
            finally:
                with self._wake:
                    self._inflight.discard(trader)
                    if self._pending.get(trader):
                        self._ready.append(trader)
                        self._wake.notify()
                    else:
                        self._pending.pop(trader, None)

    def stats(self) -> dict[str, int]:
        return {"depth": self.depth, "handled": self.handled,
                "failed": self.failed,
                "dropped_entries": self.dropped_entries,
                "workers": self._worker_count}
