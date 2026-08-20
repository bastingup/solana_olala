"""RpcRouter: ordered fall-through, breakers, batching, pinned sessions.

Two behaviours here are money-safety properties, not conveniences:

* a ``SourceRejected`` must NOT fail over — asking four more nodes the
  same malformed question multiplies damage and hides the real fault;
* a pinned session must ABORT rather than silently switch source, because
  confirming a swap on a node that never saw it turns "still confirming"
  into "definitively never landed".
"""

import time

import pytest

from olala.chain.errors import (SourceRateLimited, SourceRejected,
                                SourceUnavailable)
from olala.chain.router import NoSourceAvailable, RpcRouter
from olala.chain.sources.base import BatchItem, SourceStats
from olala.config import AppConfig, RoutingConfig, SourceConfig


class FakeSource:
    """A source whose every answer is scripted."""

    def __init__(self, name, *, results=None, error=None, supports_batch=False,
                 max_batch=10, enabled=True, budget=True, unsupported=()):
        self.name = name
        self.supports_batch = supports_batch
        self.max_batch = max_batch
        self.enabled = enabled
        self.metered = False
        self._results = list(results or [])
        self._error = error
        self._budget = budget
        self._unsupported = set(unsupported)
        self.stats = SourceStats()
        self.calls = []
        self.responding = True
        self.batches = []
        self.reservations = []

    def ws_endpoint(self):
        return f"wss://{self.name}.test"

    def supports(self, method):
        return method not in self._unsupported

    def try_reserve(self, cost=1.0, timeout=None):
        self.reservations.append(cost)
        return self._budget

    def call(self, method, params):
        self.calls.append((method, params))
        if self._error is not None:
            self.stats.last_failure_at = time.time()
            raise self._error
        self.stats.last_ok_at = time.time()
        return self._results.pop(0) if self._results else f"{self.name}-ok"

    def batch(self, items):
        self.batches.append(list(items))
        if self._error is not None:
            raise self._error
        return [f"{self.name}-{i}" for i in range(len(items))]


def make_router(sources, **policies):
    config = AppConfig()
    # The router only reads routing + source names from config.
    names = list(sources)
    defaults = {p: names for p in ("tracking", "history", "metadata",
                                   "broadcast", "confirm", "stream")}
    defaults.update(policies)
    config.routing = RoutingConfig(**defaults)
    config.sources = {n: SourceConfig(endpoints=["https://x"]) for n in names}
    return RpcRouter(config, sources=sources, reserve_timeout_sec=0.01)


# -- ordered fall-through --------------------------------------------------

def test_first_healthy_source_serves_the_call():
    a, b = FakeSource("a"), FakeSource("b")
    router = make_router({"a": a, "b": b})
    assert router.call("history", "getBalance", ["x"]) == "a-ok"
    assert b.calls == []


def test_failure_falls_through_to_the_next_source_in_order():
    a = FakeSource("a", error=SourceUnavailable("down", source="a"))
    b = FakeSource("b")
    router = make_router({"a": a, "b": b})
    assert router.call("history", "getBalance", ["x"]) == "b-ok"
    assert router.metrics()["failovers"] == 1


def test_rate_limited_source_is_skipped_for_the_next():
    a = FakeSource("a", error=SourceRateLimited("429", source="a"))
    b = FakeSource("b")
    router = make_router({"a": a, "b": b})
    assert router.call("tracking", "getSignaturesForAddress", ["w"]) == "b-ok"


def test_a_source_with_no_budget_is_skipped_not_waited_on():
    """Proactive fall-through: queueing behind a throttled source is what
    makes one slow endpoint stall the whole poll cycle."""
    a = FakeSource("a", budget=False)
    b = FakeSource("b")
    router = make_router({"a": a, "b": b})
    assert router.call("tracking", "getBalance", ["x"]) == "b-ok"
    assert a.calls == []                      # never even attempted


def test_rejected_request_does_not_fail_over():
    a = FakeSource("a", error=SourceRejected("bad pubkey", source="a"))
    b = FakeSource("b")
    router = make_router({"a": a, "b": b})
    with pytest.raises(SourceRejected):
        router.call("history", "getBalance", ["nonsense"])
    assert b.calls == []


def test_unsupported_method_skips_that_source_entirely():
    a = FakeSource("a", unsupported={"getTokenLargestAccounts"})
    b = FakeSource("b")
    router = make_router({"a": a, "b": b})
    assert router.call("metadata", "getTokenLargestAccounts", ["m"]) == "b-ok"
    assert a.calls == []


def test_disabled_sources_are_never_used():
    a = FakeSource("a", enabled=False)
    b = FakeSource("b")
    router = make_router({"a": a, "b": b})
    router.call("history", "getBalance", ["x"])
    assert a.calls == []


def test_every_source_failing_raises_with_the_whole_story():
    a = FakeSource("a", error=SourceUnavailable("a down", source="a"))
    b = FakeSource("b", error=SourceUnavailable("b down", source="b"))
    router = make_router({"a": a, "b": b})
    with pytest.raises(NoSourceAvailable) as excinfo:
        router.call("history", "getBalance", ["x"])
    message = str(excinfo.value)
    assert "history" in message and "getBalance" in message


def test_policy_with_no_usable_source_raises():
    router = make_router({"a": FakeSource("a")}, tracking=[])
    with pytest.raises(NoSourceAvailable):
        router.call("tracking", "getBalance", ["x"])


# -- circuit breaker -------------------------------------------------------

def test_repeated_failures_open_the_breaker_and_stop_the_attempts():
    a = FakeSource("a", error=SourceUnavailable("down", source="a"))
    b = FakeSource("b")
    router = make_router({"a": a, "b": b})
    for _ in range(4):
        router.call("history", "getBalance", ["x"])
    # After the breaker opens, the dead source is no longer attempted.
    attempts_before = len(a.calls)
    router.call("history", "getBalance", ["x"])
    assert len(a.calls) == attempts_before
    assert "a" not in router.available("history")


# -- batching --------------------------------------------------------------

def test_batch_picks_the_preferred_batch_capable_source():
    plain = FakeSource("plain", supports_batch=False)
    batcher = FakeSource("batcher", supports_batch=True)
    router = make_router({"plain": plain, "batcher": batcher})
    results = router.batch("tracking", [BatchItem("getBalance", ["w"])])
    assert results == ["batcher-0"]
    assert router.batch_capable("tracking") == "batcher"


def test_batch_is_chunked_to_the_measured_max_batch():
    """Helius refuses 50 elements but serves 10; the roster may be any
    size, so chunking belongs here rather than in every caller."""
    batcher = FakeSource("b", supports_batch=True, max_batch=10)
    router = make_router({"b": batcher})
    items = [BatchItem("getSignaturesForAddress", [f"w{i}"])
             for i in range(25)]
    results = router.batch("tracking", items)
    assert [len(chunk) for chunk in batcher.batches] == [10, 10, 5]
    assert len(results) == 25


def test_batch_charges_the_sum_of_sub_call_costs():
    """Public nodes meter per sub-call: 10 addresses cost 10, not 1."""
    batcher = FakeSource("b", supports_batch=True, max_batch=10)
    router = make_router({"b": batcher})
    router.batch("tracking", [BatchItem("getBalance", [f"w{i}"], cost=1.0)
                              for i in range(10)])
    assert batcher.reservations == [10.0]


def test_no_batch_capable_source_is_reported_not_faked():
    """The tracker needs a truthful answer here — it downshifts to
    round-robin rather than pretending a batch happened."""
    plain = FakeSource("plain", supports_batch=False)
    router = make_router({"plain": plain})
    assert router.batch_capable("tracking") is None
    with pytest.raises(NoSourceAvailable):
        router.batch("tracking", [BatchItem("getBalance", ["w"])])


# -- pinned sessions -------------------------------------------------------

def test_session_pins_one_source_for_every_call():
    a, b = FakeSource("a"), FakeSource("b")
    router = make_router({"a": a, "b": b})
    with router.session("broadcast") as pinned:
        pinned.call("sendTransaction", ["tx"])
        pinned.call("getSignatureStatuses", [["sig"]])
    assert len(a.calls) == 2
    assert b.calls == []


def test_session_can_be_pinned_to_a_named_source():
    a, b = FakeSource("a"), FakeSource("b")
    router = make_router({"a": a, "b": b})
    with router.session("confirm", pin="b") as pinned:
        assert pinned.source_name == "b"
        pinned.call("getSignatureStatuses", [["sig"]])
    assert a.calls == []


def test_session_failure_aborts_instead_of_switching_source():
    """Confirming on a node that never saw the transaction reports null,
    which the executor would read as 'definitively never landed'."""
    a = FakeSource("a", error=SourceUnavailable("died mid-sequence",
                                                source="a"))
    b = FakeSource("b")
    router = make_router({"a": a, "b": b})
    with pytest.raises(SourceUnavailable):
        with router.session("broadcast") as pinned:
            pinned.call("getSignatureStatuses", [["sig"]])
    assert b.calls == []


def test_session_without_any_available_source_raises():
    router = make_router({"a": FakeSource("a", enabled=False)})
    with pytest.raises(NoSourceAvailable):
        with router.session("broadcast"):
            pass


# -- observability ---------------------------------------------------------

def test_metrics_report_who_served_what():
    a = FakeSource("a", error=SourceUnavailable("down", source="a"))
    b = FakeSource("b")
    router = make_router({"a": a, "b": b})
    router.call("history", "getBalance", ["x"])
    metrics = router.metrics()
    assert metrics["routed"] == {"b": 1}
    assert metrics["failovers"] == 1
    assert set(metrics["sources"]) == {"a", "b"}


# -- the confirmation money bug --------------------------------------------

def test_confirmation_is_answered_by_the_broadcasting_source():
    """The bug this pinning exists to prevent.

    Broadcast on 'fast', confirm on 'slow' — which never saw the
    transaction and honestly answers null. Unpinned, the executor reads
    that null as 'definitively never landed' and writes a TIMEOUT receipt
    while the swap is actually confirming, so the position is never
    booked and the trader is copied out of sync.
    """
    from olala.chain.provider import RoutedProvider

    landed = {"value": [{"confirmationStatus": "confirmed", "slot": 42}]}
    fast = FakeSource("fast", results=["SIGNATURE", landed])
    slow = FakeSource("slow", results=[{"value": [None]}])
    router = make_router({"fast": fast, "slow": slow})
    provider = RoutedProvider(router)

    with provider.broadcast_session() as channel:
        signature = channel.send_transaction("base64tx")
        status = channel.get_signature_status(signature)

    assert signature == "SIGNATURE"
    assert status is not None and status["slot"] == 42
    assert slow.calls == []          # never consulted about our own tx


def test_single_source_provider_needs_no_pinning():
    """The base contract: a provider with one source is already pinned,
    so the executor's session works identically in tests and in paper."""
    from olala.chain.provider import RoutedProvider

    only = FakeSource("only", results=["SIG", {"value": [{"slot": 1}]}])
    provider = RoutedProvider(make_router({"only": only}))
    with provider.broadcast_session() as channel:
        assert channel.send_transaction("tx") == "SIG"


# -- what the operator sees ------------------------------------------------
#
# A standby endpoint of unknown health is not a fall-through you can
# trust. These states are what make the difference between "we have not
# needed this one" and "this one is dead" visible.

def test_an_uncontacted_source_claims_nothing():
    router = make_router({"a": FakeSource("a")})
    assert router.source_state("a") == "unknown"


def test_a_source_serving_routed_traffic_is_active():
    router = make_router({"a": FakeSource("a")})
    router.call("history", "getBalance", ["x"])
    assert router.source_state("a") == "active"


def test_a_source_that_only_answered_a_probe_is_ready_not_active():
    """A health probe goes straight to the source, bypassing the router.
    Counting it as traffic made an idle standby look like it was carrying
    the load for ten seconds out of every thirty."""
    import time as _time

    standby = FakeSource("standby")
    router = make_router({"standby": standby})
    # Exactly what SourceHealthDaemon does: call the source directly.
    standby.call("getHealth", [])
    standby.stats.last_ok_at = _time.time()

    assert router.source_state("standby") == "ready"


def test_a_source_whose_last_contact_failed_is_down():
    import time as _time

    source = FakeSource("a")
    router = make_router({"a": source})
    source.stats.last_ok_at = _time.time() - 60
    source.stats.last_failure_at = _time.time()
    assert router.source_state("a") == "down"


def test_an_open_breaker_reads_as_down():
    a = FakeSource("a", error=SourceUnavailable("down", source="a"))
    b = FakeSource("b")
    router = make_router({"a": a, "b": b})
    for _ in range(4):
        router.call("history", "getBalance", ["x"])
    assert router.source_state("a") == "down"


def test_a_disabled_source_is_off_not_down():
    """'No credential' and 'broken' are different problems and must not
    look the same."""
    router = make_router({"a": FakeSource("a", enabled=False)})
    assert router.source_state("a") == "off"


def test_metrics_carry_the_state_to_the_ui():
    router = make_router({"a": FakeSource("a")})
    router.call("history", "getBalance", ["x"])
    assert router.metrics()["sources"]["a"]["state"] == "active"


# -- call_accept: escalate a soft miss (null / empty) to a deeper source ----
#
# publicnode keeps only ~2 days of history, so an aged getTransaction reads
# null and an aged getSignaturesForAddress reads empty THERE while a deeper
# source still serves it. `null`/`[]` are not errors, so ordinary routing
# returns the shallow answer verbatim. call_accept treats an unsatisfying
# result as a soft miss and tries the next source, without failing over on
# every legitimately-quiet wallet.

def test_call_accept_escalates_a_null_to_the_next_source():
    a = FakeSource("a", results=[None])
    b = FakeSource("b", results=[{"tx": 1}])
    router = make_router({"a": a, "b": b})
    result = router.call_accept("history", "getTransaction", ["sig"],
                                accept=lambda r: r is not None)
    assert result == {"tx": 1}
    assert a.calls and b.calls              # a answered null, b served it


def test_call_accept_returns_the_soft_miss_when_no_source_does_better():
    """All sources return null: the honest null is returned, NOT an
    exhaustion error — the tracker reads that as 'not yet, retry'."""
    a = FakeSource("a", results=[None])
    b = FakeSource("b", results=[None])
    router = make_router({"a": a, "b": b})
    assert router.call_accept("history", "getTransaction", ["sig"],
                              accept=lambda r: r is not None) is None


def test_call_accept_keeps_a_soft_missing_source_healthy():
    """A soft miss is a real answer, so the source stays closed and
    counts as routed — it just could not serve THIS request fully."""
    a = FakeSource("a", results=[None])
    b = FakeSource("b", results=[{"tx": 1}])
    router = make_router({"a": a, "b": b})
    router.call_accept("history", "getTransaction", ["sig"],
                       accept=lambda r: r is not None)
    assert router.source_state("a") in ("active", "ready")
    assert router.metrics()["failovers"] == 0    # a soft miss is not a failover


def test_call_accept_stops_at_the_first_accepted_result():
    a = FakeSource("a", results=[{"tx": 1}])
    b = FakeSource("b", results=[{"tx": 2}])
    router = make_router({"a": a, "b": b})
    assert router.call_accept("history", "getTransaction", ["sig"],
                              accept=lambda r: r is not None) == {"tx": 1}
    assert b.calls == []


def test_call_accept_escalates_an_empty_signature_page():
    a = FakeSource("a", results=[[]])
    b = FakeSource("b", results=[[{"signature": "s", "slot": 9}]])
    router = make_router({"a": a, "b": b})
    result = router.call_accept("history", "getSignaturesForAddress", ["w"],
                                accept=lambda r: bool(r))
    assert result == [{"signature": "s", "slot": 9}]
