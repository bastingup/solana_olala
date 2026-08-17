"""Solana RPC access.

``RpcProvider`` is the single gateway to Solana JSON-RPC. The default
``PublicRpcProvider`` rotates across keyless public endpoints under a strict
rate limit; ``HeliusRpcProvider`` upgrades throughput automatically when a
(free-tier) Helius API key is present in configuration. Callers never know
which one they are talking to.
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

import requests

from ..config import ChainConfig
from .rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000


class ChainError(RuntimeError):
    """A chain request failed after retries."""


class RateLimited(ChainError):
    """The endpoint rejected the request with 429."""


def redact(endpoint: str) -> str:
    """Endpoints carry API keys — never let one reach a log file."""
    scrubbed = re.sub(r"(api[-_]?key=)[^&\s]+", r"\1<redacted>", endpoint)
    return scrubbed.split("?")[0] if scrubbed != endpoint else scrubbed


class RpcProvider(ABC):
    """Gateway to Solana JSON-RPC, independent of the concrete endpoint."""

    def __init__(self, config: ChainConfig) -> None:
        self._config = config
        self._session = requests.Session()
        self._limiter = RateLimiter(self._requests_per_second())
        self._id_counter = itertools.count(1)
        self._lock = threading.Lock()
        self._throttle_events = 0
        self._last_throttle_log = 0.0

    @abstractmethod
    def _requests_per_second(self) -> float: ...

    @abstractmethod
    def _next_endpoint(self) -> str: ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def ws_endpoint(self) -> str:
        """WebSocket endpoint for subscription methods."""

    # -- JSON-RPC plumbing -------------------------------------------------

    def _call(self, method: str, params: list[Any], retries: int = 4) -> Any:
        last_error: Exception | None = None
        for attempt in range(retries):
            self._limiter.acquire()
            endpoint = self._next_endpoint()
            payload = {"jsonrpc": "2.0", "id": next(self._id_counter),
                       "method": method, "params": params}
            try:
                response = self._session.post(
                    endpoint, json=payload,
                    timeout=self._config.request_timeout_sec)
                if response.status_code == 429:
                    # Slow the WHOLE bucket down, not just this call:
                    # retrying into a throttled endpoint is what turns one
                    # 429 into a storm of them.
                    retry_after = self._retry_after(response)
                    cooldown = self._limiter.penalize(retry_after)
                    self._note_throttle(cooldown)
                    raise RateLimited("endpoint is rate limiting us")
                response.raise_for_status()
                body = response.json()
                if "error" in body:
                    raise ChainError(f"{method}: {body['error']}")
                return body.get("result")
            except (requests.RequestException, ChainError, ValueError) as exc:
                last_error = exc
                if not isinstance(exc, RateLimited):
                    logger.warning("rpc %s failed on %s (attempt %d): %s",
                                   method, redact(endpoint), attempt + 1, exc)
                if attempt == retries - 1:
                    break  # No point sleeping before giving up.
                if not isinstance(exc, RateLimited):
                    # The limiter already paused us for rate limits; only
                    # other failures need their own backoff.
                    time.sleep(min(2.0 ** attempt, 15.0))
        raise ChainError(f"{method} failed after {retries} attempts: "
                         f"{last_error}")

    @staticmethod
    def _retry_after(response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def _note_throttle(self, cooldown: float) -> None:
        """One line per throttling episode, not per rejected call."""
        now = time.monotonic()
        with self._lock:
            self._throttle_events += 1
            if now - self._last_throttle_log < 30.0:
                return
            self._last_throttle_log = now
            events = self._throttle_events
        logger.warning(
            "%s is rate limiting us — backing off %.1fs, issue rate now "
            "%.2f/s (%d rejections so far). Discovery will simply run "
            "slower; nothing is lost.",
            self.name, cooldown, self._limiter.current_rate, events)

    # -- Public surface ----------------------------------------------------

    def get_signatures(self, address: str, limit: int = 100,
                       before: str | None = None) -> list[dict[str, Any]]:
        options: dict[str, Any] = {"limit": min(limit, 1000)}
        if before:
            options["before"] = before
        return self._call("getSignaturesForAddress", [address, options]) or []

    def get_transaction(self, signature: str) -> dict[str, Any] | None:
        return self._call("getTransaction", [signature, {
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
        }])

    def get_sol_balance(self, address: str) -> float:
        result = self._call("getBalance", [address]) or {}
        return (result.get("value") or 0) / LAMPORTS_PER_SOL

    def get_account_info(self, pubkey: str) -> dict[str, Any] | None:
        result = self._call("getAccountInfo",
                            [pubkey, {"encoding": "jsonParsed"}]) or {}
        return result.get("value")

    def get_token_supply(self, mint: str) -> float:
        result = self._call("getTokenSupply", [mint]) or {}
        value = result.get("value") or {}
        return float(value.get("uiAmount") or 0.0)

    def get_token_decimals(self, mint: str) -> int:
        result = self._call("getTokenSupply", [mint]) or {}
        value = result.get("value") or {}
        return int(value.get("decimals") or 0)

    def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        result = self._call("getTokenLargestAccounts", [mint]) or {}
        return result.get("value") or []

    def get_token_account_owners(
            self, token_accounts: list[str]) -> list[str | None]:
        """Owner wallet of each SPL token account, in order."""
        if not token_accounts:
            return []
        result = self._call("getMultipleAccounts", [
            token_accounts[:100], {"encoding": "jsonParsed"}]) or {}
        owners: list[str | None] = []
        for value in result.get("value") or []:
            info = ((((value or {}).get("data") or {}).get("parsed")
                     or {}).get("info") or {})
            owners.append(info.get("owner"))
        return owners

    def send_transaction(self, signed_tx_base64: str) -> str:
        return self._call("sendTransaction", [signed_tx_base64, {
            "encoding": "base64", "skipPreflight": False,
        }])

    def get_signature_status(self, signature: str) -> dict[str, Any] | None:
        """Confirmation status of one signature, or None if unknown.

        ``searchTransactionHistory`` looks past the node's recent-status
        cache, so a transaction that landed minutes ago still reports.
        """
        result = self._call("getSignatureStatuses", [
            [signature], {"searchTransactionHistory": True}]) or {}
        values = result.get("value") or [None]
        return values[0]


class PublicRpcProvider(RpcProvider):
    """Keyless public endpoints, rotated round-robin under a shared budget."""

    def __init__(self, config: ChainConfig) -> None:
        self._endpoints = itertools.cycle(config.rpc_endpoints)
        self._endpoint_lock = threading.Lock()
        super().__init__(config)

    @property
    def name(self) -> str:
        return "public-rpc"

    def _requests_per_second(self) -> float:
        return self._config.requests_per_second

    def _next_endpoint(self) -> str:
        with self._endpoint_lock:
            return next(self._endpoints)

    def ws_endpoint(self) -> str:
        primary = self._config.rpc_endpoints[0]
        return primary.replace("https://", "wss://", 1)


class HeliusRpcProvider(RpcProvider):
    """Keyed Helius endpoint: same interface, considerably higher budget."""

    @property
    def name(self) -> str:
        return "helius"

    def _requests_per_second(self) -> float:
        return max(self._config.requests_per_second, 8.0)

    def _next_endpoint(self) -> str:
        return ("https://mainnet.helius-rpc.com/"
                f"?api-key={self._config.helius_api_key}")

    def ws_endpoint(self) -> str:
        return ("wss://mainnet.helius-rpc.com/"
                f"?api-key={self._config.helius_api_key}")


def build_provider(config: ChainConfig) -> RpcProvider:
    if config.helius_api_key:
        return HeliusRpcProvider(config)
    return PublicRpcProvider(config)
