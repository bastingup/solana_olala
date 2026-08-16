"""WebSocket stream: the frontend's live view of the system.

Each client gets a full snapshot on connect, then every event the daemons
publish, in order. A ping frame goes out during quiet periods so both sides
notice dead connections.
"""

from __future__ import annotations

import json
import logging
import queue

from flask_sock import Sock
from simple_websocket import ConnectionClosed

from ..events import json_safe

logger = logging.getLogger(__name__)

PING_INTERVAL_SEC = 15


def _frame(payload) -> str:
    # allow_nan=False is the backstop: json_safe() already nulled every
    # non-finite float, so a failure here means a new unserializable type
    # slipped in — better one loud error than a silently dead frontend.
    return json.dumps(json_safe(payload), allow_nan=False)


def register_stream(sock: Sock, app_context) -> None:
    ctx = app_context

    @sock.route("/ws")
    def stream(ws):  # noqa: ANN001 - flask-sock supplies the socket type
        events = ctx.bus.subscribe()
        logger.info("websocket client connected")
        try:
            ws.send(_frame({"type": "snapshot", "data": ctx.snapshot()}))
            while True:
                try:
                    event = events.get(timeout=PING_INTERVAL_SEC)
                except queue.Empty:
                    ws.send(_frame({"type": "ping", "data": {}}))
                    continue
                ws.send(_frame(event))
        except ConnectionClosed:
            pass
        finally:
            ctx.bus.unsubscribe(events)
            logger.info("websocket client disconnected")
