"""Base class for background daemons.

Each daemon is a single thread with a tick interval, a cooperative stop
signal, and exception isolation: a failing tick is logged and retried on
the next interval, never allowed to kill the thread.

Two properties matter for anything cadence-sensitive:

* **The sleep compensates for how long the tick took.** Sleeping a full
  interval *after* the work makes the real period `interval + work`, so a
  "5 second" sweep that takes 2 seconds actually runs every 7. Cadence
  claims then quietly stop being true under load.
* **Ticks never stack.** If the work outran its interval, the next tick
  starts immediately and the overrun is COUNTED rather than queued.
  Running two sweeps concurrently to "catch up" would double the cost of
  the very thing that was already too slow.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


class Daemon(threading.Thread):
    def __init__(self, name: str, interval_sec: float) -> None:
        super().__init__(name=name, daemon=True)
        self._interval = interval_sec
        self._stop_event = threading.Event()
        self.overruns = 0
        self.last_tick_sec = 0.0

    def interval_sec(self) -> float:
        """Seconds between ticks, re-read every tick.

        Overriding this is how a daemon follows a configuration change
        without a restart; several daemons previously captured the value
        once at construction and never saw an update.
        """
        return self._interval

    def tick(self) -> None:
        raise NotImplementedError

    def run(self) -> None:
        logger.info("daemon %s started (interval %ss)",
                    self.name, self.interval_sec())
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception:
                logger.exception("daemon %s tick failed", self.name)
            self.last_tick_sec = elapsed = time.monotonic() - started
            interval = max(self.interval_sec(), 0.0)
            if elapsed > interval and interval > 0:
                self.overruns += 1
            self._stop_event.wait(max(0.0, interval - elapsed))
        logger.info("daemon %s stopped", self.name)

    def stop(self) -> None:
        self._stop_event.set()
