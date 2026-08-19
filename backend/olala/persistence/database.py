"""SQLite persistence.

One ``Database`` instance owns the connection; all access is serialized
through an internal lock so daemon threads can share it safely. Domain
objects go in and come out — SQL never leaks past this module.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..domain.models import (Fill, ObservedTrade, Position, PositionStatus,
                             Receipt, TraderProfile, TraderStats,
                             TraderStatus, TradeSide)

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
CREATE TABLE IF NOT EXISTS processed_signatures (
    signature TEXT PRIMARY KEY,
    trader TEXT NOT NULL,
    slot INTEGER NOT NULL,
    processed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_trader_slot
    ON processed_signatures(trader, slot);
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
CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    order_id TEXT NOT NULL,
    wallet_id TEXT NOT NULL,
    side TEXT NOT NULL,
    mint TEXT NOT NULL,
    status TEXT NOT NULL,
    quoted_sol REAL NOT NULL DEFAULT 0,
    quoted_tokens REAL NOT NULL DEFAULT 0,
    actual_sol REAL NOT NULL DEFAULT 0,
    actual_tokens REAL NOT NULL DEFAULT 0,
    fee_sol REAL NOT NULL DEFAULT 0,
    slot INTEGER NOT NULL DEFAULT 0,
    block_time REAL NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipts_wallet
    ON receipts(wallet_id, created_at);
"""


SCHEMA_VERSION = 2

# Columns added after a table's original definition. Declaring them here
# rather than as bespoke checks means a new column is one line and is
# applied idempotently to every existing database; the previous code
# hardcoded a single `wallets.armed` probe, so the next added column
# would have needed its own hand-written guard.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("wallets", "armed", "INTEGER NOT NULL DEFAULT 0"),
    # The follow cursor gained a slot: a bare signature cannot be
    # compared, so a window that missed it used to look entirely fresh.
    ("traders", "follow_cursor_slot", "INTEGER NOT NULL DEFAULT 0"),
)


class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        self._configure()
        with self._write():
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def _configure(self) -> None:
        """Durability and concurrency pragmas.

        WAL lets readers (the API thread serialising /api/state) run
        while a daemon writes, which is the actual access pattern here.
        ``synchronous=NORMAL`` is the standard companion: it trades an
        fsync per commit for one per checkpoint, and under WAL it still
        cannot corrupt the database — only the last commits are at risk
        in a power loss, which for reconstructable trade history is the
        right trade. These must run outside any transaction.
        """
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def _migrate(self) -> None:
        self._conn.execute("CREATE TABLE IF NOT EXISTS schema_version ("
                           "version INTEGER PRIMARY KEY,"
                           "applied_at REAL NOT NULL)")
        # The DEX census was removed; its ledger would otherwise linger
        # in every existing database as a table nothing reads.
        self._conn.execute("DROP TABLE IF EXISTS sightings")
        for table, column, ddl in _ADDED_COLUMNS:
            self._ensure_column(table, column, ddl)
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) "
            "VALUES (?, ?)", (SCHEMA_VERSION, time.time()))

    def _ensure_column(self, table: str, column: str, ddl: str) -> bool:
        """Add ``column`` if the table lacks it. Returns whether it was added."""
        columns = {row["name"] for row in self._conn.execute(
            f"PRAGMA table_info({table})")}
        if column in columns:
            return False
        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        return True

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT max(version) AS v FROM schema_version").fetchone()
        return int(row["v"]) if row and row["v"] is not None else 0

    # -- write scoping -----------------------------------------------------

    @contextlib.contextmanager
    def transaction(self):
        """Commit several writes as one unit, or none of them.

        A position and its fill must land together: a crash between them
        leaves a position with no record of how it was entered. Nesting
        joins the outer transaction instead of committing early, so a
        helper cannot half-commit its caller's work.
        """
        with self._lock:
            if self._depth > 0:
                self._depth += 1
                try:
                    yield self
                finally:
                    self._depth -= 1
                return
            self._depth = 1
            try:
                with self._conn:
                    yield self
            finally:
                self._depth = 0

    @contextlib.contextmanager
    def _write(self):
        """Lock plus a transaction scope for a single write method.

        Inside an explicit :meth:`transaction` this joins it rather than
        committing, which is what makes grouped writes atomic.
        """
        with self._lock:
            if self._depth > 0:
                yield self._conn
            else:
                with self._conn:
                    yield self._conn

    # -- wallets -----------------------------------------------------------

    def upsert_wallet(self, wallet_id: str, label: str, address: str,
                      is_paper: bool, sol_balance: float,
                      armed: bool = False) -> None:
        with self._write():
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
                      history_complete: bool = False) -> None:
        """Persist a trader's identity, status and score.

        Deliberately does NOT touch `follow_cursor`/`follow_cursor_slot`.
        Those belong to the tracker (see :meth:`update_watermarks`) and
        used to be re-written from a registry cache on every status or
        score change — so an ordinary discovery update silently wiped the
        tracking watermark, and the tracker re-armed at the newest
        signature, skipping every trade in between.
        """
        stats_json = json.dumps(
            profile.stats.to_dict() if profile.stats else {})
        with self._write():
            self._conn.execute(
                "INSERT INTO traders(address,status,score,rejection_reason,"
                "assigned_wallet_id,discovered_at,stats_json,history_cursor,"
                "history_complete) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(address) DO UPDATE SET status=excluded.status, "
                "score=excluded.score, rejection_reason=excluded.rejection_reason, "
                "assigned_wallet_id=excluded.assigned_wallet_id, "
                "stats_json=excluded.stats_json, "
                "history_cursor=excluded.history_cursor, "
                "history_complete=excluded.history_complete",
                (profile.address, profile.status.value, profile.score,
                 profile.rejection_reason, profile.assigned_wallet_id,
                 profile.discovered_at, stats_json, history_cursor,
                 int(history_complete)))

    # -- tracking watermarks ------------------------------------------

    def update_watermarks(
            self, marks: list[tuple[str, int, str]]) -> None:
        """Narrow, batched cursor write — the ONLY thing the tracker
        persists per cycle.

        The old path re-upserted the whole trader row for every scanned
        signature, under the registry lock. That re-persisted whatever
        else the in-memory object happened to hold, which could resurrect
        a stale status, and it wrote hundreds of rows to advance a marker.
        """
        if not marks:
            return
        with self._write():
            self._conn.executemany(
                "UPDATE traders SET follow_cursor=?, follow_cursor_slot=? "
                "WHERE address=?",
                [(signature, slot, address)
                 for address, slot, signature in marks])

    def load_watermarks(self) -> dict[str, tuple[int, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT address, follow_cursor_slot, follow_cursor "
                "FROM traders").fetchall()
        return {r["address"]: (int(r["follow_cursor_slot"] or 0),
                               r["follow_cursor"] or "")
                for r in rows}

    # -- processed signatures ------------------------------------------

    def record_processed(self, entries: list[tuple[str, str, int]]) -> None:
        """Remember signatures already handed to the trading engine.

        PERSISTED, unlike the old 500-entry in-memory LRU: a restart in
        the middle of a poll window used to replay whatever had not yet
        moved the cursor, which means copying the same trade twice.
        """
        if not entries:
            return
        now = time.time()
        with self._write():
            self._conn.executemany(
                "INSERT OR IGNORE INTO processed_signatures"
                "(signature,trader,slot,processed_at) VALUES(?,?,?,?)",
                [(signature, trader, slot, now)
                 for trader, signature, slot in entries])

    def load_processed(self, trader: str,
                       min_slot: int = 0) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT signature, slot FROM processed_signatures "
                "WHERE trader=? AND slot>=?", (trader, min_slot)).fetchall()
        return {r["signature"]: int(r["slot"]) for r in rows}

    def prune_processed(self, trader: str, before_slot: int) -> int:
        """Drop entries the watermark has moved safely past.

        The watermark slot is what bounds this: anything below it is
        settled, so keeping it only grows the table.
        """
        with self._write():
            cursor = self._conn.execute(
                "DELETE FROM processed_signatures WHERE trader=? AND slot<?",
                (trader, before_slot))
            return cursor.rowcount

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
        with self._write():
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

    # -- positions ---------------------------------------------------------

    def save_position(self, p: Position) -> None:
        with self._write():
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
        with self._write():
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

    # -- receipts ----------------------------------------------------------

    def save_receipt(self, receipt: Receipt) -> None:
        with self._write():
            self._conn.execute(
                "INSERT OR REPLACE INTO receipts VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (receipt.id, receipt.signature, receipt.order_id,
                 receipt.wallet_id, receipt.side.value, receipt.mint,
                 receipt.status.value, receipt.quoted_sol,
                 receipt.quoted_tokens, receipt.actual_sol,
                 receipt.actual_tokens, receipt.fee_sol, receipt.slot,
                 receipt.block_time, receipt.detail, receipt.created_at))

    def load_receipts(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM receipts ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]
