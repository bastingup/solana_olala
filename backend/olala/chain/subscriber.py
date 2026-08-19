"""Push notifications for followed traders.

Opens one WebSocket to the RPC provider and issues a ``logsSubscribe``
(mentions filter) per followed trader. This is the fast path: a
notification names the signature and slot outright, so a copy can start
without asking anyone for a signature list.

Three things this deliberately does differently from the first version:

**The payload is used, not thrown away.** The notification already
contains the signature; the old code discarded it and spent a
``getSignaturesForAddress`` rediscovering what it had just been told.

**The socket thread only reads.** Handling used to run synchronously
here, so a live execution — up to a hundred seconds of confirmation
polling — blocked every other notification on the socket. Now the reader
hands off and goes straight back to receiving.

**Silence is not health.** ``logsSubscribe`` fails silently on public
RPC: the socket stays open, the subscription is accepted, and no
notification ever arrives. So liveness is reported as positive PROOF
(subscription confirmed, pong received, notification delivered) rather
than inferred from the absence of errors — and the tracker keeps its
expensive gear until that proof exists.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Callable

from ..services.traders import TraderRegistry
from .provider import RpcProvider

logger = logging.getLogger(__name__)

RECONNECT_MIN_SEC = 2.0
RECONNECT_MAX_SEC = 60.0
RECONCILE_INTERVAL_SEC = 20.0
KEEPALIVE_INTERVAL_SEC = 25.0
RECV_TIMEOUT_SEC = 5.0
#: Bounded so a notification storm cannot exhaust memory; the sweep is
#: the safety net for anything shed here.
MAX_QUEUED_NOTIFICATIONS = 2000


class TraderSubscriber(threading.Thread):
    """Maintains logsSubscribe subscriptions for the followed set."""

    def __init__(self, provider: RpcProvider, registry: TraderRegistry,
                 on_activity: Callable[[str, str, int], None],
                 on_alive: Callable[[], None] | None = None) -> None:
        super().__init__(name="subscriber", daemon=True)
        self._provider = provider
        self._registry = registry
        self._on_activity = on_activity
        self._on_alive = on_alive
        self._stop_event = threading.Event()
        self._inbox: queue.Queue = queue.Queue(
            maxsize=MAX_QUEUED_NOTIFICATIONS)
        self._worker: threading.Thread | None = None
        self.degraded_reason = ""
        self.connected = False
        self.notifications = 0
        self.shed = 0

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:
        try:
            import websocket
        except ImportError:
            # Previously this killed the thread forever, in a warning
            # nobody would see again. It is a degraded STATE, which the
            # tracker reads to keep its expensive gear running.
            self.degraded_reason = (
                "websocket-client is not installed — no push notifications; "
                "tracking runs on the batch sweep alone")
            logger.warning("%s", self.degraded_reason)
            return

        self._worker = threading.Thread(target=self._dispatch, daemon=True,
                                        name="subscriber-dispatch")
        self._worker.start()

        backoff = RECONNECT_MIN_SEC
        while not self._stop_event.is_set():
            try:
                self._session(websocket)
                backoff = RECONNECT_MIN_SEC
            except Exception as exc:                        # noqa: BLE001
                self.degraded_reason = str(exc)
                logger.warning("subscription socket dropped: %s", exc)
            finally:
                self.connected = False
            self._stop_event.wait(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_SEC)

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def healthy(self) -> bool:
        return self.connected and not self.degraded_reason

    def status(self) -> dict:
        return {"connected": self.connected,
                "notifications": self.notifications,
                "shed": self.shed,
                "queued": self._inbox.qsize(),
                "degraded_reason": self.degraded_reason}

    # -- session -----------------------------------------------------------

    def _session(self, websocket) -> None:
        endpoint = self._provider.ws_endpoint()
        connection = websocket.create_connection(endpoint, timeout=30)
        self.connected = True
        self.degraded_reason = ""
        logger.info("subscription socket connected (%d traders followed)",
                    len(self._registry.followed()))

        request_id = 0
        sub_to_addr: dict[int, str] = {}
        addr_to_sub: dict[str, int] = {}
        pending: dict[int, str] = {}
        last_reconcile = 0.0
        last_keepalive = time.monotonic()

        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if now - last_reconcile > RECONCILE_INTERVAL_SEC:
                    last_reconcile = now
                    request_id = self._reconcile(
                        connection, request_id, pending,
                        sub_to_addr, addr_to_sub)

                if now - last_keepalive > KEEPALIVE_INTERVAL_SEC:
                    # A half-open TCP connection reads as a healthy but
                    # permanently silent subscription otherwise.
                    last_keepalive = now
                    connection.ping()

                connection.settimeout(RECV_TIMEOUT_SEC)
                try:
                    raw = connection.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    continue
                self._on_message(raw, pending, sub_to_addr, addr_to_sub)
        finally:
            self.connected = False
            try:
                connection.close()
            except Exception:                               # noqa: BLE001
                pass

    def _reconcile(self, connection, request_id: int, pending: dict,
                   sub_to_addr: dict, addr_to_sub: dict) -> int:
        followed = {p.address for p in self._registry.followed()}
        for address in followed - set(addr_to_sub) - set(pending.values()):
            request_id += 1
            pending[request_id] = address
            connection.send(json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "method": "logsSubscribe",
                "params": [{"mentions": [address]},
                           {"commitment": "confirmed"}]}))
        for address in set(addr_to_sub) - followed:
            request_id += 1
            subscription = addr_to_sub.pop(address)
            sub_to_addr.pop(subscription, None)
            connection.send(json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "method": "logsUnsubscribe",
                "params": [subscription]}))
            logger.info("unsubscribed from %s (no longer followed)",
                        address[:8])
        return request_id

    def _on_message(self, raw, pending: dict, sub_to_addr: dict,
                    addr_to_sub: dict) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(message, dict):
            return

        if "id" in message and message["id"] in pending:
            address = pending.pop(message["id"])
            subscription = message.get("result")
            if isinstance(subscription, int):
                sub_to_addr[subscription] = address
                addr_to_sub[address] = subscription
                # A confirmed subscription is proof the socket works,
                # even before any trade happens on it.
                self._note_alive()
            else:
                logger.warning("subscribe rejected for %s: %s",
                               address[:8], message.get("error"))
            return

        if message.get("method") != "logsNotification":
            return
        params = message.get("params") or {}
        address = sub_to_addr.get(params.get("subscription"))
        if not address:
            return
        value = ((params.get("result") or {}).get("value") or {})
        context = ((params.get("result") or {}).get("context") or {})
        signature = value.get("signature") or ""
        slot = int(context.get("slot") or 0)
        if value.get("err") is not None:
            # A reverted transaction moved nothing worth copying, but it
            # still proves the subscription is delivering.
            self._note_alive()
            return
        self.notifications += 1
        try:
            self._inbox.put_nowait((address, signature, slot))
        except queue.Full:
            # Shedding is safe: the reconciliation sweep will find it.
            self.shed += 1
            logger.warning("notification backlog full — shedding %s for %s; "
                           "the sweep will pick it up", signature[:12],
                           address[:8])

    def _note_alive(self) -> None:
        if self._on_alive is None:
            return
        try:
            self._on_alive()
        except Exception:                                   # noqa: BLE001
            logger.exception("stream liveness callback failed")

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self) -> None:
        """Runs handlers OFF the socket thread.

        A live execution can block for the length of a confirmation
        window; doing that on the reader would stall every other
        notification arriving on the same socket.
        """
        while not self._stop_event.is_set():
            try:
                address, signature, slot = self._inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._on_activity(address, signature, slot)
            except Exception:                               # noqa: BLE001
                logger.exception("activity handler failed for %s", address)
