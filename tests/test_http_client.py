"""The shared HTTP client: status mapping, throttle feedback, timeouts.

The behaviour that matters most here is that a 429 reaches the caller's
OWN rate limiter. Before consolidation, solana_tracker recognised HTTP
429 and never penalised its bucket, so the limiter kept issuing at full
rate into a service that was refusing us.
"""

import email.utils
import time

import pytest
import requests

from olala.chain.http import (HttpClient, HttpError, parse_retry_after)
from olala.chain.rate_limiter import RateLimiter


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, bad_json=False):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1")
        return self._body


class FakeSession:
    """Stands in for requests.Session inside HttpClient."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params,
                           "json": json, "timeout": timeout})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(responses, *, rps=100.0, error_cls=HttpError):
    client = HttpClient("https://example.test", name="stub",
                        error_cls=error_cls,
                        limiter=RateLimiter(requests_per_second=rps, burst=50))
    client._session = FakeSession(responses)
    return client, client._session


def test_get_returns_parsed_json_and_builds_url():
    client, session = make_client([FakeResponse(body={"ok": 1})])
    assert client.get("/v2/thing", params={"a": "b"}) == {"ok": 1}
    call = session.calls[0]
    assert call["url"] == "https://example.test/v2/thing"
    assert call["params"] == {"a": "b"}
    assert call["method"] == "GET"


def test_absolute_path_is_not_prefixed():
    client, session = make_client([FakeResponse(body={})])
    client.get("https://other.test/x")
    assert session.calls[0]["url"] == "https://other.test/x"


def test_429_penalises_the_limiter_and_reports_status():
    """The regression this consolidation exists to prevent."""
    client, _ = make_client([FakeResponse(status=429)])
    limiter = client.limiter
    before = limiter.current_rate

    with pytest.raises(HttpError) as excinfo:
        client.get("/x")

    assert excinfo.value.status == 429
    assert excinfo.value.rate_limited
    assert limiter.current_rate < before          # rate was actually halved
    assert limiter.throttled


def test_retry_after_seconds_is_honoured():
    client, _ = make_client(
        [FakeResponse(status=429, headers={"Retry-After": "7"})])
    with pytest.raises(HttpError) as excinfo:
        client.get("/x")
    assert excinfo.value.retry_after == pytest.approx(7.0)


def test_unauthorized_is_distinguishable_from_throttling():
    client, _ = make_client([FakeResponse(status=403)])
    with pytest.raises(HttpError) as excinfo:
        client.get("/x")
    assert excinfo.value.unauthorized
    assert not excinfo.value.rate_limited
    # An entitlement problem must NOT slow the bucket down: retrying
    # later cannot fix it, and throttling ourselves hides the real cause.
    assert not client.limiter.throttled


def test_server_error_raises_with_status():
    client, _ = make_client([FakeResponse(status=503)])
    with pytest.raises(HttpError) as excinfo:
        client.get("/x")
    assert excinfo.value.status == 503


def test_transport_failure_is_wrapped():
    client, _ = make_client([requests.ConnectionError("no route to host")])
    with pytest.raises(HttpError) as excinfo:
        client.get("/x")
    assert excinfo.value.status is None
    assert "no route to host" in str(excinfo.value)


def test_non_json_body_is_wrapped():
    client, _ = make_client([FakeResponse(bad_json=True)])
    with pytest.raises(HttpError) as excinfo:
        client.get("/x")
    assert "non-JSON" in str(excinfo.value)


def test_error_class_is_the_callers_own_type():
    class MyError(HttpError):
        pass

    client, _ = make_client([FakeResponse(status=500)], error_cls=MyError)
    with pytest.raises(MyError):
        client.get("/x")


def test_post_sends_json_body_and_custom_timeout():
    client, session = make_client([FakeResponse(body={"r": True})])
    client.post("/swap", json={"a": 1}, timeout=20)
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["json"] == {"a": 1}
    assert call["timeout"] == 20


# -- Retry-After parsing ---------------------------------------------------

def test_parse_retry_after_handles_both_legal_forms():
    assert parse_retry_after(None) is None
    assert parse_retry_after("12") == pytest.approx(12.0)
    assert parse_retry_after("-5") == 0.0        # never negative

    future = email.utils.formatdate(time.time() + 30, usegmt=True)
    seconds = parse_retry_after(future)
    assert seconds is not None and 20 <= seconds <= 40

    past = email.utils.formatdate(time.time() - 30, usegmt=True)
    assert parse_retry_after(past) == 0.0


def test_parse_retry_after_ignores_garbage():
    assert parse_retry_after("soon-ish") is None
