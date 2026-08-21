"""Regression tests for the findings of the independent MVP audit.

Each test pins one confirmed bug: if it ever fails again, the same
money-losing behavior is back.
"""

import time

import pytest

from olala.domain.models import (ExitReason, TradeSide, TraderProfile,
                                 TraderStatus)
from olala.risk.atr import CANDLE_SECONDS, AtrTracker
from olala.risk.engine import RiskEngine
from olala.services.traders import TraderRegistry
from olala.trading.engine import TradingEngine
from olala.trading.executor import PaperExecutor
from olala.trading.portfolio import PortfolioManager
from olala.trading.tracker import WalletTracker

from conftest import make_token
from fakes import FakeMarketData, FakeProvider, make_swap_tx
from test_trading_engine import ApprovingSafety, signal

TRADER = "TraderAAAA1111111111111111111111111111111111"
MINT = "MintAAAA111111111111111111111111111111111111"


# -- C1: config PUT must not open a live-trading side door ----------------
# (Originally: PUT /api/config could set mode=live, bypassing the arming
# guards. The universe mode is gone; the pin now holds the door shut for
# legacy clients still sending the key.)

def test_config_put_cannot_change_mode(tmp_path, config_store):
    from olala.api.server import AppContext, build_app
    from olala.persistence.database import Database
    from olala.security.keystore import EncryptedKeystore

    ctx = AppContext(
        config_store=config_store,
        database=Database(path=tmp_path / "c1.db"),
        keystore=EncryptedKeystore(path=tmp_path / "c1.enc"),
        provider=FakeProvider(), market_data=FakeMarketData())
    app = build_app(ctx)
    app.testing = True
    client = app.test_client()

    response = client.put("/api/config", json={"mode": "live"})
    assert response.status_code == 400
    assert "mode" not in client.get("/api/state").get_json()


# -- H3: closing a position twice must not mint SOL -----------------------

@pytest.fixture
def engine_world(db, bus, config_store, token):
    portfolio = PortfolioManager(db, bus, config_store, FakeProvider())
    registry = TraderRegistry(db, bus)
    wallet = portfolio.wallets()[0]
    registry.update(TraderProfile(address="t1",
                                  status=TraderStatus.FOLLOWED,
                                  assigned_wallet_id=wallet.id))
    engine = TradingEngine(
        config_store, portfolio, registry,
        FakeMarketData({token.mint: token}), ApprovingSafety(),
        RiskEngine(), bus, paper_executor=PaperExecutor(),
        live_executor=None)
    return engine, portfolio, wallet


def test_double_close_credits_once(engine_world, token):
    engine, portfolio, wallet = engine_world
    engine.handle_signal(signal(TradeSide.BUY))
    position = portfolio.open_positions(wallet.id)[0]
    engine.close_position(position, ExitReason.PANIC_STOP)
    balance_after_first = wallet.base_balance()
    engine.close_position(position, ExitReason.MANUAL)
    engine.close_position(position, ExitReason.TRADER_EXIT)
    assert wallet.base_balance() == pytest.approx(balance_after_first)


def test_begin_close_claims_exclusively(engine_world, token):
    engine, portfolio, wallet = engine_world
    engine.handle_signal(signal(TradeSide.BUY))
    position = portfolio.open_positions(wallet.id)[0]
    assert portfolio.begin_close(position) is True
    assert portfolio.begin_close(position) is False  # already claimed
    portfolio.abort_close(position.id)
    assert portfolio.begin_close(position) is True   # claim released


# -- H1/H2: tracking must neither skip nor replay -------------------------
#
# These pinned the follow daemon. The daemon is gone, replaced by
# WalletTracker, but the CONTRACT is unchanged and must survive the new
# gears, batching and multi-source routing.

class RecordingQueue:
    def __init__(self):
        self.signals = []

    def submit(self, sig):
        self.signals.append(sig)
        return True


class SingleSourceRouter:
    """One batch-capable source that fans out to the fake provider."""

    def __init__(self, provider):
        self._provider = provider

    def batch_capable(self, policy):
        return "publicnode"

    def batch(self, policy, items, timeout=None):
        return [self._provider.get_signatures(
            item.params[0], limit=item.params[1].get("limit", 30))
            for item in items]


@pytest.fixture
def follow_world(db, bus, config_store):
    config_store.update({"tracking": {"max_transactions_per_cycle": 10}})
    provider = FakeProvider()
    provider.router = SingleSourceRouter(provider)
    registry = TraderRegistry(db, bus)
    registry.update(TraderProfile(address=TRADER,
                                  status=TraderStatus.FOLLOWED,
                                  assigned_wallet_id="w1"))
    queue = RecordingQueue()
    tracker = WalletTracker(config_store, provider, registry, db, queue)
    return provider, registry, queue, tracker


def entry(name, slot=0):
    return {"signature": name, "slot": slot, "err": None,
            "blockTime": time.time()}


def swap_for(provider, name, buy=True):
    provider.transactions[name] = make_swap_tx(
        TRADER, -2_000_005_000 if buy else 1_000_000_000,
        MINT, 400.0 if buy else -200.0)
    provider.transactions[name]["blockTime"] = time.time()


def sweep(tracker):
    """Run the next scheduled sweep now instead of waiting out the
    derived interval."""
    tracker._next_batch_at = 0.0
    tracker.tick()


def test_burst_larger_than_budget_carries_over(follow_world):
    provider, registry, queue, tracker = follow_world
    provider.signatures[TRADER] = [entry("s00", 100)]
    tracker.tick()  # arm the watermark

    names = [f"s{i:02d}" for i in range(14, 0, -1)]  # s14 newest … s01
    provider.signatures[TRADER] = ([entry(n, 100 + int(n[1:])) for n in names]
                                   + [entry("s00", 100)])
    for n in names:
        swap_for(provider, n)
    sweep(tracker)
    # Budget is 10: the OLDEST ten processed first, none skipped.
    assert [s.observed.signature for s in queue.signals] == [
        f"s{i:02d}" for i in range(1, 11)]
    sweep(tracker)
    assert [s.observed.signature for s in queue.signals] == [
        f"s{i:02d}" for i in range(1, 15)]


def test_rpc_failure_mid_poll_never_duplicates(follow_world):
    provider, registry, queue, tracker = follow_world
    provider.signatures[TRADER] = [entry("s0", 100)]
    tracker.tick()

    provider.signatures[TRADER] = [entry("s2", 300), entry("s1", 200),
                                   entry("s0", 100)]
    swap_for(provider, "s1")
    swap_for(provider, "s2")
    provider.fail_transactions.add("s2")
    sweep(tracker)  # s1 executes, s2 fetch fails
    assert [s.observed.signature for s in queue.signals] == ["s1"]

    provider.fail_transactions.clear()
    sweep(tracker)  # resumes at s2 — and must NOT replay s1
    assert [s.observed.signature for s in queue.signals] == ["s1", "s2"]


# -- H4: discovery cursor must not jump past unfetched transactions -------

def test_scanner_budget_cut_does_not_skip_history(db, bus, config_store):
    from olala.discovery.scanner import RpcBudget, TraderDiscoveryDaemon

    provider = FakeProvider()
    registry = TraderRegistry(db, bus)
    registry.add_candidate(TRADER)
    daemon = TraderDiscoveryDaemon(
        config_store, provider, FakeMarketData(), registry, db, bus,
        assign_wallet=lambda *_a: "w1")

    names = [f"h{i}" for i in range(10)]  # newest-first history
    provider.signatures[TRADER] = [entry(n) for n in names]
    for n in names:
        swap_for(provider, n)

    # Budget of 4: one signature call + three tx fetches.
    daemon._advance_candidate(config_store.config, TRADER, RpcBudget(4))
    cursor, complete = registry.history_cursor(TRADER)
    assert cursor == "h2"  # advanced only over fetched entries
    assert not complete
    assert len(db.load_observed_trades(TRADER)) == 3

    # Next tick resumes exactly at h3 — nothing lost.
    daemon._advance_candidate(config_store.config, TRADER, RpcBudget(50))
    assert len(db.load_observed_trades(TRADER)) == 10


# -- M1: ATR must reflect completed candle ranges -------------------------

def test_atr_uses_full_candle_range():
    tracker = AtrTracker(period=14)
    for i in range(20):
        t = i * CANDLE_SECONDS
        tracker.add_sample("m1", t, 1.0)
        tracker.add_sample("m1", t + 30, 1.2)
    atr = tracker.atr("m1")
    assert atr is not None
    # True range of every completed candle is 0.2; the old stub-based
    # computation reported roughly half of that.
    assert atr == pytest.approx(0.2, rel=0.15)


# -- M2: the 1% liquidity cap is fleet-wide, not per wallet ---------------

def test_liquidity_cap_counts_all_wallets(db, bus, config_store):
    from olala.risk.engine import WalletExposure

    token = make_token(liquidity_usd=10_000.0, price_usd=1.0,
                       price_sol=0.005)  # 1% of pool ≈ 0.5 SOL
    exposure = WalletExposure(
        wallet_id="w1", cash_sol=100.0, equity_sol=100.0,
        open_positions=0, invested_in_mint_sol=0.0,
        # Other wallets have all but filled the pool's 1% allowance.
        fleet_invested_in_mint_sol=0.4999)
    verdict = RiskEngine().evaluate_entry(
        config_store.config, token, exposure, is_resize=False)
    assert not verdict.approved
    assert "liquidity" in verdict.reason


def test_liquidity_cap_sizes_down_before_it_refuses(db, bus, config_store):
    """The fleet's share of a pool clamps the order rather than killing
    it — a small remaining allowance is a small trade."""
    from olala.risk.engine import WalletExposure

    token = make_token(liquidity_usd=10_000.0, price_usd=1.0,
                       price_sol=0.005)  # 1% of pool ~ 0.5 SOL
    exposure = WalletExposure(
        wallet_id="w1", cash_sol=100.0, equity_sol=100.0,
        open_positions=0, invested_in_mint_sol=0.0,
        fleet_invested_in_mint_sol=0.45)
    verdict = RiskEngine().evaluate_entry(
        config_store.config, token, exposure, is_resize=False)
    assert verdict.approved
    assert verdict.size_sol == pytest.approx(0.05, abs=1e-6)


def test_portfolio_exposure_reports_fleet_total(db, bus, config_store,
                                                token):
    from olala.domain.models import Fill

    portfolio = PortfolioManager(db, bus, config_store, FakeProvider())
    w1, w2 = portfolio.wallets()[:2]
    fill = Fill(order_id="o1", side=TradeSide.BUY, mint=token.mint,
                quantity=100, price_sol=0.01, sol_amount=1.0, fee_sol=0)
    portfolio.apply_buy(w1, "t1", token, fill)
    fill2 = Fill(order_id="o2", side=TradeSide.BUY, mint=token.mint,
                 quantity=100, price_sol=0.01, sol_amount=1.0, fee_sol=0)
    portfolio.apply_buy(w2, "t2", token, fill2)

    exposure = portfolio.exposure(w1.id, token.mint)
    assert exposure.invested_in_mint_sol == pytest.approx(1.0)
    assert exposure.fleet_invested_in_mint_sol == pytest.approx(2.0)


# -- C2 tripwire: the escaping helper and its wiring must survive ---------

def test_frontend_escaping_wired():
    from pathlib import Path
    frontend = Path(__file__).resolve().parent.parent / "frontend"
    format_js = (frontend / "js/format.js").read_text()
    assert "&quot;" in format_js  # attribute-safe escaping
    state_js = (frontend / "js/state.js").read_text()
    assert "escapeHtml" in state_js.splitlines()[6] or \
        "escapeHtml" in state_js  # store imports the escaper
    # Raw interpolation of chain-sourced fields into feed HTML is banned:
    for banned in ("${data.symbol}", "${data.reason}", "${data.error}",
                   "${data.label}", "${fill.mint"):
        assert banned not in state_js, f"unescaped interpolation: {banned}"

# -- Infinity in trader stats killed the frontend stream ------------------
# A trader with no observed trades has inactive_hours == inf. Python's
# json module serializes that as the token `Infinity`, which Python
# accepts back but every browser rejects — so one such trader in the
# snapshot made the ENTIRE snapshot unparseable and the frontend sat on
# "LINKING" forever. Strict JSON must hold at every boundary.

def _strict_json(text: str):
    """Parse like a browser: Infinity/NaN tokens are a failure."""
    import json

    def refuse(token):
        raise AssertionError(f"non-standard JSON token emitted: {token}")
    return json.loads(text, parse_constant=refuse)


def test_stats_with_no_trades_serialize_to_strict_json():
    import json

    from olala.domain.models import TraderStats

    stats = TraderStats(address=TRADER)  # no trades: inactive_hours == inf
    assert stats.inactive_hours == float("inf")  # filters still see inf
    payload = stats.to_dict()
    assert payload["inactive_hours"] is None
    _strict_json(json.dumps(payload, allow_nan=False))


def test_stream_frame_nulls_non_finite_floats():
    from olala.api.stream import _frame

    frame = _frame({"type": "x", "data": {
        "a": float("inf"), "b": float("nan"),
        "nested": [{"c": float("-inf")}]}})
    parsed = _strict_json(frame)
    assert parsed["data"]["a"] is None
    assert parsed["data"]["b"] is None
    assert parsed["data"]["nested"][0]["c"] is None


def test_rest_state_with_tradeless_trader_is_strict_json(tmp_path,
                                                         config_store):
    from olala.api.server import AppContext, build_app
    from olala.api.stream import _frame
    from olala.domain.models import TraderStats
    from olala.persistence.database import Database
    from olala.security.keystore import EncryptedKeystore

    ctx = AppContext(
        config_store=config_store,
        database=Database(path=tmp_path / "inf.db"),
        keystore=EncryptedKeystore(path=tmp_path / "inf.enc"),
        provider=FakeProvider(), market_data=FakeMarketData())
    profile = TraderProfile(address=TRADER,
                            status=TraderStatus.REJECTED,
                            stats=TraderStats(address=TRADER),
                            rejection_reason="no trades observed in window")
    ctx.registry.update(profile)
    app = build_app(ctx)
    app.testing = True
    client = app.test_client()

    for path in ("/api/state", "/api/traders"):
        body = client.get(path).get_data(as_text=True)
        assert "Infinity" not in body
        _strict_json(body)

    snapshot_frame = _frame({"type": "snapshot", "data": ctx.snapshot()})
    _strict_json(snapshot_frame)
