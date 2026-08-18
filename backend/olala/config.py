"""Application configuration.

Configuration is layered: built-in defaults, overridden by the master
``config.yaml``, overridden by the active TRADING PROFILE file, then by
runtime REST updates (persisted back to whichever file owns the section).

The master file holds identity and machine-wide settings — the mode flags
(``dev_mode``, ``hft``), server, chain credentials, risk exposure, and
paper wallets. The strategy-shaped sections live in one profile file per
trading style::

    config.yaml         master: modes + risk exposure + credentials
    config.hft.yaml     filters/discovery/follow for high-frequency
    config.slow.yaml    filters/discovery/follow for slow/swing

``hft: true`` in the master selects ``config.hft.yaml``; ``false`` selects
``config.slow.yaml``. Switching styles is therefore a one-line edit plus a
restart — no code changes, no value hunting across a single large file.

A legacy single-file config still works: profile sections found in the
master are applied first, and the profile file (when present) overrides
them. The mode flags always live in the master, so the running config can
never disagree with what the operator set — a parallel-file design that
hid the flag from the UI was reverted once before for exactly that.
"""

from __future__ import annotations

import copy
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

# Sections that stay in the master file: identity, credentials, money at
# risk, and the simulated fleet — all independent of trading style.
MASTER_SECTIONS = ("server", "chain", "risk", "paper")
# Sections that define a trading style and therefore live in a profile.
PROFILE_SECTIONS = ("filters", "discovery", "follow")

# The leaderboard service accepts exactly these ranking fields; anything
# else would silently fall back to a server-side default, so it is
# rejected at load instead.
VALID_LEADERBOARD_SORTS = ("win_percentage", "realized", "trades")


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
    # The leaderboard service is polled at most this often (seconds) so a
    # free API tier lasts the month; between polls the on-chain sources
    # carry the sweep. 900s ≈ 2.9k requests/month.
    leaderboard_interval_sec: int = 900
    # Nominations must show sustained trading: minimum active trading
    # days inside the window. The service's own default is 3, which
    # nominates week-old wallets that can never clear our history gate.
    leaderboard_min_active_days: int = 30
    # Leaderboard ranking: "win_percentage" (service win rate),
    # "realized" (net realized PnL) or "trades" (most active). The
    # deep-scan queue follows this order.
    leaderboard_sort: str = "win_percentage"
    # Pages walked per poll (100 wallets each) until enough copyable
    # nominees are found — the board's top is dominated by ultra-HF
    # machines our activity cap discards, so depth buys usable names.
    # Budget: pages × polls/month must stay inside the service tier.
    leaderboard_pages: int = 2
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
    # Trading profile selector: True loads config.hft.yaml (chase
    # high-frequency traders), False loads config.slow.yaml (slow/swing
    # traders). Read at boot only — changing it needs a restart, exactly
    # like dev_mode, because it reshapes the whole discovery pipeline.
    hft: bool = False
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
    """Thread-safe owner of the live :class:`AppConfig`.

    Loads the master file plus the profile file its ``hft`` flag selects,
    and writes each section back to the file that owns it.
    """

    def __init__(self, path: Path = CONFIG_PATH,
                 profile_dir: Path | None = None) -> None:
        self._path = path
        self._profile_dir = profile_dir or path.parent
        self._lock = threading.RLock()
        self._config = self._load()

    @property
    def config(self) -> AppConfig:
        with self._lock:
            return copy.deepcopy(self._config)

    @property
    def profile_name(self) -> str:
        """Name of the active trading profile ("hft" or "slow")."""
        with self._lock:
            return "hft" if self._config.hft else "slow"

    def profile_path(self, name: str | None = None) -> Path:
        return self._profile_dir / f"config.{name or self.profile_name}.yaml"

    def update(self, patch: dict[str, Any]) -> AppConfig:
        """Apply a partial update to mutable sections and persist it.

        Transactional: the patch lands on a copy first, so a rejected
        value never leaves a half-mutated config behind.
        """
        with self._lock:
            updated = copy.deepcopy(self._config)
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
                    setattr(target, key, type(current)(value))
            _validate(updated)
            self._config = updated
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
            if "hft" in raw:
                config.hft = bool(raw["hft"])
            # Profile sections are applied from the master FIRST so a
            # legacy single-file config keeps working; the profile file
            # below then overrides them.
            _apply(config, raw, MASTER_SECTIONS + PROFILE_SECTIONS)

        profile = self.profile_path("hft" if config.hft else "slow")
        if profile.exists():
            raw_profile = yaml.safe_load(profile.read_text()) or {}
            _apply(config, raw_profile, PROFILE_SECTIONS)
            logger.info("config: master %s + profile %s",
                        self._path.name, profile.name)
        elif self._path.exists():
            logger.warning(
                "config: profile %s not found — running on master file "
                "plus built-in defaults", profile.name)
        _validate(config)
        return config

    def _save(self) -> None:
        """Persist each section to the file that owns it.

        Splitting the write the same way as the read is what keeps the
        profile structure intact across runtime config updates.
        """
        full = self._config.to_dict()
        master = {"dev_mode": full["dev_mode"], "hft": full["hft"]}
        master.update({s: full[s] for s in MASTER_SECTIONS})
        self._path.write_text(yaml.safe_dump(master, sort_keys=False))

        profile = {s: full[s] for s in PROFILE_SECTIONS}
        self.profile_path().write_text(
            yaml.safe_dump(profile, sort_keys=False))


def _apply(config: AppConfig, raw: dict[str, Any],
           sections: tuple[str, ...]) -> None:
    for section in sections:
        values = raw.get(section) or {}
        target = getattr(config, section)
        for key, value in values.items():
            if hasattr(target, key):
                setattr(target, key, value)


def _validate(config: AppConfig) -> None:
    if config.discovery.leaderboard_sort not in VALID_LEADERBOARD_SORTS:
        raise ValueError(
            f"discovery.leaderboard_sort must be one of "
            f"{VALID_LEADERBOARD_SORTS}, got "
            f"{config.discovery.leaderboard_sort!r}")
