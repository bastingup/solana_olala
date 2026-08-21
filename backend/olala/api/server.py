"""Application wiring: builds every service, daemon, and the Flask app.

This is the composition root — the only module that knows concrete classes.
Everything else depends on the abstractions those classes implement.
"""

from __future__ import annotations

import logging
import random
import re
from pathlib import Path

from flask import Flask, send_from_directory
from flask.json.provider import DefaultJSONProvider
from flask_sock import Sock

from ..chain.health import SourceHealthDaemon
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
from ..trading.marker import MarkDaemon
from ..trading.portfolio import PortfolioManager
from ..trading.signals import SignalQueue
from ..trading.tracker import WalletTracker
from .rest import build_rest_blueprint
from .stream import register_stream

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"


# Anything whose name looks like a credential never reaches a client,
# whatever section it was added to. A hand-maintained list of secret
# keys is one forgotten entry away from publishing an API key.
#
# Matched on WORD boundaries, not raw substring: a bare substring test
# treated `max_tokens_per_day` as a secret because "token" is inside it,
# and silently dropped a real config field from the snapshot. A hint now
# has to be its own segment (start/end or between underscores), so
# `token`/`api_key`/`secret` still redact `auth_token`, `helius_api_key`,
# `client_secret`, while `tokens_per_day` and `tokens_traded` survive.
_SECRET_RE = re.compile(
    r"(?:^|_)(?:api_?key|secret|password|passphrase|mnemonic|"
    r"private_key|token)(?:$|_)",
    re.IGNORECASE)


def _is_secret_key(key: str) -> bool:
    return bool(_SECRET_RE.search(key))


def _redact_secrets(value):
    """Recursively drop credential-shaped keys from a config snapshot."""
    if isinstance(value, dict):
        return {k: _redact_secrets(v) for k, v in value.items()
                if not _is_secret_key(str(k))}
    if isinstance(value, list):
        return [_redact_secrets(v) for v in value]
    return value


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
        self.provider = provider or build_provider(config)
        self.market_data = market_data or MarketDataService()
        self.registry = TraderRegistry(self.db, self.bus)
        self.keystore = keystore or EncryptedKeystore()
        self.portfolio = PortfolioManager(
            self.db, self.bus, self.store, self.provider)
        self.atr = AtrTracker(config.risk.atr_period)
        safety = TokenSafetyScreen(self.provider)
        risk = RiskEngine()
        self.jupiter = jupiter = JupiterClient(config.chain.slippage_bps)
        self.engine = TradingEngine(
            self.store, self.portfolio, self.registry, self.market_data,
            safety, risk, self.bus,
            paper_executor=PaperExecutor(config.paper_fills),
            live_executor=LiveJupiterExecutor(
                jupiter, self.provider, self.keystore,
                on_receipt=self.record_receipt),
            tracking_health=lambda trader: self.tracker.blind_reason(trader),
            quoter=jupiter)
        # Detection and execution run at wildly different speeds, so the
        # tracker never calls the engine directly: it enqueues, and
        # workers execute. One confirming swap must not stop every other
        # wallet from being watched.
        self.signals = SignalQueue(self.engine.handle_signal)
        self.tracker = WalletTracker(
            self.store, self.provider, self.registry, self.db,
            self.signals, on_status=self._publish_tracking,
            stream_health=lambda: self.subscriber.healthy)
        self.subscriber = TraderSubscriber(
            self.provider, self.registry,
            on_activity=self.tracker.note_activity,
            on_alive=self.tracker.note_stream_alive)
        leaderboard = (SolanaTrackerClient(config.chain.solana_tracker_api_key)
                       if config.chain.solana_tracker_api_key else None)
        self.discovery = TraderDiscoveryDaemon(
            self.store, self.provider, self.market_data, self.registry,
            self.db, self.bus, assign_wallet=self.assign_wallet,
            jupiter=jupiter, tracker=leaderboard)
        self.daemons = [
            self.discovery,
            self.tracker,
            MarkDaemon(self.store, self.portfolio, self.market_data,
                       self.atr, self.engine, self.bus),
            self.subscriber,
        ]
        # Standby sources are never contacted by routing alone, so their
        # health would otherwise be unknown until the moment we needed
        # them — which is the worst moment to find out.
        if hasattr(self.provider, "router"):
            self.daemons.append(SourceHealthDaemon(self.provider.router))
        # After every close, refresh measured performance and rebalance
        # live-wallet priority. Wired last, once every collaborator exists.
        self.portfolio.on_close = self._after_position_closed
        logger.info("application context ready (provider: %s%s)",
                    self.provider.name,
                    ", dev mode" if config.dev_mode else "")

    # -- cross-service policies -------------------------------------------

    def _publish_tracking(self, status: dict) -> None:
        self.bus.publish("tracking_status", status)

    def record_receipt(self, receipt) -> None:
        """Persist and broadcast one live-order receipt — the on-chain
        audit trail must survive restarts and reach the operator's eyes
        no matter how the order ended."""
        self.db.save_receipt(receipt)
        self.bus.publish("receipt_recorded", receipt.to_dict())

    def assign_wallet(self, address: str = "") -> str:
        """Which wallet a followed trader should trade through.

        The SECOND performance hierarchy lives here: seats spread evenly by
        COUNT across wallets (so exposure never stacks), but WHICH trader
        lands on WHICH wallet is decided by our own measured track record —
        the top proven performers take the live-wallet seats first, and
        everything unproven starts on paper. See :meth:`_plan_assignment`.
        """
        plan = self._plan_assignment(extra=address or None)
        if address and address in plan:
            return plan[address]
        # No address (legacy caller) or an empty roster: fall back to the
        # least-loaded wallet so a seat is never left unassigned.
        wallets = self.portfolio.wallets()
        if not wallets:
            return ""
        load: dict[str, int] = {w.id: 0 for w in wallets}
        for profile in self.registry.followed():
            if profile.assigned_wallet_id in load:
                load[profile.assigned_wallet_id] += 1
        minimum = min(load.values())
        return random.choice(
            [wid for wid, count in load.items() if count == minimum])

    def _plan_assignment(self, extra: str | None = None) -> dict[str, str]:
        """Target wallet for every followed trader, best performers first.

        Wallets are ordered LIVE-first (the premium seats), then paper;
        each gets an even share of the roster by count. Traders are ranked
        by measured realized PnL — proven performers descending, then the
        unproven — and dealt into that ordering, so the strongest measured
        traders fill the live wallets and the unproven ones sit on paper
        until they earn a record. Fully deterministic (address breaks
        ties), so equal traders never churn between wallets.
        """
        wallets = self.portfolio.wallets()
        if not wallets:
            return {}
        addresses = [p.address for p in self.registry.followed()]
        if extra and extra not in addresses:
            addresses.append(extra)
        if not addresses:
            return {}
        perf = self.portfolio.trader_performance()

        ordered = sorted(wallets, key=lambda w: (w.is_paper, w.id))
        count, k = len(addresses), len(ordered)
        capacity = [count // k + (1 if i < count % k else 0) for i in range(k)]

        def rank_key(addr: str):
            measured = perf.get(addr)
            proven = bool(measured and measured.proven)
            pnl = measured.realized_pnl_sol if (measured and proven) else 0.0
            # proven-before-unproven, then PnL desc, then a stable tiebreak
            return (0 if proven else 1, -pnl, addr)

        plan: dict[str, str] = {}
        index = 0
        for addr in sorted(addresses, key=rank_key):
            while index < k and capacity[index] == 0:
                index += 1
            if index >= k:
                index = k - 1
            plan[addr] = ordered[index].id
            capacity[index] -= 1
        return plan

    def rebalance_assignments(self) -> int:
        """Move traders toward the desired live/paper layout — SAFELY.

        Only a trader that is FLAT (no open position anywhere) is moved, so
        rebalancing never liquidates real money: a trader holding a
        position keeps its wallet until it closes out on its own, then gets
        repositioned on the next close. Returns how many were moved.
        """
        plan = self._plan_assignment()
        moved = 0
        for profile in self.registry.followed():
            target = plan.get(profile.address)
            if not target or target == profile.assigned_wallet_id:
                continue
            if self.portfolio.has_open_for_trader(profile.address):
                continue  # not flat — moving it would strand/liquidate money
            profile.assigned_wallet_id = target
            self.registry.update(profile, event="trader_reassigned")
            moved += 1
        return moved

    def _after_position_closed(self, position) -> None:
        """Runs after every committed close: refresh the measured
        performance the frontend colours moons by, then rebalance which
        traders sit on live wallets (safe swaps only)."""
        self.bus.publish("trader_performance", self._performance_payload())
        try:
            self.rebalance_assignments()
        except Exception:                                   # noqa: BLE001
            # A rebalance failure must never break the close that triggered
            # it — the position is already closed and booked.
            logger.exception("assignment rebalance failed after a close")

    def _performance_payload(self) -> dict:
        return {"traders": {a: m.to_dict()
                            for a, m in
                            self.portfolio.trader_performance().items()}}

    # -- snapshots ---------------------------------------------------------

    def public_config(self) -> dict:
        """The configuration as the browser may see it.

        Every credential is replaced by a boolean. This runs over the
        WHOLE config, not a hand-listed pair of keys: `sources.*.api_key`
        is populated from `chain.helius_api_key` at load, so a redactor
        that only knew about `chain` would have shipped the key to every
        connected client through the source block instead.
        """
        config = self.store.config.to_dict()
        chain = config.get("chain", {})
        chain["helius_enabled"] = bool(chain.pop("helius_api_key", ""))
        chain["solana_tracker_enabled"] = bool(
            chain.pop("solana_tracker_api_key", ""))
        for source in (config.get("sources") or {}).values():
            source.pop("api_key", None)
        return _redact_secrets(config)

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
            "tracking": self.tracker.status.to_dict(),
            "sources": (self.provider.router.metrics()
                        if hasattr(self.provider, "router") else {}),
            "trader_performance": self._performance_payload(),
        })
        return snapshot

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.signals.start()
        for daemon in self.daemons:
            daemon.start()

    def stop(self) -> None:
        for daemon in self.daemons:
            daemon.stop()
        self.signals.stop()


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
