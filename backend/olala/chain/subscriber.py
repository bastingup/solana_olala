"""Push notifications for followed traders.

Opens one WebSocket to the RPC provider and issues a ``logsSubscribe``
(mentions filter) per followed trader. When a notification arrives, the
follower is poked to poll that trader immediately — the copy happens
seconds after the trader's transaction lands instead of on the next poll
tick. The interval poll remains as the safety net, so a dropped WebSocket
degrades to the previous behavior, never to silence.

The triggered polls run through the follower's existing cursor protocol,
so ordering, deduplication, and budget rules are identical to polled
trades.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from ..chain.provider import RpcProvider
from ..services.traders import TraderRegistry

logger = logging.getLogger(__name__)

RECONNECT_MIN_SEC = 2.0
RECONNECT_MAX_SEC = 60.0
RECONCILE_INTERVAL_SEC = 20.0
DEBOUNCE_SEC = 1.5


class TraderSubscriber(threading.Thread):
    """Maintains logsSubscribe subscriptions for the followed set."""

    def __init__(self, provider: RpcProvider, registry: TraderRegistry,
                 on_activity) -> None:
        super().__init__(name="subscriber", daemon=True)
        self._provider = provider
        self._registry = registry
        self._on_activity = on_activity
        self._stop_event = threading.Event()
        self._last_poke: dict[str, float] = {}

    def run(self) -> None:
        try:
            import websocket
        except ImportError:
            logger.warning("websocket-client not installed; falling back "
                           "to interval polling only")
            return
        backoff = RECONNECT_MIN_SEC
        while not self._stop_event.is_set():
            try:
                self._session(websocket)
                backoff = RECONNECT_MIN_SEC
            except Exception as exc:
                logger.warning("subscription socket dropped: %s", exc)
            self._stop_event.wait(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SEC)

    def stop(self) -> None:
        self._stop_event.set()

    # -- session -----------------------------------------------------------

    def _session(self, websocket) -> None:
        endpoint = self._provider.ws_endpoint()
        connection = websocket.create_connection(endpoint, timeout=30)
        logger.info("subscription socket connected (%s traders followed)",
                    len(self._registry.followed()))
        request_id = 0
        sub_to_addr: dict[int, str] = {}
        addr_to_sub: dict[str, int] = {}
        pending: dict[int, str] = {}
        last_reconcile = 0.0
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if now - last_reconcile > RECONCILE_INTERVAL_SEC:
                    last_reconcile = now
                    followed = {p.address for p in self._registry.followed()}
                    for address in followed - set(addr_to_sub):
                        request_id += 1
                        pending[request_id] = address
                        connection.send(json.dumps({
                            "jsonrpc": "2.0", "id": request_id,
                            "method": "logsSubscribe",
                            "params": [{"mentions": [address]},
                                       {"commitment": "confirmed"}]}))
                    for address in set(addr_to_sub) - followed:
                        request_id += 1
                        connection.send(json.dumps({
                            "jsonrpc": "2.0", "id": request_id,
                            "method": "logsUnsubscribe",
                            "params": [addr_to_sub[address]]}))
                        sub_to_addr.pop(addr_to_sub.pop(address), None)

                connection.settimeout(5)
                try:
                    raw = connection.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                message = json.loads(raw)
                if "id" in message and message["id"] in pending:
                    address = pending.pop(message["id"])
                    sub_id = message.get("result")
                    if isinstance(sub_id, int):
                        sub_to_addr[sub_id] = address
                        addr_to_sub[address] = sub_id
                elif message.get("method") == "logsNotification":
                    sub_id = (message.get("params") or {}).get("subscription")
                    address = sub_to_addr.get(sub_id)
                    if address:
                        self._poke(address)
        finally:
            connection.close()

    def _poke(self, address: str) -> None:
        now = time.monotonic()
        if now - self._last_poke.get(address, 0.0) < DEBOUNCE_SEC:
            return
        self._last_poke[address] = now
        logger.info("on-chain activity from %s — polling now", address[:8])
        try:
            self._on_activity(address)
        except Exception:
            logger.exception("activity handler failed for %s", address)
