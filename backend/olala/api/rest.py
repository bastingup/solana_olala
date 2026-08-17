"""REST API: actions and snapshots.

The WebSocket stream carries all state changes; REST exists for commands —
adding wallets, unlocking the keystore, arming wallets, changing
configuration — plus a full state snapshot for initial page load.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..domain.models import ExitReason, TraderStatus
from ..security.keystore import KeystoreError


def build_rest_blueprint(app_context) -> Blueprint:
    api = Blueprint("api", __name__, url_prefix="/api")
    ctx = app_context

    @api.get("/state")
    def state():
        return jsonify(ctx.snapshot())

    # -- keystore ---------------------------------------------------------

    @api.post("/keystore/unlock")
    def unlock_keystore():
        payload = request.get_json(silent=True) or {}
        passphrase = payload.get("passphrase") or ""
        try:
            ctx.keystore.unlock(passphrase)
        except KeystoreError as exc:
            return jsonify({"error": str(exc)}), 400
        ctx.bus.publish("keystore_unlocked", {})
        return jsonify({"ok": True})

    # -- wallets ----------------------------------------------------------

    @api.get("/wallets")
    def list_wallets():
        return jsonify([ctx.portfolio.wallet_summary(w)
                        for w in ctx.portfolio.wallets()])

    @api.post("/wallets")
    def add_wallet():
        payload = request.get_json(silent=True) or {}
        label = (payload.get("label") or "").strip() or "Wallet"
        if payload.get("paper"):
            starting = float(payload.get("starting_sol")
                             or ctx.store.config.paper.starting_sol)
            wallet = ctx.portfolio.add_paper_wallet(label, starting)
            return jsonify(ctx.portfolio.wallet_summary(wallet)), 201
        secret = payload.get("secret") or ""
        if not secret:
            return jsonify({"error": "secret required for live wallet"}), 400
        try:
            address = ctx.keystore.add_key(label, secret)
        except KeystoreError as exc:
            return jsonify({"error": str(exc)}), 400
        wallet = ctx.portfolio.add_live_wallet(label, address)
        return jsonify(ctx.portfolio.wallet_summary(wallet)), 201

    @api.post("/wallets/<wallet_id>/arm")
    def arm_wallet(wallet_id: str):
        payload = request.get_json(silent=True) or {}
        armed = bool(payload.get("armed"))
        wallet = ctx.portfolio.get_wallet(wallet_id)
        if wallet is None:
            return jsonify({"error": "unknown wallet"}), 404
        if wallet.is_paper:
            return jsonify({"error": "paper wallets always simulate; "
                                     "there is nothing to arm"}), 400
        if armed:
            # Arming needs a signer; disarming must always be possible.
            if ctx.store.config.dev_mode:
                return jsonify({"error": "dev mode is on — arming live "
                                         "wallets is locked out because "
                                         "dev configs relax the safety "
                                         "screens"}), 400
            if ctx.keystore.is_locked:
                return jsonify({"error": "unlock the keystore first"}), 400
            try:
                ctx.keystore.get_signer(wallet.address)
            except KeystoreError:
                return jsonify({"error": "no key in the keystore for this "
                                         "wallet's address"}), 400
        ctx.portfolio.set_wallet_armed(wallet_id, armed)
        return jsonify(ctx.portfolio.wallet_summary(
            ctx.portfolio.get_wallet(wallet_id)))

    # -- traders ----------------------------------------------------------

    @api.get("/traders")
    def list_traders():
        return jsonify([p.to_dict() for p in ctx.registry.all()])

    @api.post("/traders/<address>/assign")
    def assign_trader(address: str):
        """Re-assign a followed trader to a different wallet (the galaxy's
        drag-and-drop lands here). Existing positions stay with the wallet
        that opened them; new copies go to the new wallet."""
        payload = request.get_json(silent=True) or {}
        wallet_id = payload.get("wallet_id") or ""
        profile = ctx.registry.get(address)
        if profile is None:
            return jsonify({"error": "unknown trader"}), 404
        wallet = ctx.portfolio.get_wallet(wallet_id)
        if wallet is None:
            return jsonify({"error": "unknown wallet"}), 404
        if profile.status is not TraderStatus.FOLLOWED:
            return jsonify({"error": "only followed traders can be "
                                     "assigned to a wallet"}), 400
        profile.assigned_wallet_id = wallet.id
        ctx.registry.update(profile, event="trader_reassigned")
        return jsonify(profile.to_dict())

    @api.post("/traders/<address>/unfollow")
    def unfollow(address: str):
        profile = ctx.registry.get(address)
        if profile is None:
            return jsonify({"error": "unknown trader"}), 404
        profile.status = TraderStatus.RETIRED
        ctx.registry.update(profile, event="trader_retired")
        return jsonify(profile.to_dict())

    # -- positions --------------------------------------------------------

    @api.get("/positions")
    def list_positions():
        return jsonify([p.to_dict() for p in ctx.portfolio.all_positions()])

    @api.post("/positions/<position_id>/close")
    def close_position(position_id: str):
        position = next((p for p in ctx.portfolio.open_positions()
                         if p.id == position_id), None)
        if position is None:
            return jsonify({"error": "no such open position"}), 404
        ctx.engine.close_position(position, ExitReason.MANUAL)
        return jsonify({"ok": True})

    @api.get("/fills")
    def list_fills():
        return jsonify(ctx.db.load_fills())

    @api.get("/receipts")
    def list_receipts():
        return jsonify(ctx.db.load_receipts())

    # -- configuration and mode -------------------------------------------

    @api.get("/config")
    def get_config():
        return jsonify(ctx.public_config())

    @api.put("/config")
    def put_config():
        payload = request.get_json(silent=True) or {}
        try:
            ctx.store.update(payload)
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400
        ctx.bus.publish("config_changed", ctx.public_config())
        return jsonify(ctx.public_config())

    return api
