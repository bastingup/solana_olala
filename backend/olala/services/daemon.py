"""Base class for background daemons.

Each daemon is a single thread with a fixed tick interval, a cooperative
stop signal, and exception isolation: a failing tick is logged and retried
on the next interval, never allowed to kill the thread.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class Daemon(threading.Thread):
    def __init__(self, name: str, interval_sec: float) -> None:
        super().__init__(name=name, daemon=True)
        self._interval = interval_sec
        self._stop_event = threading.Event()

    def tick(self) -> None:
        raise NotImplementedError

    def run(self) -> None:
        logger.info("daemon %s started (interval %ss)", self.name, self._interval)
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("daemon %s tick failed", self.name)
            self._stop_event.wait(self._interval)
        logger.info("daemon %s stopped", self.name)

    def stop(self) -> None:
        self._stop_event.set()
