"""Application configuration.

Configuration is layered: built-in defaults, overridden by ``config.yaml``
next to the backend package, overridden at runtime through the REST API.
Runtime changes are persisted back to the YAML file so they survive restarts.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8420


@dataclass
class ChainConfig:
    rpc_endpoints: list[str] = field(default_factory=lambda: [
        "https://api.mainnet-beta.solana.com",
        "https://solana-rpc.publicnode.com",
    ])
    helius_api_key: str = ""
    birdeye_api_key: str = ""
    # Solana Tracker Data API (free tier: 10k requests/month). Feeds the
    # PnL leaderboard as a candidate source; empty key just means the
    # scanner relies on the on-chain census and winners' holders alone.
    solana_tracker_api_key: str = ""
    requests_per_second: float = 2.0
    request_timeout_sec: float = 15.0


@dataclass
class FilterConfig:
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


@dataclass
class DiscoveryConfig:
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
    # DEX census: every sweep observes the live flow of these programs and
    # tallies fee payers in a persistent sightings ledger. Wallets seen
    # trading repeatedly get their full window scored — enumeration is
    # "provably trades", judgment is only ever our computed win rate.
    census_programs: list[str] = field(default_factory=lambda: [
        "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",   # Jupiter v6
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",   # Orca Whirlpool
    ])
    census_tx_sample: int = 8
    census_min_sightings: int = 2
    # Birdeye leaderboard window for the primary candidate source.
    gainers_window: str = "1W"
    gainers_limit: int = 10
    # Leaderboard services are polled at most this often (seconds) so a
    # free API tier lasts the month; between polls the on-chain sources
    # carry the sweep. 900s ≈ 2.9k requests/month.
    leaderboard_interval_sec: int = 900
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
class FollowConfig:
    poll_interval_sec: int = 12
    price_mark_interval_sec: int = 20


@dataclass
class PaperConfig:
    wallet_count: int = 3
    starting_sol: float = 10.0


@dataclass
class AppConfig:
    # Dev mode: bypass the trader-admission and token-safety gates so
    # paper activity flows immediately (you will be copying bots and
    # trash — that is the point). Live wallets refuse to arm while on.
    dev_mode: bool = False
    server: ServerConfig = field(default_factory=ServerConfig)
    chain: ChainConfig = field(default_factory=ChainConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    follow: FollowConfig = field(default_factory=FollowConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)

    _MUTABLE_SECTIONS = ("filters", "risk", "discovery", "follow")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ConfigStore:
    """Thread-safe owner of the live :class:`AppConfig`."""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._config = self._load()

    @property
    def config(self) -> AppConfig:
        with self._lock:
            return copy.deepcopy(self._config)

    def update(self, patch: dict[str, Any]) -> AppConfig:
        """Apply a partial update to mutable sections and persist it."""
        with self._lock:
            for section, values in patch.items():
                if section not in AppConfig._MUTABLE_SECTIONS:
                    raise ValueError(f"section not updatable: {section!r}")
                target = getattr(self._config, section)
                if not isinstance(values, dict):
                    raise ValueError(f"section {section!r} expects an object")
                for key, value in values.items():
                    if not hasattr(target, key):
                        raise ValueError(f"unknown option {section}.{key}")
                    current = getattr(target, key)
                    setattr(target, key, type(current)(value))
            self._save()
            return copy.deepcopy(self._config)

    def _load(self) -> AppConfig:
        config = AppConfig()
        if self._path.exists():
            raw = yaml.safe_load(self._path.read_text()) or {}
            # A legacy "mode" key may linger in older config files; it is
            # ignored here and dropped on the next save.
            if "dev_mode" in raw:
                config.dev_mode = bool(raw["dev_mode"])
            for section in ("server", "chain", "filters", "risk",
                            "discovery", "follow", "paper"):
                values = raw.get(section) or {}
                target = getattr(config, section)
                for key, value in values.items():
                    if hasattr(target, key):
                        setattr(target, key, value)
        return config

    def _save(self) -> None:
        self._path.write_text(
            yaml.safe_dump(self._config.to_dict(), sort_keys=False))
