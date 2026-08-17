"""Application wiring: builds every service, daemon, and the Flask app.

This is the composition root — the only module that knows concrete classes.
Everything else depends on the abstractions those classes implement.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path

from flask import Flask, send_from_directory
from flask.json.provider import DefaultJSONProvider
from flask_sock import Sock

from ..chain.birdeye import BirdeyeClient
from ..chain.jupiter import JupiterClient
from ..chain.market_data import MarketDataService
from ..chain.provider import build_provider
from ..chain.solana_tracker import SolanaTrackerClient
from ..chain.subscriber import TraderSubscriber
from ..config import ConfigStore
from ..discovery.scanner import TraderDiscoveryDaemon
from ..events import EventBus, json_safe
from ..persistence.database import Database
from ..risk.atr import AtrTracker
from ..risk.engine import RiskEngine
from ..risk.token_safety import TokenSafetyScreen
from ..security.keystore import EncryptedKeystore
from ..services.traders import TraderRegistry
from ..trading.engine import TradingEngine
from ..trading.executor import LiveJupiterExecutor, PaperExecutor
from ..trading.follower import FollowDaemon
from ..trading.marker import MarkDaemon
from ..trading.portfolio import PortfolioManager
from .rest import build_rest_blueprint
from .stream import register_stream

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"


class AppContext:
    """Owns every long-lived service; handed to the API layers.

    The optional overrides exist for tests and tooling: they swap storage
    paths and the chain-facing services without touching the wiring.
    """

    def __init__(self, *, config_store=None, database=None, keystore=None,
                 provider=None, market_data=None) -> None:
        self.store = config_store or ConfigStore()
        config = self.store.config
        self.bus = EventBus()
        self.db = database or Database()
        self.provider = provider or build_provider(config.chain)
        self.market_data = market_data or MarketDataService()
        self.registry = TraderRegistry(self.db, self.bus)
        self.keystore = keystore or EncryptedKeystore()
        self.portfolio = PortfolioManager(
            self.db, self.bus, self.store, self.provider)
        self.atr = AtrTracker(config.risk.atr_period)
        safety = TokenSafetyScreen(self.provider)
        risk = RiskEngine()
        self.jupiter = jupiter = JupiterClient()
        self.engine = TradingEngine(
            self.store, self.portfolio, self.registry, self.market_data,
            safety, risk, self.bus,
            paper_executor=PaperExecutor(),
            live_executor=LiveJupiterExecutor(
                jupiter, self.provider, self.keystore,
                on_receipt=self.record_receipt))
        follower = FollowDaemon(self.store, self.provider, self.registry,
                                self.engine)
        birdeye = (BirdeyeClient(config.chain.birdeye_api_key)
                   if config.chain.birdeye_api_key else None)
        tracker = (SolanaTrackerClient(config.chain.solana_tracker_api_key)
                   if config.chain.solana_tracker_api_key else None)
        self.discovery = TraderDiscoveryDaemon(
            self.store, self.provider, self.market_data, self.registry,
            self.db, self.bus, assign_wallet=self.assign_wallet,
            birdeye=birdeye, jupiter=jupiter, tracker=tracker)
        self.daemons = [
            self.discovery,
            follower,
            MarkDaemon(self.store, self.portfolio, self.market_data,
                       self.atr, self.engine, self.bus),
            TraderSubscriber(self.provider, self.registry,
                             on_activity=follower.poll_now),
        ]
        logger.info("application context ready (provider: %s%s)",
                    self.provider.name,
                    ", dev mode" if config.dev_mode else "")

    # -- cross-service policies -------------------------------------------

    def record_receipt(self, receipt) -> None:
        """Persist and broadcast one live-order receipt — the on-chain
        audit trail must survive restarts and reach the operator's eyes
        no matter how the order ended."""
        self.db.save_receipt(receipt)
        self.bus.publish("receipt_recorded", receipt.to_dict())

    def assign_wallet(self) -> str:
        """Randomized wallet assignment biased toward the least-loaded
        wallet, so exposure spreads instead of stacking."""
        wallets = self.portfolio.wallets()
        if not wallets:
            return ""
        load: dict[str, int] = {w.id: 0 for w in wallets}
        for profile in self.registry.followed():
            if profile.assigned_wallet_id in load:
                load[profile.assigned_wallet_id] += 1
        minimum = min(load.values())
        candidates = [wid for wid, count in load.items() if count == minimum]
        return random.choice(candidates)

    # -- snapshots ---------------------------------------------------------

    def public_config(self) -> dict:
        config = self.store.config.to_dict()
        chain = config.get("chain", {})
        chain["helius_enabled"] = bool(chain.pop("helius_api_key", ""))
        chain["birdeye_enabled"] = bool(chain.pop("birdeye_api_key", ""))
        chain["solana_tracker_enabled"] = bool(
            chain.pop("solana_tracker_api_key", ""))
        return config

    def snapshot(self) -> dict:
        snapshot = self.portfolio.snapshot()
        snapshot.update({
            "dev_mode": self.store.config.dev_mode,
            "keystore": {"exists": self.keystore.exists,
                         "locked": self.keystore.is_locked},
            "traders": [p.to_dict() for p in self.registry.all()],
            "config": self.public_config(),
            "fills": self.db.load_fills(50),
            "receipts": self.db.load_receipts(50),
            "discovery": self.discovery.last_status,
        })
        return snapshot

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        for daemon in self.daemons:
            daemon.start()

    def stop(self) -> None:
        for daemon in self.daemons:
            daemon.stop()


class StrictJSONProvider(DefaultJSONProvider):
    """jsonify() that never emits Infinity/NaN — browsers reject those
    tokens and the whole response body becomes unreadable."""

    def dumps(self, obj, **kwargs) -> str:
        kwargs.setdefault("allow_nan", False)
        return super().dumps(json_safe(obj), **kwargs)


def build_app(ctx: AppContext) -> Flask:
    app = Flask(__name__, static_folder=str(FRONTEND_DIR),
                static_url_path="")
    app.json = StrictJSONProvider(app)
    app.register_blueprint(build_rest_blueprint(ctx))
    sock = Sock(app)
    register_stream(sock, ctx)

    @app.get("/")
    def index():
        return send_from_directory(str(FRONTEND_DIR), "index.html")

    return app
