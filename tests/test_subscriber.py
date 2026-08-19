"""TraderSubscriber: payload parsing, off-thread dispatch, health.

The old subscriber discarded the notification payload and re-derived the
signature with an extra RPC call, then ran the handler synchronously on
the socket thread — so one live execution, up to a hundred seconds of
confirmation polling, blocked every other notification on that socket.
"""

import json
import time

from olala.chain.subscriber import TraderSubscriber
from olala.domain.models import TraderProfile, TraderStatus
from olala.services.traders import TraderRegistry

from fakes import FakeProvider

TRADER = "TraderAAAA1111111111111111111111111111111111"
OTHER = "TraderBBBB2222222222222222222222222222222222"


def make_subscriber(db, bus, **kwargs):
    registry = TraderRegistry(db, bus)
    registry.update(TraderProfile(address=TRADER,
                                  status=TraderStatus.FOLLOWED,
                                  assigned_wallet_id="w1"))
    seen = []
    alive = []
    subscriber = TraderSubscriber(
        FakeProvider(), registry,
        on_activity=lambda a, s, sl: seen.append((a, s, sl)),
        on_alive=lambda: alive.append(time.time()), **kwargs)
    return subscriber, registry, seen, alive


def notification(subscription, signature, slot, err=None):
    return json.dumps({
        "jsonrpc": "2.0", "method": "logsNotification",
        "params": {"subscription": subscription,
                   "result": {"context": {"slot": slot},
                              "value": {"signature": signature,
                                        "err": err, "logs": []}}}})


def test_notification_payload_carries_the_signature_and_slot(db, bus):
    """It is already in the message; rediscovering it cost an RPC call."""
    subscriber, _, seen, _ = make_subscriber(db, bus)
    sub_to_addr = {7: TRADER}
    subscriber._on_message(notification(7, "SIG-ABC", 12345), {},
                           sub_to_addr, {TRADER: 7})
    address, signature, slot = subscriber._inbox.get_nowait()
    assert (address, signature, slot) == (TRADER, "SIG-ABC", 12345)


def test_reverted_transactions_are_not_dispatched_but_prove_liveness(db, bus):
    subscriber, _, _, alive = make_subscriber(db, bus)
    subscriber._on_message(notification(7, "SIG-BAD", 1, err={"x": 1}), {},
                           {7: TRADER}, {TRADER: 7})
    assert subscriber._inbox.empty()
    assert alive                      # the subscription is delivering


def test_a_confirmed_subscription_is_proof_of_life(db, bus):
    """logsSubscribe dies silently on public RPC, so only positive proof
    counts — a quiet market must not look like a dead socket."""
    subscriber, _, _, alive = make_subscriber(db, bus)
    pending = {1: TRADER}
    sub_to_addr, addr_to_sub = {}, {}
    subscriber._on_message(json.dumps({"id": 1, "result": 42}),
                           pending, sub_to_addr, addr_to_sub)
    assert sub_to_addr == {42: TRADER}
    assert addr_to_sub == {TRADER: 42}
    assert alive


def test_a_rejected_subscription_is_not_proof_of_life(db, bus):
    subscriber, _, _, alive = make_subscriber(db, bus)
    subscriber._on_message(
        json.dumps({"id": 1, "error": {"code": -32601,
                                       "message": "not supported"}}),
        {1: TRADER}, {}, {})
    assert alive == []


def test_notifications_for_unknown_subscriptions_are_ignored(db, bus):
    subscriber, _, _, _ = make_subscriber(db, bus)
    subscriber._on_message(notification(99, "SIG", 1), {}, {}, {})
    assert subscriber._inbox.empty()


def test_malformed_messages_do_not_raise(db, bus):
    subscriber, _, _, _ = make_subscriber(db, bus)
    for raw in ("not json", "[]", json.dumps({"method": "other"}), ""):
        subscriber._on_message(raw, {}, {}, {})
    assert subscriber._inbox.empty()


def test_handlers_run_off_the_socket_thread(db, bus):
    """A confirming swap must not stall every other notification."""
    subscriber, _, seen, _ = make_subscriber(db, bus)
    subscriber._on_message(notification(7, "SIG-1", 5), {}, {7: TRADER},
                           {TRADER: 7})
    # The reader only enqueued; nothing has been handled yet.
    assert seen == []
    assert subscriber._inbox.qsize() == 1


def test_backlog_is_shed_rather_than_grown_without_bound(db, bus):
    subscriber, _, _, _ = make_subscriber(db, bus)
    subscriber._inbox.maxsize = 2
    for index in range(5):
        subscriber._on_message(notification(7, f"SIG-{index}", index), {},
                               {7: TRADER}, {TRADER: 7})
    assert subscriber._inbox.qsize() == 2
    assert subscriber.shed == 3       # the sweep is the safety net


# -- subscription reconciliation ------------------------------------------

class RecordingConnection:
    def __init__(self):
        self.sent = []

    def send(self, raw):
        self.sent.append(json.loads(raw))


def test_new_followed_traders_are_subscribed(db, bus):
    subscriber, registry, _, _ = make_subscriber(db, bus)
    connection = RecordingConnection()
    subscriber._reconcile(connection, 0, {}, {}, {})
    methods = [m["method"] for m in connection.sent]
    assert methods == ["logsSubscribe"]
    assert connection.sent[0]["params"][0] == {"mentions": [TRADER]}


def test_unfollowed_traders_are_unsubscribed(db, bus):
    """Leaking subscriptions is how a public node starts refusing us."""
    subscriber, registry, _, _ = make_subscriber(db, bus)
    connection = RecordingConnection()
    sub_to_addr = {5: OTHER}
    addr_to_sub = {OTHER: 5}
    subscriber._reconcile(connection, 0, {}, sub_to_addr, addr_to_sub)
    methods = [m["method"] for m in connection.sent]
    assert "logsUnsubscribe" in methods
    # The mapping is cleared in both directions, so a later notification
    # on that subscription id cannot be attributed to anyone.
    assert addr_to_sub == {}
    assert sub_to_addr == {}


def test_pending_subscriptions_are_not_requested_twice(db, bus):
    subscriber, _, _, _ = make_subscriber(db, bus)
    connection = RecordingConnection()
    pending = {1: TRADER}
    subscriber._reconcile(connection, 1, pending, {}, {})
    assert connection.sent == []


# -- degraded state --------------------------------------------------------

def test_missing_websocket_library_is_a_visible_state(db, bus, monkeypatch):
    """It used to kill the thread forever behind a single log line."""
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "websocket":
            raise ImportError("no module named websocket")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    subscriber, _, _, _ = make_subscriber(db, bus)
    subscriber.run()

    assert subscriber.degraded_reason
    assert subscriber.healthy is False
    assert "websocket-client" in subscriber.degraded_reason


def test_status_reports_what_the_operator_needs(db, bus):
    subscriber, _, _, _ = make_subscriber(db, bus)
    status = subscriber.status()
    assert set(status) == {"connected", "notifications", "shed", "queued",
                           "degraded_reason"}
