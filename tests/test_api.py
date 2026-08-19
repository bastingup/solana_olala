import pytest
from solders.keypair import Keypair

from olala.api.server import AppContext, build_app
from olala.persistence.database import Database
from olala.security.keystore import EncryptedKeystore

from conftest import make_token
from fakes import FakeMarketData, FakeProvider


@pytest.fixture
def ctx(tmp_path, config_store):
    return AppContext(
        config_store=config_store,
        database=Database(path=tmp_path / "api.db"),
        keystore=EncryptedKeystore(path=tmp_path / "keystore.enc"),
        provider=FakeProvider(),
        market_data=FakeMarketData({"MintA111": make_token()}))


@pytest.fixture
def client(ctx):
    app = build_app(ctx)
    app.testing = True
    return app.test_client()


def test_state_snapshot_shape(client):
    data = client.get("/api/state").get_json()
    for key in ("wallets", "positions", "traders", "config",
                "keystore", "fills", "sol_price_usd", "dev_mode"):
        assert key in data
    assert len(data["wallets"]) == 3


def test_config_does_not_leak_helius_key(client):
    config = client.get("/api/config").get_json()
    assert "helius_api_key" not in config["chain"]
    assert "helius_enabled" in config["chain"]


def test_add_paper_wallet(client):
    response = client.post("/api/wallets", json={
        "paper": True, "label": "Scout Four", "starting_sol": 5})
    assert response.status_code == 201
    body = response.get_json()
    assert body["label"] == "Scout Four"
    assert body["equity_sol"] == 5.0


def test_add_live_wallet_requires_unlocked_keystore(client):
    response = client.post("/api/wallets", json={
        "label": "Vault", "secret": str(Keypair())})
    assert response.status_code == 400

    assert client.post("/api/keystore/unlock",
                       json={"passphrase": "pw"}).status_code == 200
    response = client.post("/api/wallets", json={
        "label": "Vault", "secret": str(Keypair())})
    assert response.status_code == 201
    assert response.get_json()["is_paper"] is False


def test_arming_guarded_by_keystore(client):
    # Register a live wallet, then lock the keystore behind it: arming
    # must demand an unlocked keystore holding the wallet's key, while
    # disarming is always allowed.
    client.post("/api/keystore/unlock", json={"passphrase": "pw"})
    wallet = client.post("/api/wallets", json={
        "label": "V", "secret": str(Keypair())}).get_json()

    armed = client.post(f"/api/wallets/{wallet['id']}/arm",
                        json={"armed": True})
    assert armed.status_code == 200
    assert armed.get_json()["armed"] is True

    disarmed = client.post(f"/api/wallets/{wallet['id']}/arm",
                           json={"armed": False})
    assert disarmed.status_code == 200
    assert disarmed.get_json()["armed"] is False


def test_legacy_mode_requests_rejected(client):
    # The universe mode is gone: legacy clients get a clear 400, and no
    # config write can smuggle it back in.
    assert client.put("/api/config",
                      json={"mode": "live"}).status_code == 400
    # The route is gone; only the GET-only static handler pattern-matches
    # the path, so legacy POSTs bounce with 405.
    assert client.post("/api/mode",
                       json={"mode": "live"}).status_code in (404, 405)


def test_config_update_roundtrip_and_validation(client):
    response = client.put("/api/config", json={
        "filters_onchain": {"min_win_rate": 0.7}})
    assert response.status_code == 200
    assert response.get_json()["filters_onchain"]["min_win_rate"] == 0.7
    assert client.put("/api/config", json={
        "server": {"port": 1}}).status_code == 400


def test_close_unknown_position_404(client):
    assert client.post("/api/positions/nope/close").status_code == 404


def test_unfollow_unknown_trader_404(client):
    assert client.post("/api/traders/nope/unfollow").status_code == 404


def test_index_serves_frontend(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"SOLANA" in response.data


def test_arm_endpoint_guards(client):
    wallets = client.get("/api/wallets").get_json()
    paper_id = wallets[0]["id"]
    # Paper wallets have no arm state.
    assert client.post(f"/api/wallets/{paper_id}/arm",
                       json={"armed": True}).status_code == 400
    assert client.post("/api/wallets/nope/arm",
                       json={"armed": True}).status_code == 404

    client.post("/api/keystore/unlock", json={"passphrase": "pw"})
    live = client.post("/api/wallets", json={
        "label": "Vault", "secret": str(Keypair())}).get_json()
    assert live["armed"] is False

    armed = client.post(f"/api/wallets/{live['id']}/arm",
                        json={"armed": True})
    assert armed.status_code == 200
    assert armed.get_json()["armed"] is True

    # Disarming works and never needs the keystore.
    disarmed = client.post(f"/api/wallets/{live['id']}/arm",
                           json={"armed": False})
    assert disarmed.get_json()["armed"] is False


def test_snapshot_carries_discovery_status(client):
    assert "discovery" in client.get("/api/state").get_json()


def test_first_live_wallet_flow_from_empty_keystore(client):
    """The first-run path: no keystore file exists yet, so unlocking IS
    creating it, and a live wallet must be registerable immediately after.
    (Regression: the UI hid the unlock panel until a keystore existed,
    making the first live wallet impossible to add.)"""
    state = client.get("/api/state").get_json()
    assert state["keystore"] == {"exists": False, "locked": True}

    # Adding a live wallet while locked fails with a clear reason.
    blocked = client.post("/api/wallets", json={
        "label": "Vault", "secret": str(Keypair())})
    assert blocked.status_code == 400
    assert "locked" in blocked.get_json()["error"]

    # Creating the keystore with a fresh passphrase, then registering.
    assert client.post("/api/keystore/unlock",
                       json={"passphrase": "first-run-passphrase"}
                       ).status_code == 200
    created = client.get("/api/state").get_json()["keystore"]
    assert created == {"exists": True, "locked": False}

    wallet = client.post("/api/wallets", json={
        "label": "Vault", "secret": str(Keypair())})
    assert wallet.status_code == 201
    assert wallet.get_json()["is_paper"] is False
    assert wallet.get_json()["armed"] is False


def test_assign_trader_to_wallet(ctx, client):
    from olala.domain.models import TraderProfile, TraderStatus

    wallets = client.get("/api/wallets").get_json()
    w1, w2 = wallets[0]["id"], wallets[1]["id"]
    ctx.registry.update(TraderProfile(
        address="TraderX", status=TraderStatus.FOLLOWED,
        assigned_wallet_id=w1))

    # Guards: unknown trader / unknown wallet / not-followed trader.
    assert client.post("/api/traders/nope/assign",
                       json={"wallet_id": w2}).status_code == 404
    assert client.post("/api/traders/TraderX/assign",
                       json={"wallet_id": "nope"}).status_code == 404
    ctx.registry.update(TraderProfile(address="CandidateY"))
    assert client.post("/api/traders/CandidateY/assign",
                       json={"wallet_id": w2}).status_code == 400

    # The real move: drag TraderX from wallet 1 to wallet 2.
    response = client.post("/api/traders/TraderX/assign",
                           json={"wallet_id": w2})
    assert response.status_code == 200
    assert response.get_json()["assigned_wallet_id"] == w2

    # Persisted: a fresh registry from the same DB sees the new wallet.
    from olala.services.traders import TraderRegistry
    reloaded = TraderRegistry(ctx.db, ctx.bus)
    assert reloaded.get("TraderX").assigned_wallet_id == w2


def open_copied_position(ctx, wallet, trader="TraderX", mint="MintA111"):
    from olala.domain.models import Fill, TradeSide

    fill = Fill(order_id=f"o-{trader}-{mint}", side=TradeSide.BUY,
                mint=mint, quantity=100, price_sol=0.005, sol_amount=0.5,
                fee_sol=0)
    return ctx.portfolio.apply_buy(
        wallet, trader, ctx.market_data.get_token_info(mint), fill)


def test_reassign_liquidates_copied_positions(ctx, client):
    from olala.domain.models import (PositionStatus, TraderProfile,
                                     TraderStatus)

    wallets = ctx.portfolio.wallets()
    w1, w2 = wallets[0], wallets[1]
    ctx.registry.update(TraderProfile(
        address="TraderX", status=TraderStatus.FOLLOWED,
        assigned_wallet_id=w1.id))
    position = open_copied_position(ctx, w1)
    events = ctx.bus.subscribe()

    response = client.post("/api/traders/TraderX/assign",
                           json={"wallet_id": w2.id})

    assert response.status_code == 200
    closed = next(p for p in ctx.portfolio.all_positions()
                  if p.id == position.id)
    assert closed.status is PositionStatus.CLOSED
    assert closed.exit_reason == "reassigned"
    assert ctx.portfolio.open_positions() == []
    # The liquidation is announced BEFORE the moon re-anchors, so the
    # frontend never shows the old position hanging on the new wallet.
    kinds = []
    while not events.empty():
        kinds.append(events.get_nowait()["type"])
    assert kinds.index("position_closed") < kinds.index("trader_reassigned")


def test_reassign_aborts_when_liquidation_blocked(ctx, client):
    from olala.domain.models import TraderProfile, TraderStatus

    ctx.keystore.unlock("pw")
    live = ctx.portfolio.add_live_wallet("Vault", "LiveAddr111")
    paper = ctx.portfolio.wallets()[0]
    ctx.registry.update(TraderProfile(
        address="TraderX", status=TraderStatus.FOLLOWED,
        assigned_wallet_id=live.id))
    # A copied position sits in the DARK live wallet: it cannot close.
    open_copied_position(ctx, live)

    response = client.post("/api/traders/TraderX/assign",
                           json={"wallet_id": paper.id})

    assert response.status_code == 409
    assert "could not liquidate" in response.get_json()["error"]
    # Nothing moved: assignment intact, position still open.
    assert ctx.registry.get("TraderX").assigned_wallet_id == live.id
    assert len(ctx.portfolio.open_positions()) == 1


def test_reassign_to_same_wallet_is_a_noop(ctx, client):
    from olala.domain.models import (PositionStatus, TraderProfile,
                                     TraderStatus)

    w1 = ctx.portfolio.wallets()[0]
    ctx.registry.update(TraderProfile(
        address="TraderX", status=TraderStatus.FOLLOWED,
        assigned_wallet_id=w1.id))
    position = open_copied_position(ctx, w1)

    response = client.post("/api/traders/TraderX/assign",
                           json={"wallet_id": w1.id})

    assert response.status_code == 200
    still_open = next(p for p in ctx.portfolio.all_positions()
                      if p.id == position.id)
    assert still_open.status is PositionStatus.OPEN
