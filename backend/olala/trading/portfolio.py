"""Portfolio manager: wallets, positions, balances, PnL.

Single source of truth for what the system owns. All mutations happen under
one lock, are persisted immediately, and are broadcast on the event bus so
the frontend stream never diverges from reality.
"""

from __future__ import annotations

import threading

from ..chain.provider import RpcProvider
from ..config import ConfigStore
from ..domain.models import (ExitReason, Fill, Position, PositionStatus,
                             TokenInfo)
from ..domain.wallet import LiveSolanaWallet, PaperSolanaWallet, Wallet
from ..events import EventBus
from ..persistence.database import Database
from ..risk.engine import WalletExposure


class PortfolioManager:
    def __init__(self, db: Database, bus: EventBus, store: ConfigStore,
                 provider: RpcProvider) -> None:
        self._db = db
        self._bus = bus
        self._store = store
        self._provider = provider
        self._lock = threading.RLock()
        self._wallets: dict[str, Wallet] = {}
        self._positions: dict[str, Position] = {}
        self._closing: set[str] = set()
        self._sol_price_usd: float = 0.0
        self._load()

    def _load(self) -> None:
        for row in self._db.load_wallets():
            if row["is_paper"]:
                wallet = PaperSolanaWallet(
                    row["id"], row["label"], row["sol_balance"])
            else:
                wallet = LiveSolanaWallet(
                    row["id"], row["label"], row["address"], self._provider,
                    armed=bool(row["armed"]))
            self._wallets[wallet.id] = wallet
        for position in self._db.load_positions():
            self._positions[position.id] = position

        paper_config = self._store.config.paper
        existing_paper = sum(
            1 for w in self._wallets.values() if w.is_paper)
        for index in range(existing_paper, paper_config.wallet_count):
            wallet = PaperSolanaWallet.create(
                f"Paper {index + 1}", paper_config.starting_sol)
            self._wallets[wallet.id] = wallet
            self._db.upsert_wallet(wallet.id, wallet.label, wallet.address,
                                   True, wallet.base_balance())

    # -- wallets -----------------------------------------------------------

    def wallets(self) -> list[Wallet]:
        with self._lock:
            return list(self._wallets.values())

    def get_wallet(self, wallet_id: str) -> Wallet | None:
        with self._lock:
            return self._wallets.get(wallet_id)

    def add_paper_wallet(self, label: str, starting_sol: float) -> Wallet:
        with self._lock:
            wallet = PaperSolanaWallet.create(label, starting_sol)
            self._wallets[wallet.id] = wallet
            self._db.upsert_wallet(wallet.id, wallet.label, wallet.address,
                                   True, wallet.base_balance())
        self._bus.publish("wallet_added", self.wallet_summary(wallet))
        return wallet

    def add_live_wallet(self, label: str, address: str) -> Wallet:
        with self._lock:
            wallet = LiveSolanaWallet(
                PaperSolanaWallet.create("", 0).id, label, address,
                self._provider, armed=False)
            self._wallets[wallet.id] = wallet
            self._db.upsert_wallet(wallet.id, label, address, False, 0.0,
                                   armed=False)
        self._bus.publish("wallet_added", wallet.to_dict())
        return wallet

    def set_wallet_armed(self, wallet_id: str, armed: bool) -> Wallet:
        """Arm or disarm one live wallet. Paper wallets have no arm state."""
        with self._lock:
            wallet = self._wallets.get(wallet_id)
            if wallet is None:
                raise KeyError(wallet_id)
            if not isinstance(wallet, LiveSolanaWallet):
                raise ValueError("paper wallets are always simulating; "
                                 "there is nothing to arm")
            wallet.set_armed(armed)
            self._db.upsert_wallet(wallet.id, wallet.label, wallet.address,
                                   False, 0.0, armed=armed)
        self._publish_wallet(wallet)
        return wallet

    # -- exposure queries --------------------------------------------------

    def set_sol_price_usd(self, price: float) -> None:
        if price > 0:
            self._sol_price_usd = price

    @property
    def sol_price_usd(self) -> float:
        return self._sol_price_usd

    def open_positions(self, wallet_id: str | None = None) -> list[Position]:
        with self._lock:
            return [p for p in self._positions.values()
                    if p.status is PositionStatus.OPEN
                    and (wallet_id is None or p.wallet_id == wallet_id)]

    def all_positions(self) -> list[Position]:
        with self._lock:
            return list(self._positions.values())

    def find_open(self, wallet_id: str, trader: str,
                  mint: str) -> Position | None:
        with self._lock:
            for position in self._positions.values():
                if (position.status is PositionStatus.OPEN
                        and position.wallet_id == wallet_id
                        and position.trader == trader
                        and position.mint == mint):
                    return position
        return None

    def exposure(self, wallet_id: str, mint: str) -> WalletExposure:
        with self._lock:
            wallet = self._wallets[wallet_id]
            open_here = self.open_positions(wallet_id)
            cash = wallet.base_balance()
            equity = cash + sum(p.market_value_sol for p in open_here)
            invested = sum(p.sol_invested for p in open_here
                           if p.mint == mint)
            fleet_invested = sum(p.sol_invested
                                 for p in self.open_positions()
                                 if p.mint == mint)
            live_holders = {
                p.wallet_id for p in self.open_positions()
                if p.mint == mint
                and (holder := self._wallets.get(p.wallet_id)) is not None
                and not holder.is_paper}
            return WalletExposure(
                wallet_id=wallet_id, cash_sol=cash, equity_sol=equity,
                open_positions=len(open_here),
                invested_in_mint_sol=invested,
                fleet_invested_in_mint_sol=fleet_invested,
                wallet_is_paper=wallet.is_paper,
                live_wallets_holding_mint=len(live_holders))

    # -- mutations ---------------------------------------------------------

    def apply_buy(self, wallet: Wallet, trader: str, token: TokenInfo,
                  fill: Fill) -> Position:
        with self._lock:
            if isinstance(wallet, PaperSolanaWallet):
                wallet.debit(fill.sol_amount)
                self._db.upsert_wallet(wallet.id, wallet.label,
                                       wallet.address, True,
                                       wallet.base_balance())
            position = self.find_open(wallet.id, trader, token.mint)
            if position:
                total_qty = position.quantity + fill.quantity
                position.entry_price_sol = (
                    (position.entry_price_sol * position.quantity
                     + fill.price_sol * fill.quantity) / total_qty)
                position.quantity = total_qty
                position.sol_invested += fill.sol_amount
                position.last_price_sol = fill.price_sol
                event = "position_resized"
            else:
                position = Position.open_new(
                    wallet.id, trader, token.mint, token.symbol,
                    fill.quantity, fill.price_sol, fill.sol_amount)
                self._positions[position.id] = position
                event = "position_opened"
            self._db.save_position(position)
            self._db.save_fill(wallet.id, fill)
        self._bus.publish(event, position.to_dict())
        self._publish_wallet(wallet)
        return position

    def begin_close(self, position: Position) -> bool:
        """Atomically claim a position for closing.

        Panic stops, trader exits, and manual closes run on different
        threads; exactly one caller wins. The claim is released by
        ``apply_close`` or ``abort_close``.
        """
        with self._lock:
            if (position.status is not PositionStatus.OPEN
                    or position.id in self._closing):
                return False
            self._closing.add(position.id)
            return True

    def abort_close(self, position_id: str) -> None:
        with self._lock:
            self._closing.discard(position_id)

    def apply_close(self, wallet: Wallet, position: Position, fill: Fill,
                    reason: ExitReason) -> Position:
        import time
        with self._lock:
            self._closing.discard(position.id)
            if position.status is not PositionStatus.OPEN:
                # Already closed by a concurrent caller: never credit twice.
                return position
            position.status = PositionStatus.CLOSED
            position.closed_at = time.time()
            position.exit_reason = reason.value
            position.realized_pnl_sol = fill.sol_amount - position.sol_invested
            position.last_price_sol = fill.price_sol
            if isinstance(wallet, PaperSolanaWallet):
                wallet.credit(fill.sol_amount)
                self._db.upsert_wallet(wallet.id, wallet.label,
                                       wallet.address, True,
                                       wallet.base_balance())
            self._db.save_position(position)
            self._db.save_fill(wallet.id, fill)
        self._bus.publish("position_closed", position.to_dict())
        self._publish_wallet(wallet)
        return position

    def mark_price(self, mint: str, price_sol: float) -> list[Position]:
        """Update open positions in this mint; returns the ones updated."""
        updated = []
        with self._lock:
            for position in self._positions.values():
                if (position.status is PositionStatus.OPEN
                        and position.mint == mint):
                    position.last_price_sol = price_sol
                    position.peak_price_sol = max(
                        position.peak_price_sol, price_sol)
                    self._db.save_position(position)
                    updated.append(position)
        return updated

    def update_stop(self, position: Position, stop_price: float) -> None:
        with self._lock:
            position.stop_price_sol = stop_price
            self._db.save_position(position)

    def _publish_wallet(self, wallet: Wallet) -> None:
        self._bus.publish("wallet_update", self.wallet_summary(wallet))

    # -- snapshots ---------------------------------------------------------

    def wallet_summary(self, wallet: Wallet) -> dict:
        open_here = self.open_positions(wallet.id)
        cash = wallet.base_balance()
        market_value = sum(p.market_value_sol for p in open_here)
        summary = wallet.to_dict()
        summary.update({
            "cash_sol": round(cash, 6),
            "positions_value_sol": round(market_value, 6),
            "equity_sol": round(cash + market_value, 6),
            "open_positions": len(open_here),
            "reserve_sol": round(
                (cash + market_value)
                * self._store.config.risk.reserve_fraction, 6),
        })
        return summary

    def snapshot(self) -> dict:
        with self._lock:
            wallets = [self.wallet_summary(w) for w in self._wallets.values()]
            positions = [p.to_dict() for p in self._positions.values()]
        return {
            "wallets": wallets,
            "positions": positions,
            "sol_price_usd": self._sol_price_usd,
        }
