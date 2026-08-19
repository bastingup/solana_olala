"""Per-scan RPC allowance.

Discovery is the greedy consumer in this system: a deep scan can spend
thousands of calls if nothing stops it. ``RpcBudget`` is the ceiling for
one scan cycle, handed down through every discovery source so no single
source can drain the cycle out from under the others.

It lives in its own module because both ``scanner`` (which creates it)
and ``onchain`` (which spends it) need the type, and having the spender
import from the creator made those two modules circular.
"""

from __future__ import annotations


class RpcBudget:
    def __init__(self, calls: int) -> None:
        self._remaining = calls

    def take(self, count: int = 1) -> bool:
        if self._remaining < count:
            return False
        self._remaining -= count
        return True

    @property
    def exhausted(self) -> bool:
        return self._remaining <= 0

    @property
    def remaining(self) -> int:
        return max(self._remaining, 0)
