"""SQLite persistence.

One ``Database`` instance owns the connection; all access is serialized
through an internal lock so daemon threads can share it safely. Domain
objects go in and come out — SQL never leaks past this module.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..domain.models import (Fill, ObservedTrade, Position, PositionStatus,
                             TraderProfile, TraderStats, TraderStatus,
                             TradeSide)

DB_PATH = Path(__file__).resolve().parent.parent.parent / "olala.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    address TEXT NOT NULL,
    is_paper INTEGER NOT NULL,
    sol_balance REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS traders (
    address TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    rejection_reason TEXT NOT NULL DEFAULT '',
    assigned_wallet_id TEXT NOT NULL DEFAULT '',
    discovered_at REAL NOT NULL,
    stats_json TEXT NOT NULL DEFAULT '{}',
    history_cursor TEXT NOT NULL DEFAULT '',
    history_complete INTEGER NOT NULL DEFAULT 0,
    follow_cursor TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS observed_trades (
    signature TEXT PRIMARY KEY,
    trader TEXT NOT NULL,
    side TEXT NOT NULL,
    mint TEXT NOT NULL,
    token_amount REAL NOT NULL,
    sol_amount REAL NOT NULL,
    price_sol REAL NOT NULL,
    block_time REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observed_trader ON observed_trades(trader, block_time);
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    wallet_id TEXT NOT NULL,
    trader TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity REAL NOT NULL,
    entry_price_sol REAL NOT NULL,
    sol_invested REAL NOT NULL,
    opened_at REAL NOT NULL,
    status TEXT NOT NULL,
    peak_price_sol REAL NOT NULL DEFAULT 0,
    stop_price_sol REAL NOT NULL DEFAULT 0,
    last_price_sol REAL NOT NULL DEFAULT 0,
    closed_at REAL NOT NULL DEFAULT 0,
    exit_reason TEXT NOT NULL DEFAULT '',
    realized_pnl_sol REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sightings (
    wallet TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 1,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    order_id TEXT PRIMARY KEY,
    wallet_id TEXT NOT NULL,
    side TEXT NOT NULL,
    mint TEXT NOT NULL,
    quantity REAL NOT NULL,
    price_sol REAL NOT NULL,
    sol_amount REAL NOT NULL,
    fee_sol REAL NOT NULL,
    executed_at REAL NOT NULL,
    signature TEXT NOT NULL DEFAULT ''
);
"""


class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(wallets)")}
        if "armed" not in columns:
            self._conn.execute(
                "ALTER TABLE wallets ADD COLUMN armed INTEGER NOT NULL DEFAULT 0")

    # -- wallets -----------------------------------------------------------

    def upsert_wallet(self, wallet_id: str, label: str, address: str,
                      is_paper: bool, sol_balance: float,
                      armed: bool = False) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO wallets(id,label,address,is_paper,sol_balance,"
                "armed) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "label=excluded.label, sol_balance=excluded.sol_balance, "
                "armed=excluded.armed",
                (wallet_id, label, address, int(is_paper), sol_balance,
                 int(armed)))

    def load_wallets(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM wallets").fetchall()
        return [dict(r) for r in rows]

    # -- traders -----------------------------------------------------------

    def upsert_trader(self, profile: TraderProfile, history_cursor: str = "",
                      history_complete: bool = False,
                      follow_cursor: str = "") -> None:
        stats_json = json.dumps(
            profile.stats.to_dict() if profile.stats else {})
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO traders(address,status,score,rejection_reason,"
                "assigned_wallet_id,discovered_at,stats_json,history_cursor,"
                "history_complete,follow_cursor) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(address) DO UPDATE SET status=excluded.status, "
                "score=excluded.score, rejection_reason=excluded.rejection_reason, "
                "assigned_wallet_id=excluded.assigned_wallet_id, "
                "stats_json=excluded.stats_json, "
                "history_cursor=excluded.history_cursor, "
                "history_complete=excluded.history_complete, "
                "follow_cursor=excluded.follow_cursor",
                (profile.address, profile.status.value, profile.score,
                 profile.rejection_reason, profile.assigned_wallet_id,
                 profile.discovered_at, stats_json, history_cursor,
                 int(history_complete), follow_cursor))

    def load_traders(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM traders").fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def trader_from_row(row: dict[str, Any]) -> TraderProfile:
        stats_raw = json.loads(row["stats_json"] or "{}")
        stats = None
        if stats_raw:
            stats = TraderStats(
                address=row["address"],
                first_trade_at=stats_raw.get("first_trade_at", 0.0),
                last_trade_at=stats_raw.get("last_trade_at", 0.0),
                total_trades=stats_raw.get("total_trades", 0),
                closed_round_trips=stats_raw.get("closed_round_trips", 0),
                wins=stats_raw.get("wins", 0),
                realized_pnl_sol=stats_raw.get("realized_pnl_sol", 0.0),
                distinct_tokens=stats_raw.get("distinct_tokens", 0),
                median_token_liquidity_usd=stats_raw.get(
                    "median_token_liquidity_usd", 0.0),
                median_hold_minutes=stats_raw.get(
                    "median_hold_minutes", 0.0),
                open_bags=stats_raw.get("open_bags", 0),
                bag_cost_sol=stats_raw.get("bag_cost_sol", 0.0),
                sharpe=stats_raw.get("sharpe", 0.0))
        return TraderProfile(
            address=row["address"], status=TraderStatus(row["status"]),
            stats=stats, score=row["score"],
            rejection_reason=row["rejection_reason"],
            assigned_wallet_id=row["assigned_wallet_id"],
            discovered_at=row["discovered_at"])

    # -- observed trades ---------------------------------------------------

    def insert_observed_trades(self, trades: list[ObservedTrade]) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO observed_trades VALUES(?,?,?,?,?,?,?,?)",
                [(t.signature, t.trader, t.side.value, t.mint, t.token_amount,
                  t.sol_amount, t.price_sol, t.block_time) for t in trades])

    def count_observed_trades(self, trader: str) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM observed_trades WHERE trader=?",
                (trader,)).fetchone()[0]

    def load_observed_trades(self, trader: str) -> list[ObservedTrade]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM observed_trades WHERE trader=? ORDER BY block_time",
                (trader,)).fetchall()
        return [ObservedTrade(
            trader=r["trader"], signature=r["signature"],
            side=TradeSide(r["side"]), mint=r["mint"],
            token_amount=r["token_amount"], sol_amount=r["sol_amount"],
            price_sol=r["price_sol"], block_time=r["block_time"])
            for r in rows]

    # -- census sightings --------------------------------------------------

    def record_sightings(self, wallets: set[str]) -> None:
        """Tally wallets observed trading on a DEX this sweep (one count
        per wallet per sweep)."""
        import time
        now = time.time()
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO sightings(wallet,count,first_seen,last_seen) "
                "VALUES(?,1,?,?) ON CONFLICT(wallet) DO UPDATE SET "
                "count=count+1, last_seen=excluded.last_seen",
                [(w, now, now) for w in wallets])

    def frequent_sightings(self, min_count: int,
                           limit: int = 25) -> list[tuple[str, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT wallet, count FROM sightings WHERE count>=? "
                "ORDER BY count DESC, last_seen DESC LIMIT ?",
                (min_count, limit)).fetchall()
        return [(r["wallet"], r["count"]) for r in rows]

    # -- positions ---------------------------------------------------------

    def save_position(self, p: Position) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET quantity=excluded.quantity, "
                "sol_invested=excluded.sol_invested, status=excluded.status, "
                "peak_price_sol=excluded.peak_price_sol, "
                "stop_price_sol=excluded.stop_price_sol, "
                "last_price_sol=excluded.last_price_sol, "
                "entry_price_sol=excluded.entry_price_sol, "
                "closed_at=excluded.closed_at, exit_reason=excluded.exit_reason, "
                "realized_pnl_sol=excluded.realized_pnl_sol",
                (p.id, p.wallet_id, p.trader, p.mint, p.symbol, p.quantity,
                 p.entry_price_sol, p.sol_invested, p.opened_at,
                 p.status.value, p.peak_price_sol, p.stop_price_sol,
                 p.last_price_sol, p.closed_at, p.exit_reason,
                 p.realized_pnl_sol))

    def load_positions(self) -> list[Position]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM positions").fetchall()
        positions = []
        for r in rows:
            positions.append(Position(
                id=r["id"], wallet_id=r["wallet_id"], trader=r["trader"],
                mint=r["mint"], symbol=r["symbol"], quantity=r["quantity"],
                entry_price_sol=r["entry_price_sol"],
                sol_invested=r["sol_invested"], opened_at=r["opened_at"],
                status=PositionStatus(r["status"]),
                peak_price_sol=r["peak_price_sol"],
                stop_price_sol=r["stop_price_sol"],
                last_price_sol=r["last_price_sol"], closed_at=r["closed_at"],
                exit_reason=r["exit_reason"],
                realized_pnl_sol=r["realized_pnl_sol"]))
        return positions

    # -- fills -------------------------------------------------------------

    def save_fill(self, wallet_id: str, fill: Fill) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO fills VALUES(?,?,?,?,?,?,?,?,?,?)",
                (fill.order_id, wallet_id, fill.side.value, fill.mint,
                 fill.quantity, fill.price_sol, fill.sol_amount, fill.fee_sol,
                 fill.executed_at, fill.signature))

    def load_fills(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM fills ORDER BY executed_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]
