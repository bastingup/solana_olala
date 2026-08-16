"""ATR tracking from streamed price marks.

Price samples arrive from the marking daemon; they are bucketed into
one-minute candles per mint, and a Wilder-smoothed Average True Range is
maintained once enough candles exist. Until then no stop is armed — the
panic stop is deliberately generous, never premature.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

CANDLE_SECONDS = 60.0
MAX_CANDLES = 500


@dataclass
class Candle:
    start: float
    high: float
    low: float
    close: float


class AtrTracker:
    def __init__(self, period: int) -> None:
        self._period = period
        self._lock = threading.Lock()
        self._candles: dict[str, deque[Candle]] = {}
        self._atr: dict[str, float] = {}

    def add_sample(self, mint: str, timestamp: float, price: float) -> None:
        if price <= 0:
            return
        with self._lock:
            candles = self._candles.setdefault(
                mint, deque(maxlen=MAX_CANDLES))
            bucket = timestamp - (timestamp % CANDLE_SECONDS)
            if candles and candles[-1].start == bucket:
                candle = candles[-1]
                candle.high = max(candle.high, price)
                candle.low = min(candle.low, price)
                candle.close = price
            else:
                # Recompute BEFORE appending the new stub so the candle
                # that just completed contributes its full range — a stub
                # with one sample has no range and would halve the ATR.
                self._recompute(mint)
                candles.append(Candle(bucket, price, price, price))

    def _recompute(self, mint: str) -> None:
        candles = self._candles.get(mint)
        if not candles or len(candles) < self._period + 1:
            return
        window = list(candles)[-(self._period + 1):]
        true_ranges = []
        for prev, current in zip(window, window[1:]):
            true_ranges.append(max(
                current.high - current.low,
                abs(current.high - prev.close),
                abs(current.low - prev.close)))
        previous = self._atr.get(mint)
        latest = true_ranges[-1]
        if previous is None:
            self._atr[mint] = sum(true_ranges) / len(true_ranges)
        else:
            self._atr[mint] = (previous * (self._period - 1) + latest) / self._period

    def atr(self, mint: str) -> float | None:
        with self._lock:
            return self._atr.get(mint)

    def stop_price(self, mint: str, peak_price: float,
                   multiplier: float) -> float:
        """Trailing panic-stop level; 0.0 while ATR is still warming up."""
        atr = self.atr(mint)
        if atr is None:
            return 0.0
        return max(peak_price - multiplier * atr, 0.0)

    def forget(self, mint: str) -> None:
        with self._lock:
            self._candles.pop(mint, None)
            self._atr.pop(mint, None)
