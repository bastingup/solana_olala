"""JsonRpcSource: error classification, batching, budget, support map.

The most important test in this file is
``test_batch_results_are_matched_by_id_not_position``. mainnet-beta was
MEASURED returning a 50-element batch out of order; zipping responses to
requests by index would attribute one wallet's transactions to another,
which for a copy trader means buying what somebody else bought.
"""

import pytest
import requests

from olala.chain.errors import (SourceDataError, SourceIncomplete,
                                SourceRateLimited, SourceRejected,
                                SourceUnavailable, SourceUnsupported,
                                classify_rpc_error)
from olala.chain.sources.base import BatchItem, CircuitBreaker
from olala.chain.sources.json_rpc import JsonRpcSource
from olala.config import SourceConfig


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, bad_json=False):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("no json here")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.posts = []
        self.headers = {}

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_source(responses, **overrides):
    settings = dict(endpoints=["https://node.test"], supports_batch=True,
                    max_batch=10, max_wallet_calls_per_sec=1000.0)
    settings.update(overrides)
    source = JsonRpcSource("stub", SourceConfig(**settings))
    source._session = FakeSession(responses)
    return source, source._session


def ok(result, id_=None):
    body = {"jsonrpc": "2.0", "result": result}
    if id_ is not None:
        body["id"] = id_
    return body


# -- single calls ----------------------------------------------------------

def test_call_returns_result_and_posts_to_the_endpoint():
    source, session = make_source([FakeResponse(body=ok([1, 2]))])
    assert source.call("getSignaturesForAddress", ["addr"]) == [1, 2]
    payload = session.posts[0]["json"]
    assert payload["method"] == "getSignaturesForAddress"
    assert payload["params"] == ["addr"]


def test_api_key_is_attached_to_the_endpoint():
    source, session = make_source([FakeResponse(body=ok(1))],
                                  endpoints=["https://node.test/"],
                                  api_key="KEY123")
    source.call("getBalance", ["a"])
    assert session.posts[0]["url"] == "https://node.test/?api-key=KEY123"


def test_transport_failure_becomes_source_unavailable():
    source, _ = make_source([requests.ConnectionError("refused")])
    with pytest.raises(SourceUnavailable):
        source.call("getBalance", ["a"])
    assert source.stats.failures == 1


def test_http_429_penalises_the_bucket_and_reports_retry_after():
    source, _ = make_source(
        [FakeResponse(status=429, headers={"Retry-After": "4"})])
    before = source.limiter.current_rate
    with pytest.raises(SourceRateLimited) as excinfo:
        source.call("getBalance", ["a"])
    assert excinfo.value.retry_after == pytest.approx(4.0)
    assert source.limiter.current_rate < before
    assert source.stats.rate_limited == 1


def test_non_json_body_becomes_a_data_error():
    source, _ = make_source([FakeResponse(bad_json=True)])
    with pytest.raises(SourceDataError):
        source.call("getBalance", ["a"])


# -- error classification --------------------------------------------------

@pytest.mark.parametrize("code,expected", [
    (-32601, SourceUnsupported),      # method not found / disabled
    (-32602, SourceRejected),         # invalid params — never fail over
    (-32600, SourceRejected),
    (-32007, SourceIncomplete),       # slot skipped
    (-32004, SourceIncomplete),       # block not available
    (-32019, SourceIncomplete),       # long-term storage query failed
    (-32603, SourceUnavailable),      # internal error
    (-99999, SourceUnavailable),      # unknown -> safe default
])
def test_rpc_error_codes_map_to_the_right_action(code, expected):
    assert isinstance(classify_rpc_error({"code": code, "message": "x"}),
                      expected)


def test_node_behind_and_too_many_requests_share_a_code():
    """-32005 means both; only one of them should slow our bucket."""
    behind = classify_rpc_error({"code": -32005,
                                 "message": "Node is behind by 51 slots"})
    throttled = classify_rpc_error({"code": -32005,
                                    "message": "Too many requests"})
    assert isinstance(behind, SourceUnavailable)
    assert not isinstance(behind, SourceRateLimited)
    assert isinstance(throttled, SourceRateLimited)


def test_rejected_errors_do_not_fail_over():
    assert SourceRejected("bad").failover is False
    assert SourceRateLimited("slow").failover is True
    assert SourceIncomplete("short").failover is True


def test_unsupported_method_is_remembered():
    """Public nodes disable getTokenLargestAccounts; asking again wastes
    a request and a retry cycle every single time."""
    source, session = make_source([
        FakeResponse(body={"error": {"code": -32601,
                                     "message": "Method not found"}})])
    assert source.supports("getTokenLargestAccounts")
    with pytest.raises(SourceUnsupported):
        source.call("getTokenLargestAccounts", ["mint"])
    assert not source.supports("getTokenLargestAccounts")
    assert source.supports("getBalance")          # unrelated method fine


# -- batching --------------------------------------------------------------

def test_batch_results_are_matched_by_id_not_position():
    """MEASURED on mainnet-beta: batch responses arrive out of order."""
    source, _ = make_source([FakeResponse(body=[
        # deliberately shuffled relative to the request order
        ok("third", 1002), ok("first", 1000), ok("second", 1001),
    ])])
    results = source.batch([
        BatchItem("getSignaturesForAddress", ["walletA"]),
        BatchItem("getSignaturesForAddress", ["walletB"]),
        BatchItem("getSignaturesForAddress", ["walletC"]),
    ])
    assert results == ["first", "second", "third"]


def test_one_bad_sub_call_does_not_discard_the_others():
    source, _ = make_source([FakeResponse(body=[
        ok("a", 1000),
        {"jsonrpc": "2.0", "id": 1001,
         "error": {"code": -32602, "message": "Invalid param: WrongPubkey"}},
        ok("c", 1002),
    ])])
    results = source.batch([BatchItem("getSignaturesForAddress", [a])
                            for a in ("a", "bad", "c")])
    assert results[0] == "a"
    assert isinstance(results[1], SourceRejected)
    assert results[2] == "c"


def test_missing_sub_call_response_is_reported_in_place():
    source, _ = make_source([FakeResponse(body=[ok("a", 1000)])])
    results = source.batch([BatchItem("getBalance", ["a"]),
                            BatchItem("getBalance", ["b"])])
    assert results[0] == "a"
    assert isinstance(results[1], SourceDataError)


def test_budget_is_charged_per_sub_call_not_per_request():
    """Public nodes meter by SUB-CALL: a 50-address batch costs 50.

    Treating a batch as one request is exactly what made a '3-second
    poll' look affordable and then throttle after 40 seconds. A source
    limited to 10/s must therefore refuse a 50-cost reservation it
    cannot fund, while granting a 1-cost one.
    """
    source, _ = make_source([], max_wallet_calls_per_sec=10.0)
    assert source.try_reserve(cost=50.0, timeout=0.05) is False
    assert source.try_reserve(cost=1.0, timeout=0.05) is True


def test_whole_batch_error_object_is_classified():
    source, _ = make_source([FakeResponse(
        body={"error": {"code": -32600, "message": "batch too large"}})])
    with pytest.raises(SourceRejected):
        source.batch([BatchItem("getBalance", ["a"])])


def test_non_batching_source_refuses_to_batch():
    source, _ = make_source([], supports_batch=False)
    with pytest.raises(SourceUnsupported):
        source.batch([BatchItem("getBalance", ["a"])])


def test_empty_batch_is_free():
    source, session = make_source([])
    assert source.batch([]) == []
    assert session.posts == []


# -- budget ----------------------------------------------------------------

def test_try_reserve_refuses_rather_than_blocking():
    """Refusing is what lets the router move to the next source instead
    of queueing behind a throttled one."""
    source, _ = make_source([], max_wallet_calls_per_sec=1.0)
    assert source.try_reserve(1.0, timeout=0.1) is True
    assert source.try_reserve(50.0, timeout=0.05) is False


def test_metered_sources_count_their_sub_calls():
    source, _ = make_source(
        [FakeResponse(body=[ok(i, 1000 + i) for i in range(3)])],
        metered=True)
    source.batch([BatchItem("getBalance", [f"w{i}"]) for i in range(3)])
    assert source.stats.metered_units == 3


def test_unmetered_source_counts_nothing_against_a_cap():
    source, _ = make_source([FakeResponse(body=ok(1))], metered=False)
    source.call("getBalance", ["a"])
    assert source.stats.metered_units == 0


# -- circuit breaker -------------------------------------------------------

def test_breaker_opens_after_repeated_failures_and_closes_on_success():
    breaker = CircuitBreaker(threshold=3, base_cooldown_sec=60.0)
    assert breaker.closed
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.closed              # a blip must not open it
    breaker.record_failure()
    assert not breaker.closed
    breaker.record_success()
    assert breaker.closed


def test_breaker_honours_an_explicit_retry_after_immediately():
    breaker = CircuitBreaker(threshold=5)
    breaker.record_failure(retry_after=30.0)
    assert not breaker.closed          # the server told us to wait
    assert breaker.open_for_sec > 20.0
