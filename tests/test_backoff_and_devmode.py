"""Adaptive rate-limit backoff, log redaction, and the dev-mode lock."""

import time

import pytest

from olala.chain.provider import redact
from olala.chain.rate_limiter import (DEFAULT_COOLDOWN_SEC,
                                      MIN_RATE_FRACTION, RateLimiter)
from olala.config import ConfigStore


# -- backoff ---------------------------------------------------------------

def test_penalty_halves_rate_and_pauses():
    limiter = RateLimiter(requests_per_second=8.0, burst=4)
    assert limiter.current_rate == 8.0
    cooldown = limiter.penalize()
    assert limiter.current_rate == 4.0
    assert cooldown == DEFAULT_COOLDOWN_SEC
    assert limiter.throttled


def test_repeated_penalties_compound_the_cooldown():
    limiter = RateLimiter(requests_per_second=8.0)
    first = limiter.penalize()
    second = limiter.penalize()
    assert second > first  # an episode escalates, not a flat retry
    assert limiter.current_rate == 2.0  # halved twice


def test_rate_never_falls_below_floor():
    limiter = RateLimiter(requests_per_second=8.0)
    for _ in range(30):
        limiter.penalize()
    assert limiter.current_rate == pytest.approx(8.0 * MIN_RATE_FRACTION)


def test_retry_after_header_is_honored():
    limiter = RateLimiter(requests_per_second=8.0)
    assert limiter.penalize(retry_after_sec=7.5) == 7.5


def test_cooldown_actually_blocks_issuing():
    limiter = RateLimiter(requests_per_second=100.0, burst=10)
    limiter.penalize(retry_after_sec=0.5)
    started = time.monotonic()
    limiter.acquire()
    assert time.monotonic() - started >= 0.4


def test_rate_recovers_toward_ceiling():
    limiter = RateLimiter(requests_per_second=8.0)
    limiter.penalize(retry_after_sec=0.5)
    assert limiter.current_rate == 4.0
    # Simulate a clean stretch: rewind the recovery clock.
    limiter._blocked_until = 0.0
    limiter._last_recovery = time.monotonic() - 100.0
    limiter.acquire()
    assert limiter.current_rate > 4.0


# -- log redaction ---------------------------------------------------------

def test_api_keys_never_reach_logs():
    leaked = "https://mainnet.helius-rpc.com/?api-key=bedbd6a4-secret-key"
    assert "bedbd6a4" not in redact(leaked)
    assert "helius-rpc.com" in redact(leaked)
    # Keyless endpoints stay readable.
    assert redact("https://api.mainnet-beta.solana.com") == \
        "https://api.mainnet-beta.solana.com"


# -- dev mode --------------------------------------------------------------

def test_dev_mode_blocks_live_at_the_config_layer(tmp_path):
    path = tmp_path / "dev.yaml"
    path.write_text("mode: paper\ndev_mode: true\n")
    store = ConfigStore(path=path)
    assert store.config.dev_mode is True
    with pytest.raises(ValueError, match="dev_mode"):
        store.update({"mode": "live"})
    assert store.config.mode == "paper"


def test_dev_mode_blocks_live_at_the_api(tmp_path):
    from solders.keypair import Keypair

    from olala.api.server import AppContext, build_app
    from olala.persistence.database import Database
    from olala.security.keystore import EncryptedKeystore
    from fakes import FakeMarketData, FakeProvider

    path = tmp_path / "dev.yaml"
    path.write_text("mode: paper\ndev_mode: true\n")
    ctx = AppContext(
        config_store=ConfigStore(path=path),
        database=Database(path=tmp_path / "dev.db"),
        keystore=EncryptedKeystore(path=tmp_path / "dev.enc"),
        provider=FakeProvider(), market_data=FakeMarketData())
    app = build_app(ctx)
    app.testing = True
    client = app.test_client()

    assert client.get("/api/state").get_json()["dev_mode"] is True
    # Even fully set up — keystore unlocked, live wallet present — dev
    # mode refuses to arm.
    client.post("/api/keystore/unlock", json={"passphrase": "pw"})
    client.post("/api/wallets", json={"label": "V", "secret": str(Keypair())})
    response = client.post("/api/mode", json={"mode": "live"})
    assert response.status_code == 400
    assert "dev mode" in response.get_json()["error"]
    assert client.get("/api/state").get_json()["mode"] == "paper"


def test_dev_mode_bypasses_pre_screen(tmp_path, db, bus):
    """Dev mode welcomes bots: the pre-screen never rejects."""
    path = tmp_path / "dev.yaml"
    path.write_text("mode: paper\ndev_mode: true\n")
    store = ConfigStore(path=path)
    from olala.discovery.scanner import RpcBudget
    from test_discovery_v2 import BOT, bot_signatures, make_daemon
    provider, registry, daemon = make_daemon(db, bus, store)
    provider.signatures[BOT] = bot_signatures()
    assert daemon._pre_screen(store.config, BOT, RpcBudget(5)) is True


def test_dev_mode_admits_anyone_with_trades(tmp_path, db, bus):
    """Dev mode bypasses every admission gate: one observed swap in the
    window is enough to be followed."""
    import time
    path = tmp_path / "dev.yaml"
    path.write_text("mode: paper\ndev_mode: true\n")
    store = ConfigStore(path=path)
    from olala.discovery.scanner import RpcBudget
    from olala.domain.models import ObservedTrade, TradeSide, TraderStatus
    from test_discovery_v2 import ELITE, make_daemon
    provider, registry, daemon = make_daemon(db, bus, store)
    registry.add_candidate(ELITE)
    # A single tiny recent swap — fails every real filter.
    db.insert_observed_trades([ObservedTrade(
        trader=ELITE, signature="s1", side=TradeSide.BUY, mint="m1",
        token_amount=10, sol_amount=0.2, price_sol=0.02,
        block_time=time.time() - 60)])
    daemon._scanned_counts[ELITE] = 1
    daemon._finalize_candidate(store.config, ELITE, RpcBudget(5))
    assert registry.get(ELITE).status is TraderStatus.FOLLOWED


def test_dev_mode_skips_token_safety(tmp_path, db, bus, token):
    """A signal on a token the safety screen would refuse still executes
    a paper fill in dev mode."""
    path = tmp_path / "dev.yaml"
    path.write_text("mode: paper\ndev_mode: true\n")
    store = ConfigStore(path=path)
    from olala.domain.models import TradeSide, TraderProfile, TraderStatus
    from olala.risk.engine import RiskEngine
    from olala.services.traders import TraderRegistry
    from olala.trading.engine import TradingEngine
    from olala.trading.executor import PaperExecutor
    from olala.trading.portfolio import PortfolioManager
    from fakes import FakeMarketData, FakeProvider
    from test_trading_engine import RefusingSafety, signal
    portfolio = PortfolioManager(db, bus, store, FakeProvider())
    registry = TraderRegistry(db, bus)
    wallet = portfolio.wallets()[0]
    registry.update(TraderProfile(address="t1",
                                  status=TraderStatus.FOLLOWED,
                                  assigned_wallet_id=wallet.id))
    engine = TradingEngine(
        store, portfolio, registry, FakeMarketData({token.mint: token}),
        RefusingSafety(), RiskEngine(), bus,
        paper_executor=PaperExecutor(), live_executor=None)
    engine.handle_signal(signal(TradeSide.BUY))
    assert len(portfolio.open_positions(wallet.id)) == 1
