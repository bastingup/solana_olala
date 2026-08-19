"""Wallet abstraction.

``Wallet`` is chain-agnostic; ``SolanaWallet`` is the Solana implementation.
Future chains (Ethereum, Bitcoin) subclass ``Wallet`` — nothing outside this
module may assume Solana specifics.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

from .models import Chain, new_id

logger = logging.getLogger(__name__)

# A live balance older than this is refreshed on read. Short enough that
# sizing decisions are current, long enough that a burst of reads costs
# one RPC call rather than dozens.
BALANCE_TTL_SEC = 10.0


class Wallet(ABC):
    """A wallet the system trades from, on any chain."""

    def __init__(self, wallet_id: str, label: str, address: str) -> None:
        self.id = wallet_id
        self.label = label
        self.address = address

    @property
    @abstractmethod
    def chain(self) -> Chain: ...

    @property
    @abstractmethod
    def is_paper(self) -> bool: ...

    @property
    def armed(self) -> bool:
        """Whether this wallet may execute trades right now.

        Paper wallets always simulate; only live wallets carry a real
        arm/disarm state.
        """
        return True

    @abstractmethod
    def base_balance(self, max_age_sec: float = BALANCE_TTL_SEC) -> float:
        """Balance of the chain's base asset (SOL, ETH, ...).

        ``max_age_sec`` caps how stale a cached value may be; pass 0 when
        the number is about to decide how much money an order spends.
        """

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "address": self.address,
            "chain": self.chain.value,
            "is_paper": self.is_paper,
            "armed": self.armed,
            "base_balance": round(self.base_balance(), 6),
        }


class SolanaWallet(Wallet):
    @property
    def chain(self) -> Chain:
        return Chain.SOLANA


class PaperSolanaWallet(SolanaWallet):
    """Simulated wallet: base balance is managed locally by the portfolio."""

    def __init__(self, wallet_id: str, label: str, starting_sol: float) -> None:
        super().__init__(wallet_id, label, address=f"paper-{wallet_id}")
        self._sol = starting_sol

    @property
    def is_paper(self) -> bool:
        return True

    def base_balance(self, max_age_sec: float = BALANCE_TTL_SEC) -> float:
        # A paper balance is authoritative in memory; nothing to refresh.
        return self._sol

    def credit(self, sol: float) -> None:
        self._sol += sol

    def debit(self, sol: float) -> None:
        if sol > self._sol + 1e-9:
            raise ValueError("insufficient paper balance")
        self._sol -= sol

    @staticmethod
    def create(label: str, starting_sol: float) -> "PaperSolanaWallet":
        return PaperSolanaWallet(new_id(), label, starting_sol)


class LiveSolanaWallet(SolanaWallet):
    """A real wallet whose key lives in the encrypted keystore.

    The wallet object itself never holds key material; it asks the keystore
    for a signer only at execution time, and only while armed.
    """

    def __init__(self, wallet_id: str, label: str, address: str,
                 balance_provider, armed: bool = False) -> None:
        super().__init__(wallet_id, label, address)
        self._balance_provider = balance_provider
        self._armed = armed
        self._balance = 0.0
        self._balance_at = 0.0
        self._balance_lock = threading.Lock()

    @property
    def is_paper(self) -> bool:
        return False

    @property
    def armed(self) -> bool:
        return self._armed

    def set_armed(self, armed: bool) -> None:
        self._armed = armed

    def base_balance(self, max_age_sec: float = BALANCE_TTL_SEC) -> float:
        """SOL balance, from a short-lived cache.

        This used to issue an RPC call on every read — including from
        ``PortfolioManager.snapshot()``, which held the portfolio lock
        while it waited. A slow node therefore stalled every buy, sell
        and panic stop in the system. Reads are now cheap; a background
        refresh keeps the value current, and callers that genuinely need
        certainty pass ``max_age_sec=0``.
        """
        with self._balance_lock:
            age = time.time() - self._balance_at
            if self._balance_at and age <= max_age_sec:
                return self._balance
        return self.refresh_balance()

    def refresh_balance(self) -> float:
        """Fetch the balance from chain. Never call this under a lock."""
        try:
            balance = self._balance_provider.get_sol_balance(self.address)
        except Exception:                                   # noqa: BLE001
            # A failed read must not erase a known balance, and must not
            # propagate into a snapshot the UI depends on.
            logger.warning("balance read failed for %s; keeping the last "
                           "known value", self.address[:8])
            with self._balance_lock:
                return self._balance
        with self._balance_lock:
            self._balance = balance
            self._balance_at = time.time()
            return balance
