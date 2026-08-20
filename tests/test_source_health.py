"""SourceHealthDaemon: keeping standby endpoints' status truthful.

Routing only contacts the source it routes to, so a spare accumulates no
evidence at all — leaving "we have not needed it" indistinguishable from
"it is dead", which is precisely the failure the fall-through ordering
exists to survive.
"""

import time

from olala.chain.health import METERED_PROBE_MULTIPLIER, SourceHealthDaemon
from olala.chain.errors import SourceUnavailable
from olala.chain.sources.base import SourceStats


class FakeSource:
    def __init__(self, name, *, enabled=True, metered=False, fails=False,
                 budget=True):
        self.name = name
        self.enabled = enabled
        self.metered = metered
        self.stats = SourceStats()
        self.calls = []
        self._fails = fails
        self._budget = budget

    def try_reserve(self, cost=1.0, timeout=None):
        return self._budget

    def call(self, method, params):
        self.calls.append(method)
        if self._fails:
            self.stats.last_failure_at = time.time()
            raise SourceUnavailable("node says no", source=self.name)
        self.stats.last_ok_at = time.time()
        return "ok"


class FakeRouter:
    def __init__(self, sources):
        self.sources = sources


def daemon_for(sources, interval=30.0):
    return SourceHealthDaemon(FakeRouter(sources), interval_sec=interval)


def test_an_uncontacted_source_is_probed():
    source = FakeSource("standby")
    daemon_for({"standby": source}).tick()
    assert source.calls == ["getHealth"]


def test_a_recently_used_source_is_not_probed():
    """Its success is already proof; re-proving it is pure waste."""
    source = FakeSource("busy")
    source.stats.last_ok_at = time.time()
    daemon_for({"busy": source}).tick()
    assert source.calls == []


def test_a_disabled_source_is_never_probed():
    source = FakeSource("off", enabled=False)
    daemon_for({"off": source}).tick()
    assert source.calls == []


def test_a_healthy_metered_source_is_probed_far_less_often():
    """A credit spent proving Helius is alive is a credit not spent
    fetching a trade."""
    source = FakeSource("helius", metered=True)
    source.stats.last_ok_at = time.time() - 60      # past the base interval
    daemon_for({"helius": source}, interval=30.0).tick()
    assert source.calls == []

    source.stats.last_ok_at = time.time() - (30.0 * METERED_PROBE_MULTIPLIER) - 1
    daemon_for({"helius": source}, interval=30.0).tick()
    assert source.calls == ["getHealth"]


def test_a_FAILING_metered_source_is_rechecked_on_the_short_interval():
    """Found live: a transient 'request deprioritized' from Helius left a
    perfectly healthy endpoint showing as DOWN for five minutes, because
    failures were scheduled as leniently as successes."""
    source = FakeSource("helius", metered=True)
    source.stats.last_failure_at = time.time() - 60   # only 60s ago
    assert source.stats.responding is False

    daemon_for({"helius": source}, interval=30.0).tick()

    assert source.calls == ["getHealth"]


def test_a_failing_probe_does_not_raise():
    source = FakeSource("broken", fails=True)
    daemon_for({"broken": source}).tick()          # must not propagate
    assert source.calls == ["getHealth"]


def test_no_budget_means_no_probe_and_no_verdict():
    """Refusing a courtesy call is not evidence of anything."""
    source = FakeSource("busy", budget=False)
    daemon_for({"busy": source}).tick()
    assert source.calls == []
    assert source.stats.last_contact_at == 0
