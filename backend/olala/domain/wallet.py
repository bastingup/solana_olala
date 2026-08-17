"""Wallet abstraction.

``Wallet`` is chain-agnostic; ``SolanaWallet`` is the Solana implementation.
Future chains (Ethereum, Bitcoin) subclass ``Wallet`` — nothing outside this
module may assume Solana specifics.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .models import Chain, new_id


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
    def base_balance(self) -> float:
        """Balance of the chain's base asset (SOL, ETH, ...)."""

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

    def base_balance(self) -> float:
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

    @property
    def is_paper(self) -> bool:
        return False

    @property
    def armed(self) -> bool:
        return self._armed

    def set_armed(self, armed: bool) -> None:
        self._armed = armed

    def base_balance(self) -> float:
        return self._balance_provider.get_sol_balance(self.address)
