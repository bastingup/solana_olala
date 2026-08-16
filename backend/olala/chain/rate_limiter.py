"""Adaptive token-bucket rate limiter shared by all chain clients.

Every outbound request in the system passes through one of these buckets,
so daemons cannot starve each other or trip endpoint bans. The bucket is
adaptive: a 429 from the provider halves the issue rate and opens a
cooldown window during which nothing is issued at all (AIMD). The rate
then climbs back toward the configured ceiling while responses stay
clean, so a burst of throttling slows the whole system down instead of
being retried into a wall.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

BACKOFF_FACTOR = 0.5
RECOVERY_FACTOR = 1.15
RECOVERY_INTERVAL_SEC = 20.0
MIN_RATE_FRACTION = 0.05
DEFAULT_COOLDOWN_SEC = 2.0
MAX_COOLDOWN_SEC = 30.0


class RateLimiter:
    def __init__(self, requests_per_second: float, burst: int = 4) -> None:
        self._ceiling = max(requests_per_second, 0.1)
        self._rate = self._ceiling
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = threading.Lock()
        self._blocked_until = 0.0
        self._last_penalty = 0.0
        self._last_recovery = time.monotonic()
        self._cooldown = DEFAULT_COOLDOWN_SEC

    # -- issuing -----------------------------------------------------------

    def acquire(self) -> None:
        """Block until a request slot is available."""
        while True:
            with self._lock:
                now = time.monotonic()
                if now < self._blocked_until:
                    wait = self._blocked_until - now
                else:
                    self._recover_locked(now)
                    self._tokens = min(
                        self._capacity,
                        self._tokens + (now - self._updated) * self._rate)
                    self._updated = now
                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        return
                    wait = (1.0 - self._tokens) / self._rate
            time.sleep(min(wait, 5.0))

    # -- feedback ----------------------------------------------------------

    def penalize(self, retry_after_sec: float | None = None) -> float:
        """Report a rate-limit rejection: halve the rate and pause.

        Returns the cooldown actually applied, for logging.
        """
        with self._lock:
            now = time.monotonic()
            floor = self._ceiling * MIN_RATE_FRACTION
            # Repeated penalties inside one episode compound the cooldown;
            # an isolated 429 stays cheap.
            if now - self._last_penalty < self._cooldown + 5.0:
                self._cooldown = min(self._cooldown * 2.0, MAX_COOLDOWN_SEC)
            else:
                self._cooldown = DEFAULT_COOLDOWN_SEC
            self._rate = max(self._rate * BACKOFF_FACTOR, floor)
            cooldown = (retry_after_sec if retry_after_sec is not None
                        else self._cooldown)
            cooldown = min(max(cooldown, 0.5), MAX_COOLDOWN_SEC)
            self._blocked_until = now + cooldown
            self._last_penalty = now
            self._last_recovery = now + cooldown
            self._tokens = 0.0
            self._updated = now
            return cooldown

    def _recover_locked(self, now: float) -> None:
        """Climb back toward the ceiling while things stay clean."""
        if self._rate >= self._ceiling:
            return
        if now - self._last_recovery < RECOVERY_INTERVAL_SEC:
            return
        self._rate = min(self._rate * RECOVERY_FACTOR, self._ceiling)
        self._last_recovery = now

    @property
    def current_rate(self) -> float:
        with self._lock:
            return self._rate

    @property
    def throttled(self) -> bool:
        with self._lock:
            return (self._rate < self._ceiling * 0.99
                    or time.monotonic() < self._blocked_until)
