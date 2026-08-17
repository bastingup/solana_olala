"""Core domain model: chain-agnostic value objects shared across the system."""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Chain(str, Enum):
    SOLANA = "solana"


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class TraderStatus(str, Enum):
    CANDIDATE = "candidate"
    FOLLOWED = "followed"
    REJECTED = "rejected"
    RETIRED = "retired"


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class ExitReason(str, Enum):
    TRADER_EXIT = "trader_exit"
    PANIC_STOP = "panic_stop"
    MANUAL = "manual"


class ReceiptStatus(str, Enum):
    CONFIRMED = "confirmed"
    FAILED = "failed"        # landed on chain, but the program errored
    TIMEOUT = "timeout"      # never landed before the blockhash expired


def _now() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class TokenInfo:
    """Market snapshot for a token, sourced from the market-data provider."""

    mint: str
    symbol: str
    name: str
    price_usd: float
    price_sol: float
    liquidity_usd: float
    market_cap_usd: float
    pair_address: str
    dex: str
    pair_created_at: float
    fetched_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ObservedTrade:
    """A swap reconstructed from an on-chain transaction of a trader."""

    trader: str
    signature: str
    side: TradeSide
    mint: str
    token_amount: float
    sol_amount: float
    price_sol: float
    block_time: float

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        return d


@dataclass
class TraderStats:
    """Aggregated performance metrics reconstructed from chain history."""

    address: str
    first_trade_at: float = 0.0
    last_trade_at: float = 0.0
    total_trades: int = 0
    closed_round_trips: int = 0
    wins: int = 0
    realized_pnl_sol: float = 0.0
    distinct_tokens: int = 0
    median_token_liquidity_usd: float = 0.0
    median_hold_minutes: float = 0.0
    # Unsold in-window inventory older than the staleness threshold:
    # the "never realize a loss" tell. Each stale bag counts as a loss
    # in the adjusted win rate.
    open_bags: int = 0
    bag_cost_sol: float = 0.0
    # Per-trade Sharpe: mean(return per SOL deployed) / stdev.
    sharpe: float = 0.0

    @property
    def win_rate(self) -> float:
        if self.closed_round_trips == 0:
            return 0.0
        return self.wins / self.closed_round_trips

    @property
    def adjusted_win_rate(self) -> float:
        """Win rate with every stale bag counted as a realized loss."""
        denominator = self.closed_round_trips + self.open_bags
        if denominator == 0:
            return 0.0
        return self.wins / denominator

    @property
    def history_days(self) -> float:
        if not self.first_trade_at:
            return 0.0
        return (self.last_trade_at - self.first_trade_at) / 86_400.0

    @property
    def trades_per_day(self) -> float:
        days = self.history_days
        if days <= 0:
            return float(self.total_trades)
        return self.total_trades / days

    @property
    def inactive_hours(self) -> float:
        if not self.last_trade_at:
            return float("inf")
        return (_now() - self.last_trade_at) / 3_600.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["win_rate"] = round(self.win_rate, 4)
        d["adjusted_win_rate"] = round(self.adjusted_win_rate, 4)
        d["history_days"] = round(self.history_days, 1)
        d["trades_per_day"] = round(self.trades_per_day, 1)
        # inactive_hours is infinite when no trade was ever observed.
        # Infinity is valid for the admission filters but not for JSON:
        # browsers reject the token and drop the whole message, so the
        # serialized form must be null instead.
        hours = self.inactive_hours
        d["inactive_hours"] = round(hours, 1) if math.isfinite(hours) else None
        return d


@dataclass
class TraderProfile:
    address: str
    chain: Chain = Chain.SOLANA
    status: TraderStatus = TraderStatus.CANDIDATE
    stats: TraderStats | None = None
    score: float = 0.0
    rejection_reason: str = ""
    assigned_wallet_id: str = ""
    discovered_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "chain": self.chain.value,
            "status": self.status.value,
            "stats": self.stats.to_dict() if self.stats else None,
            "score": round(self.score, 4),
            "rejection_reason": self.rejection_reason,
            "assigned_wallet_id": self.assigned_wallet_id,
            "discovered_at": self.discovered_at,
        }


@dataclass
class CopySignal:
    """A followed trader's observed trade, ready for risk evaluation."""

    trader: str
    side: TradeSide
    mint: str
    trader_sol_amount: float
    observed: ObservedTrade

    def to_dict(self) -> dict[str, Any]:
        return {
            "trader": self.trader,
            "side": self.side.value,
            "mint": self.mint,
            "trader_sol_amount": self.trader_sol_amount,
            "signature": self.observed.signature,
        }


@dataclass
class RiskVerdict:
    approved: bool
    reason: str
    size_sol: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Position:
    id: str
    wallet_id: str
    trader: str
    mint: str
    symbol: str
    quantity: float
    entry_price_sol: float
    sol_invested: float
    opened_at: float
    status: PositionStatus = PositionStatus.OPEN
    peak_price_sol: float = 0.0
    stop_price_sol: float = 0.0
    last_price_sol: float = 0.0
    closed_at: float = 0.0
    exit_reason: str = ""
    realized_pnl_sol: float = 0.0

    @staticmethod
    def open_new(wallet_id: str, trader: str, mint: str, symbol: str,
                 quantity: float, price_sol: float, sol_invested: float) -> "Position":
        return Position(
            id=new_id(), wallet_id=wallet_id, trader=trader, mint=mint,
            symbol=symbol, quantity=quantity, entry_price_sol=price_sol,
            sol_invested=sol_invested, opened_at=_now(),
            peak_price_sol=price_sol, last_price_sol=price_sol)

    @property
    def market_value_sol(self) -> float:
        return self.quantity * self.last_price_sol

    @property
    def unrealized_pnl_sol(self) -> float:
        if self.status is not PositionStatus.OPEN:
            return 0.0
        return self.market_value_sol - self.sol_invested

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["market_value_sol"] = round(self.market_value_sol, 6)
        d["unrealized_pnl_sol"] = round(self.unrealized_pnl_sol, 6)
        return d


@dataclass
class Fill:
    """Result of an executed (paper or live) order."""

    order_id: str
    side: TradeSide
    mint: str
    quantity: float
    price_sol: float
    sol_amount: float
    fee_sol: float
    executed_at: float = field(default_factory=_now)
    signature: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        return d


@dataclass
class Receipt:
    """On-chain outcome of one live order attempt — the audit trail.

    Every live submission produces exactly one receipt, success or not:
    what was quoted, what actually moved on chain (reconstructed from the
    landed transaction, never trusted from the quote), and how it ended.
    """

    signature: str
    order_id: str
    wallet_id: str
    side: TradeSide
    mint: str
    status: ReceiptStatus
    quoted_sol: float
    quoted_tokens: float
    actual_sol: float = 0.0
    actual_tokens: float = 0.0
    fee_sol: float = 0.0
    slot: int = 0
    block_time: float = 0.0
    detail: str = ""
    id: str = field(default_factory=new_id)
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["status"] = self.status.value
        return d
