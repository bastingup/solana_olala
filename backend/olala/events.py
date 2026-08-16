"""Thread-safe publish/subscribe bus feeding the WebSocket stream.

Every daemon publishes typed events here; each connected WebSocket client
holds its own queue so a slow client never blocks producers.
"""

from __future__ import annotations

import math
import queue
import threading
import time
from typing import Any

MAX_QUEUE_SIZE = 500


def json_safe(value: Any) -> Any:
    """Replace non-finite floats with None, recursively.

    Python's json module serializes inf/nan as ``Infinity``/``NaN`` —
    tokens strict JSON parsers (every browser) reject, which makes the
    entire enclosing message undeliverable. Every payload crossing the
    process boundary passes through here so one bad float can never
    poison a snapshot or event again.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        event = {"type": kind, "ts": time.time(), "data": data}
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Drop oldest event for a lagging client rather than block.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass
