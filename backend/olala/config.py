"""Application configuration.

Configuration is layered in three:

    built-in defaults      strict, safe, and the source of truth for
                           anything the operator did not state
    config.yaml            the hand-written file — READ ONLY to this
                           process, so its comments and structure survive
    config.runtime.yaml    overrides written by the REST API at runtime

The runtime file exists because ``yaml.safe_dump`` cannot preserve
comments: a single ``PUT /api/config`` used to rewrite the whole
hand-written file and silently delete every explanation in it. Keeping
operator intent and machine state in separate files means the UI can
persist a change without destroying documentation, and the operator can
always see which values were altered at runtime by reading one short
file.

There is exactly one config file per concern and no trading profiles.
An earlier design split the file by trading style (``config.hft.yaml`` /
``config.slow.yaml``) selected by an ``hft`` flag; that is gone, along
with high-frequency mode itself.
"""

from __future__ import annotations

import copy
import logging
import threading
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
RUNTIME_SUFFIX = ".runtime.yaml"

# The leaderboard service accepts exactly these ranking fields; anything
# else would silently fall back to a server-side default, so it is
# rejected at load instead.
VALID_LEADERBOARD_SORTS = ("roi", "win_percentage", "realized", "trades")

# Every routing policy that must exist. Naming them makes a typo in the
# config a startup error instead of a silently empty fall-through chain.
ROUTING_POLICIES = ("tracking", "history", "metadata", "broadcast",
                    "confirm", "stream")


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8420


@dataclass
class SourceConfig:
    """One RPC source, described by what was MEASURED against it.

    ``max_wallet_calls_per_sec`` is the load-bearing number: public nodes
    meter by sub-call, so a batch of 50 addresses costs 50, and the
    sustainable polling interval is `roster / this`. Measured on
    publicnode: 10/s is clean, 16.7/s throttles within ~40 seconds.
    """

    endpoints: list[str] = field(default_factory=list)
    ws_endpoint: str = ""
    supports_batch: bool = False
    max_batch: int = 1
    max_wallet_calls_per_sec: float = 1.0
    # Metered sources bill per sub-call against a monthly allowance, so
    # they cannot serve a continuous heartbeat however fast they are.
    metered: bool = False
    monthly_credit_cap: int = 0
    enabled: bool = True
    # Set from chain.helius_api_key at load; never written by hand.
    api_key: str = ""


def _default_sources() -> dict[str, SourceConfig]:
    return {
        "publicnode": SourceConfig(
            endpoints=["https://solana-rpc.publicnode.com"],
            ws_endpoint="wss://solana-rpc.publicnode.com",
            supports_batch=True, max_batch=100,
            max_wallet_calls_per_sec=10.0),
        "helius": SourceConfig(
            endpoints=["https://mainnet.helius-rpc.com/"],
            ws_endpoint="wss://mainnet.helius-rpc.com/",
            # MEASURED: a 10-element batch is served, 50 is refused with
            # HTTP 429 in 34 ms — it counts elements against the tier.
            supports_batch=True, max_batch=10,
            max_wallet_calls_per_sec=8.0,
            metered=True, monthly_credit_cap=1_000_000,
            enabled=False),          # enabled by presence of an API key
        "mainnet_beta": SourceConfig(
            endpoints=["https://api.mainnet-beta.solana.com"],
            # MEASURED: batching returns per-element 429s on 42 of 50 and
            # responses arrive OUT OF ORDER. Single calls only.
            supports_batch=False, max_batch=1,
            max_wallet_calls_per_sec=1.0),
    }


@dataclass
class RoutingConfig:
    """Ordered fall-through per policy, most preferred first.

    Helius is deliberately absent from ``tracking``: it refuses batches
    the size of our roster, and a round-robin heartbeat there would cost
    roughly 2.6M credits/month against a 1M allowance.
    """

    tracking: list[str] = field(
        default_factory=lambda: ["publicnode", "mainnet_beta"])
    history: list[str] = field(
        default_factory=lambda: ["publicnode", "helius", "mainnet_beta"])
    metadata: list[str] = field(
        default_factory=lambda: ["helius", "publicnode"])
    broadcast: list[str] = field(
        default_factory=lambda: ["helius", "publicnode"])
    confirm: list[str] = field(
        default_factory=lambda: ["helius", "publicnode"])
    stream: list[str] = field(
        default_factory=lambda: ["helius", "publicnode"])


@dataclass
class ChainConfig:
    helius_api_key: str = ""
    # Solana Tracker Data API (free tier: 10k requests/month). Feeds the
    # PnL leaderboard as a candidate source; empty key just means the
    # scanner relies on winners' holders alone.
    solana_tracker_api_key: str = ""
    request_timeout_sec: float = 15.0
    # Slippage tolerance for LIVE swaps, in basis points. This decides
    # how much of a real order the market may take; it was a literal in
    # a function signature before.
    slippage_bps: int = 100


@dataclass
class TrackingConfig:
    """How followed wallets are watched. See ``trading/tracker.py``.

    Detection is the WebSocket's job; this governs the reconciliation
    sweep that proves nothing was missed.
    """

    # Floor for the BATCH gear. The interval actually used is derived
    # from roster size against the source's measured ceiling, so a
    # growing roster widens the cadence instead of throttling.
    min_interval_sec: float = 5.0
    # ROUND_ROBIN gear: one wallet per tick, ~1 call/s regardless of
    # roster size. Ten times cheaper than batching, and the mode used
    # whenever the stream is proven live.
    tick_sec: float = 1.0
    signatures_per_poll: int = 30
    max_transactions_per_cycle: int = 60
    # A signal older than this may not OPEN a position: after an outage
    # the backlog would otherwise buy into trades already exited. Exits
    # are never blocked by age.
    max_signal_age_sec: float = 90.0
    price_mark_interval_sec: int = 20
    # How long the push stream gets to deliver a trade before the poll
    # finding it first counts as a MISS. logsSubscribe dies silently on
    # public RPC, so the stream is judged by what it fails to deliver —
    # never by silence, which a quiet market produces on its own.
    stream_miss_grace_sec: float = 20.0


@dataclass
class OnChainFilters:
    """Trader admission thresholds. Defaults are the 'strict' preset."""

    min_history_days: int = 90
    min_trades: int = 200
    min_win_rate: float = 0.60
    max_inactive_hours: int = 24
    min_token_market_cap_usd: float = 20_000_000.0
    min_token_liquidity_usd: float = 500_000.0
    max_token_market_cap_usd: float = 5_000_000_000.0
    # Copyability gates: profitable is not enough — the trading style must
    # be followable at our latency. Arb/MEV bots fail both of these.
    max_trades_per_day: float = 40.0
    min_median_hold_minutes: float = 30.0
    # Bag accounting: unsold in-window inventory older than this counts as
    # a realized loss in the adjusted win rate ("never sell the losers"
    # cannot inflate the metric). Per-trade Sharpe must clear this floor.
    stale_bag_days: float = 7.0
    min_sharpe: float = 0.1
    # Closed round trips required before a win rate is considered
    # meaningful at all.
    min_round_trips: int = 20


@dataclass
class RiskConfig:
    max_liquidity_fraction: float = 0.01
    reserve_fraction: float = 0.30
    per_trade_fraction: float = 0.05
    max_positions_per_wallet: int = 8
    atr_period: int = 14
    atr_stop_multiplier: float = 3.5
    max_token_top10_holder_fraction: float = 0.50
    # A pool younger than this is untested; 0 disables the check (dev).
    min_pair_age_days: float = 14.0
    # Correlation gate: a token may be held by at most this many LIVE
    # wallets at once (SOL is the base currency, never a position, so it
    # is inherently exempt). Paper wallets are exempt by design.
    max_live_wallets_per_token: int = 2
    # An order smaller than this is not worth its fees.
    min_order_sol: float = 0.05
    # A single position may never exceed this multiple of the wallet's
    # per-trade share, whatever the sizing model computes.
    max_position_equity_multiple: float = 2.0


@dataclass
class PaperFillModel:
    """How simulated fills are priced.

    These decide whether paper results mean anything, so they are
    configuration, not literals buried in the executor.
    """

    fee_sol: float = 0.000105
    base_spread: float = 0.001
    max_modeled_impact: float = 0.05


@dataclass
class SolanaTrackerFilters:
    """STREAM A — Solana Tracker's PnL leaderboard.

    The service has already done the work: it ranks wallets and gates
    them server-side on trade count, active days, ROI, win rate and
    non-arbitrage. We take its output as given and follow the names it
    returns. **The ``filters_onchain`` section does NOT apply to this
    stream** — that governs on-chain discovery, where nobody has vetted
    anything for us. The stream runs whenever
    ``chain.solana_tracker_api_key`` is set — clearing that key is how
    you turn it off, so there is exactly one switch rather than two that
    can disagree.
    """

    # Ranking: "roi" (return on capital — separates skill from scale),
    # "realized" (absolute PnL — favours volume machines),
    # "win_percentage", or "trades".
    sort: str = "roi"
    # Board window, and the floors pushed to the service.
    window_days: int = 30
    min_active_days: int = 10
    min_trades: int = 20
    # ROI stays in PERCENT — the `_pct` suffix carries the unit, and
    # real ROI values (100%, 20000%) are unwieldy as fractions.
    min_roi_pct: float = 0.0
    # Win rate is a FRACTION (0.55 = 55%), matching
    # `filters_onchain.min_win_rate` and TraderStats.win_rate. Mixing
    # units across two win-rate settings once let 0.7 mean 0.7% instead
    # of 70%, which admitted traders winning one trade in five.
    min_win_rate: float = 0.0
    # Pages walked per poll (100 wallets each). pages x polls/month must
    # stay inside the service tier (free tier: 10k requests/month).
    pages: int = 3
    interval_sec: int = 3600
    # NOT a quality judgment — a mechanical limit. We cannot copy, or
    # afford, a wallet trading faster than this. 0 disables the cap.
    max_trades_per_day: float = 2000.0


@dataclass
class DiscoveryConfig:
    """STREAM B — our own on-chain discovery (winners' holders).
    Nobody has vetted these wallets, so they earn their seat the hard
    way: pre-screen, full history scan, then the ``filters_onchain``
    admission gate."""

    scan_interval_sec: int = 180
    max_followed_traders: int = 10
    max_candidates_per_scan: int = 120
    signatures_per_trader: int = 3000
    rpc_calls_per_scan: int = 200
    # Skill is measured over this window: win rate, PnL, holds, and
    # trades/day are computed from the last N days only, so a lucky month
    # a year ago cannot carry a wallet. History/activity requirements
    # still use the full record.
    skill_window_days: int = 90
    # Roster replacement: a passing candidate evicts the weakest followed
    # trader only when its score clears the incumbent's by this margin —
    # hysteresis so statistical noise cannot churn the roster.
    replace_margin: float = 0.02
    # Winners-holders harvest: tokens that ran at least this hard over 24h
    # with at least this much liquidity get their top holders read — the
    # wallets holding size in a winner bought early by construction.
    winner_min_price_change_pct: float = 30.0
    winner_min_liquidity_usd: float = 150_000.0
    winners_per_scan: int = 3
    winner_top_holders: int = 20
    # Accounts above this share of supply are pool vaults, lockers, or
    # exchange omnibus wallets, not traders.
    winner_max_holder_share: float = 0.10


@dataclass
class PaperConfig:
    wallet_count: int = 3
    starting_sol: float = 10.0


@dataclass
class AppConfig:
    # THE FILTER SWITCH — note the direction, it is deliberately the
    # opposite of the usual "dev mode means relaxed":
    #     dev_mode: true   -> APPLY every on-chain filter (strict)
    #     dev_mode: false  -> IGNORE them (follow what discovery finds)
    # It governs `filters_onchain` only: the pre-screen, scan depth and
    # the admission gate for STREAM B. It never touches the Solana
    # Tracker stream (that stream is vetted upstream and configured by
    # `filters_solanatracker`), and it can never expose live money to an
    # unscreened token — the safety screen is unconditional for live
    # wallets no matter how this is set.
    dev_mode: bool = False
    server: ServerConfig = field(default_factory=ServerConfig)
    chain: ChainConfig = field(default_factory=ChainConfig)
    sources: dict[str, SourceConfig] = field(default_factory=_default_sources)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    filters_onchain: OnChainFilters = field(default_factory=OnChainFilters)
    filters_solanatracker: SolanaTrackerFilters = field(
        default_factory=SolanaTrackerFilters)
    risk: RiskConfig = field(default_factory=RiskConfig)
    paper_fills: PaperFillModel = field(default_factory=PaperFillModel)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)

    _MUTABLE_SECTIONS = ("filters_onchain", "filters_solanatracker",
                         "risk", "discovery", "tracking", "paper_fills")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ConfigStore:
    """Thread-safe owner of the live :class:`AppConfig`."""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self._path = path
        self._runtime_path = path.with_name(path.stem + RUNTIME_SUFFIX)
        self._lock = threading.RLock()
        self._overrides: dict[str, Any] = {}
        self._config = self._load()

    @property
    def config(self) -> AppConfig:
        with self._lock:
            return copy.deepcopy(self._config)

    @property
    def runtime_path(self) -> Path:
        return self._runtime_path

    def update(self, patch: dict[str, Any]) -> AppConfig:
        """Apply a partial update to mutable sections and persist it.

        Transactional: the patch lands on a copy first, so a rejected
        value never leaves a half-mutated config behind. Only the changed
        values are written, and only to the runtime file.
        """
        with self._lock:
            updated = copy.deepcopy(self._config)
            overrides = copy.deepcopy(self._overrides)
            for section, values in patch.items():
                if section not in AppConfig._MUTABLE_SECTIONS:
                    raise ValueError(f"section not updatable: {section!r}")
                target = getattr(updated, section)
                if not isinstance(values, dict):
                    raise ValueError(f"section {section!r} expects an object")
                for key, value in values.items():
                    if not hasattr(target, key):
                        raise ValueError(f"unknown option {section}.{key}")
                    current = getattr(target, key)
                    coerced = type(current)(value)
                    setattr(target, key, coerced)
                    overrides.setdefault(section, {})[key] = coerced
            _validate(updated)
            self._config = updated
            self._overrides = overrides
            self._save()
            return copy.deepcopy(self._config)

    def _load(self) -> AppConfig:
        config = AppConfig()
        if self._path.exists():
            raw = yaml.safe_load(self._path.read_text()) or {}
            _apply(config, raw)
        if self._runtime_path.exists():
            self._overrides = yaml.safe_load(
                self._runtime_path.read_text()) or {}
            _apply(config, self._overrides)
            logger.info("config: %s + runtime overrides from %s",
                        self._path.name, self._runtime_path.name)
        _wire_credentials(config)
        _validate(config)
        return config

    def _save(self) -> None:
        """Persist ONLY runtime overrides, and only to the runtime file.

        The hand-written config is never rewritten: ``yaml.safe_dump``
        cannot keep comments, and one REST update used to erase all of
        them.
        """
        self._runtime_path.write_text(
            "# Written by the running application — runtime overrides on\n"
            f"# top of {self._path.name}. Safe to delete: doing so simply\n"
            "# reverts to the hand-written configuration.\n"
            + yaml.safe_dump(self._overrides, sort_keys=False))


def _apply(config: AppConfig, raw: dict[str, Any]) -> None:
    """Overlay a raw mapping onto the config, section by section.

    Unknown sections and unknown keys are ignored rather than fatal, so
    an old file cannot prevent startup — but every ignored key is logged,
    because a silently dropped setting is how a filter stops applying
    without anyone noticing.
    """
    if "dev_mode" in raw:
        config.dev_mode = bool(raw["dev_mode"])
    if isinstance(raw.get("sources"), dict):
        _apply_sources(config, raw["sources"])
    for section, values in raw.items():
        if section in ("dev_mode", "sources"):
            continue
        target = getattr(config, section, None)
        if target is None or not is_dataclass(target):
            logger.warning("config: ignoring unknown section %r", section)
            continue
        if not isinstance(values, dict):
            logger.warning("config: section %r is not a mapping", section)
            continue
        for key, value in values.items():
            if not hasattr(target, key):
                logger.warning("config: ignoring unknown option %s.%s",
                               section, key)
                continue
            setattr(target, key, value)


def _apply_sources(config: AppConfig, raw: dict[str, Any]) -> None:
    for name, values in (raw or {}).items():
        if not isinstance(values, dict):
            logger.warning("config: sources.%s is not a mapping", name)
            continue
        source = config.sources.get(name) or SourceConfig()
        for key, value in values.items():
            if not hasattr(source, key):
                logger.warning("config: ignoring unknown option sources.%s.%s",
                               name, key)
                continue
            setattr(source, key, value)
        config.sources[name] = source


def _wire_credentials(config: AppConfig) -> None:
    """Give keyed sources their key, and enable them only if they have one.

    Keeping the key in ``chain`` rather than duplicated into the source
    block means there is one place to rotate it, and a source can never
    be enabled with an empty key.
    """
    helius = config.sources.get("helius")
    if helius is not None:
        helius.api_key = config.chain.helius_api_key
        # One switch, not two that can disagree: Helius participates
        # exactly when it has a key.
        helius.enabled = bool(helius.api_key)


def _validate(config: AppConfig) -> None:
    board = config.filters_solanatracker
    if board.sort not in VALID_LEADERBOARD_SORTS:
        raise ValueError(
            f"filters_solanatracker.sort must be one of "
            f"{VALID_LEADERBOARD_SORTS}, got {board.sort!r}")
    for label, value in (("filters_solanatracker.min_win_rate",
                          board.min_win_rate),
                         ("filters_onchain.min_win_rate",
                          config.filters_onchain.min_win_rate)):
        if not 0.0 <= value <= 1.0:
            raise ValueError(
                f"{label} is a FRACTION between 0 and 1 (0.55 = 55%), "
                f"got {value!r} — did you mean {value / 100:g}?")
    if 0.0 < board.min_roi_pct < 1.0:
        # 0.5 here means half a percent, which filters nothing; nobody
        # sets that on purpose.
        raise ValueError(
            f"filters_solanatracker.min_roi_pct is a PERCENT "
            f"(100 = 100%), got {board.min_roi_pct!r} — did you mean "
            f"{board.min_roi_pct * 100:g}?")

    if config.tracking.min_interval_sec <= 0 or config.tracking.tick_sec <= 0:
        raise ValueError("tracking intervals must be positive")

    # A routing policy naming a source that does not exist would fail
    # over into nothing at the worst possible moment, so it is caught here.
    for policy in ROUTING_POLICIES:
        chain = getattr(config.routing, policy, None)
        if not chain:
            raise ValueError(f"routing.{policy} must name at least one source")
        for name in chain:
            if name not in config.sources:
                raise ValueError(
                    f"routing.{policy} names unknown source {name!r}; "
                    f"known sources: {sorted(config.sources)}")
    if not any(config.sources[n].enabled for n in config.routing.tracking):
        raise ValueError(
            "routing.tracking has no enabled source — followed wallets "
            "would never be polled")
