import queue

from olala.events import MAX_QUEUE_SIZE, EventBus


def test_publish_reaches_all_subscribers(bus):
    q1, q2 = bus.subscribe(), bus.subscribe()
    bus.publish("ping", {"n": 1})
    for q in (q1, q2):
        event = q.get_nowait()
        assert event["type"] == "ping"
        assert event["data"] == {"n": 1}
        assert "ts" in event


def test_unsubscribe_stops_delivery(bus):
    q = bus.subscribe()
    bus.unsubscribe(q)
    bus.publish("ping", {})
    try:
        q.get_nowait()
    except queue.Empty:
        return
    raise AssertionError("unsubscribed queue still received an event")


def test_slow_client_drops_oldest_not_newest(bus):
    q = bus.subscribe()
    for n in range(MAX_QUEUE_SIZE + 10):
        bus.publish("tick", {"n": n})
    drained = []
    while True:
        try:
            drained.append(q.get_nowait()["data"]["n"])
        except queue.Empty:
            break
    assert len(drained) == MAX_QUEUE_SIZE
    assert drained[-1] == MAX_QUEUE_SIZE + 9  # newest survived
    assert drained[0] == 10  # oldest were dropped
