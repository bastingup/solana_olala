"""Solana RPC access — the shape of every call, independent of who serves it.

``RpcProvider`` defines what a caller may ask for and how each answer is
unwrapped. It does not know about endpoints, credentials, rate limits or
retries: a subclass supplies :meth:`_call`, and everything else is shared.

``RoutedProvider`` is the implementation the application uses. It holds an
:class:`~olala.chain.router.RpcRouter` and maps each method to a routing
POLICY — ``metadata`` for token lookups, ``broadcast`` for sending,
``confirm`` for status, ``history`` for signatures and transactions. So a
call fails over across sources automatically, and reordering that
preference is a configuration edit.

Confirmation is the one sequence that must NOT fail over: a node that
never saw our transaction reports ``null``, which is indistinguishable
from "it never landed" unless you know who broadcast it. Use
:meth:`RoutedProvider.broadcast_session` for that, which pins both calls
to one source.
"""

from __future__ import annotations

import contextlib
import logging
from abc import ABC, abstractmethod
from typing import Any, Iterator

from ..config import AppConfig
from ..constants import LAMPORTS_PER_SOL
# Re-exported through this module: 13 call sites still import ChainError
# from here, and the taxonomy subclasses it so every one keeps working.
from .errors import (ChainError, RateLimited, SourceError, SourceIncomplete,
                     SourceRateLimited, SourceRejected, SourceUnavailable,
                     SourceUnsupported)
from .router import NoSourceAvailable, RpcRouter
from .sources.json_rpc import redact

__all__ = [
    "RpcProvider", "RoutedProvider", "BroadcastSession", "build_provider",
    "signatures_params", "account_owner", "redact", "METHOD_POLICY",
    "RpcRouter", "NoSourceAvailable",
    "ChainError", "RateLimited", "SourceError", "SourceIncomplete",
    "SourceRateLimited", "SourceRejected", "SourceUnavailable",
    "SourceUnsupported",
]

logger = logging.getLogger(__name__)

# Which routing policy serves which method. Anything unlisted is treated
# as history, the broadest and least privileged chain.
METHOD_POLICY = {
    "getSignaturesForAddress": "history",
    "getTransaction": "history",
    "getBalance": "metadata",
    "getAccountInfo": "metadata",
    "getTokenSupply": "metadata",
    "getTokenLargestAccounts": "metadata",
    "getMultipleAccounts": "metadata",
    "sendTransaction": "broadcast",
    "getSignatureStatuses": "confirm",
}


class RpcProvider(ABC):
    """Gateway to Solana JSON-RPC, independent of the concrete endpoint."""

    @abstractmethod
    def _call(self, method: str, params: list[Any]) -> Any:
        """Issue one call and return its ``result``."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def ws_endpoint(self) -> str:
        """WebSocket endpoint for subscription methods."""

    # -- public surface ----------------------------------------------------

    def get_signatures(self, address: str, limit: int = 100,
                       before: str | None = None,
                       failover_on_empty: bool = False) -> list[dict[str, Any]]:
        # ``failover_on_empty`` is honoured only by a multi-source
        # provider (see :class:`RoutedProvider`). A single-source provider
        # has nothing to fail over to, so it ignores the flag.
        return self._call("getSignaturesForAddress",
                          [address, signatures_params(limit, before)]) or []

    def get_transaction(self, signature: str,
                        failover_on_null: bool = False
                        ) -> dict[str, Any] | None:
        # ``failover_on_null`` is honoured only by a multi-source provider.
        return self._call("getTransaction", [signature, TRANSACTION_OPTIONS])

    def get_sol_balance(self, address: str) -> float:
        result = self._call("getBalance", [address]) or {}
        return (result.get("value") or 0) / LAMPORTS_PER_SOL

    def get_account_info(self, pubkey: str) -> dict[str, Any] | None:
        result = self._call("getAccountInfo",
                            [pubkey, {"encoding": "jsonParsed"}]) or {}
        return result.get("value")

    def get_token_supply_and_decimals(self, mint: str) -> tuple[float, int]:
        """Supply and decimals from ONE call — they arrive together.

        Asking twice was two round trips for one response body.
        """
        result = self._call("getTokenSupply", [mint]) or {}
        value = result.get("value") or {}
        return (float(value.get("uiAmount") or 0.0),
                int(value.get("decimals") or 0))

    def get_token_supply(self, mint: str) -> float:
        return self.get_token_supply_and_decimals(mint)[0]

    def get_token_decimals(self, mint: str) -> int:
        return self.get_token_supply_and_decimals(mint)[1]

    def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        result = self._call("getTokenLargestAccounts", [mint]) or {}
        return result.get("value") or []

    def get_token_account_owners(
            self, token_accounts: list[str]) -> list[str | None]:
        """Owner wallet of each SPL token account, in order.

        ``getMultipleAccounts`` accepts at most 100 keys per call, so
        longer lists are paged rather than silently truncated — the old
        behaviour dropped every holder past the hundredth.
        """
        owners: list[str | None] = []
        for start in range(0, len(token_accounts), MAX_MULTIPLE_ACCOUNTS):
            page = token_accounts[start:start + MAX_MULTIPLE_ACCOUNTS]
            result = self._call("getMultipleAccounts",
                                [page, {"encoding": "jsonParsed"}]) or {}
            values = result.get("value") or []
            for index in range(len(page)):
                value = values[index] if index < len(values) else None
                owners.append(account_owner(value))
        return owners

    def send_transaction(self, signed_tx_base64: str) -> str:
        return self._call("sendTransaction",
                          [signed_tx_base64, SEND_OPTIONS])

    def get_signature_status(self, signature: str) -> dict[str, Any] | None:
        """Confirmation status of one signature, or None if unknown.

        ``searchTransactionHistory`` looks past the node's recent-status
        cache, so a transaction that landed minutes ago still reports.

        **None means UNKNOWN, not "never landed"** — unless the source
        answering is the one that broadcast it. See
        :meth:`RoutedProvider.broadcast_session`.
        """
        result = self._call("getSignatureStatuses",
                            [[signature], {"searchTransactionHistory": True}]
                            ) or {}
        values = result.get("value") or [None]
        return values[0]

    @contextlib.contextmanager
    def broadcast_session(self) -> Iterator[Any]:
        """A channel that sends and confirms through ONE source.

        The base implementation yields ``self``, because a provider
        backed by a single source is already pinned by construction.
        :class:`RoutedProvider` overrides it: with several sources, a
        confirmation could otherwise be answered by a node that never saw
        the transaction, whose ``null`` is not evidence of anything.
        """
        yield self


MAX_MULTIPLE_ACCOUNTS = 100
# `confirmed`, not the node default of `finalized`. The push stream
# tells us about a transaction at CONFIRMED, roughly a second after the
# block; finalization takes ~13s more. Asking a finalized-only question
# about a transaction we heard of at confirmed gets `null` back — and a
# null read used to be recorded as "handled", so the trade was skipped
# permanently. Confirmed is also what our watermark's reorg margin is
# sized for.
TRANSACTION_OPTIONS = {"encoding": "jsonParsed",
                       "maxSupportedTransactionVersion": 0,
                       "commitment": "confirmed"}
SEND_OPTIONS = {"encoding": "base64", "skipPreflight": False}


def signatures_params(limit: int, before: str | None = None,
                      until: str | None = None) -> dict[str, Any]:
    options: dict[str, Any] = {"limit": max(1, min(limit, 1000))}
    if before:
        options["before"] = before
    if until:
        options["until"] = until
    return options


def account_owner(value: Any) -> str | None:
    """Owner pubkey out of a jsonParsed SPL token account, if present."""
    info = ((((value or {}).get("data") or {}).get("parsed")
             or {}).get("info") or {})
    return info.get("owner")


class RoutedProvider(RpcProvider):
    """The provider the application runs on: every call is routed."""

    def __init__(self, router: RpcRouter) -> None:
        self._router = router

    @property
    def router(self) -> RpcRouter:
        return self._router

    @property
    def name(self) -> str:
        names = self._router.policy("history")
        return names[0] if names else "no-source"

    def ws_endpoint(self) -> str:
        for name in self._router.available("stream"):
            endpoint = self._router.sources[name].ws_endpoint()
            if endpoint:
                return endpoint
        raise NoSourceAvailable("no source offers a WebSocket endpoint")

    def _call(self, method: str, params: list[Any]) -> Any:
        return self._router.call(METHOD_POLICY.get(method, "history"),
                                 method, params)

    def get_transaction(self, signature: str,
                        failover_on_null: bool = False
                        ) -> dict[str, Any] | None:
        """Fetch one transaction, optionally escalating a ``null``.

        publicnode keeps only ~2 days of signature history (MEASURED),
        so an aged signature reads back ``null`` there while Helius and
        mainnet-beta still serve it. Routing never failed over on ``null``
        because ``null`` is not an error — a shallow node answered
        correctly, it simply could not see that far back. On the COPY
        path a ``null`` is the difference between reconstructing a trade
        and losing it, so the tracker asks with ``failover_on_null=True``
        and a deeper source is tried before the read is called unreadable.

        The default stays ``False``: discovery fetches thousands of
        transactions per candidate and hits many legitimate nulls
        (pruned, unreconstructable), and escalating every one of those to
        the metered source would burn the credit budget for no gain.
        """
        if not failover_on_null:
            return self._call("getTransaction", [signature, TRANSACTION_OPTIONS])
        return self._router.call_accept(
            "history", "getTransaction", [signature, TRANSACTION_OPTIONS],
            accept=lambda result: result is not None)

    def get_signatures(self, address: str, limit: int = 100,
                       before: str | None = None,
                       failover_on_empty: bool = False) -> list[dict[str, Any]]:
        """List an address's signatures, optionally escalating an EMPTY page.

        A wallet whose newest transaction predates publicnode's ~2-day
        retention returns zero signatures there — indistinguishable, to a
        single source, from a wallet that simply has not traded. When the
        caller KNOWS the wallet has history (a leaderboard-seated trader
        that cannot be armed), ``failover_on_empty=True`` escalates to a
        deeper source rather than leaving the wallet silently invisible.

        The default stays ``False`` so the common poll — where an empty
        page genuinely means "nothing new" — never fans out across
        sources or spends metered credits.
        """
        params = signatures_params(limit, before)
        if not failover_on_empty:
            return self._call("getSignaturesForAddress", [address, params]) or []
        return self._router.call_accept(
            "history", "getSignaturesForAddress", [address, params],
            accept=lambda result: bool(result)) or []

    @contextlib.contextmanager
    def broadcast_session(self) -> Iterator["BroadcastSession"]:
        """Send and confirm through ONE source.

        The alternative — broadcasting on one node and confirming on
        another — makes ``null`` ambiguous: the second node may simply
        never have seen the transaction. Pinning turns that ``null`` back
        into real evidence.
        """
        with self._router.session("broadcast") as pinned:
            yield BroadcastSession(pinned)


class BroadcastSession:
    """Send a transaction and confirm it on the same node."""

    def __init__(self, pinned) -> None:
        self._pinned = pinned

    @property
    def source_name(self) -> str:
        return self._pinned.source_name

    def send_transaction(self, signed_tx_base64: str) -> str:
        return self._pinned.call("sendTransaction",
                                 [signed_tx_base64, SEND_OPTIONS])

    def get_signature_status(self, signature: str) -> dict[str, Any] | None:
        result = self._pinned.call(
            "getSignatureStatuses",
            [[signature], {"searchTransactionHistory": True}]) or {}
        values = result.get("value") or [None]
        return values[0]


def build_provider(config: AppConfig) -> RoutedProvider:
    """The application's provider: a router over every enabled source."""
    return RoutedProvider(RpcRouter(config))
