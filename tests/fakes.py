"""Offline stand-ins for chain-facing services."""

from __future__ import annotations

import contextlib

from typing import Any

from olala.domain.models import TokenInfo


class FakeProvider:
    """Scriptable RpcProvider replacement. No network, ever."""

    name = "fake"

    def __init__(self) -> None:
        self.signatures: dict[str, list[dict[str, Any]]] = {}
        self.transactions: dict[str, dict[str, Any]] = {}
        self.account_info: dict[str, dict[str, Any]] = {}
        self.token_supply: dict[str, float] = {}
        self.token_decimals: dict[str, int] = {}
        self.largest_accounts: dict[str, list[dict[str, Any]]] = {}
        self.account_owners: dict[str, str] = {}
        self.sol_balances: dict[str, float] = {}
        self.sent_transactions: list[str] = []
        self.fail_transactions: set[str] = set()
        self.signature_status: dict[str, dict[str, Any]] = {}
        # Per-address call tally, so tests can prove no RPC was spent.
        self.signature_calls: dict[str, int] = {}

    @contextlib.contextmanager
    def broadcast_session(self):
        """A single-source provider is already pinned, so this yields self
        — the same contract RpcProvider's default gives."""
        yield self

    def signature_reads_for(self, address) -> int:
        return self.signature_calls.get(address, 0)

    def get_signatures(self, address, limit=100, before=None,
                       failover_on_empty=False):
        # A single-source fake has nothing to fail over to, so the flag is
        # accepted (the real provider's signature carries it) and ignored.
        self.signature_calls[address] = self.signature_calls.get(address, 0) + 1
        entries = self.signatures.get(address, [])
        if before is not None:
            signatures = [e["signature"] for e in entries]
            if before in signatures:
                entries = entries[signatures.index(before) + 1:]
        return entries[:limit]

    def get_transaction(self, signature, failover_on_null=False):
        if signature in self.fail_transactions:
            from olala.chain.provider import ChainError
            raise ChainError(f"scripted failure for {signature}")
        return self.transactions.get(signature)

    def get_sol_balance(self, address):
        return self.sol_balances.get(address, 0.0)

    def get_account_info(self, pubkey):
        return self.account_info.get(pubkey)

    def get_token_supply(self, mint):
        return self.token_supply.get(mint, 0.0)

    def get_token_decimals(self, mint):
        return self.token_decimals.get(mint, 6)

    def get_token_largest_accounts(self, mint):
        return self.largest_accounts.get(mint, [])

    def get_token_account_owners(self, token_accounts):
        return [self.account_owners.get(a) for a in token_accounts]

    def send_transaction(self, signed_tx_base64):
        self.sent_transactions.append(signed_tx_base64)
        return f"fake-sig-{len(self.sent_transactions)}"

    def get_signature_status(self, signature):
        return self.signature_status.get(signature)


class FakeTracker:
    """SolanaTrackerClient replacement: scripted leaderboard, optional
    failure (covers missing entitlement, rate limit, outage alike)."""

    def __init__(self, traders=None, fail=False):
        self.traders = traders or []
        self.fail = fail
        self.calls = 0

    def top_traders(self, window_days=90, limit=100, min_trades=20,
                    min_active_days=0, sort="win_percentage",
                    max_trades_per_day=None, max_pages=1,
                    min_roi_pct=0.0, min_win_rate=0.0,
                    page_size=500, min_avg_buy_usd=0.0,
                    max_last_trade_age_sec=0.0, min_volume_usd=0.0,
                    require_closed_trades=False,
                    min_trades_per_day=0.0, max_tokens_per_day=0.0,
                    max_win_rate=1.0, min_profitable_days_ratio=0.0):
        self.calls += 1
        self.last_params = {
            "limit": limit, "page_size": page_size, "max_pages": max_pages,
            "min_avg_buy_usd": min_avg_buy_usd,
            "max_last_trade_age_sec": max_last_trade_age_sec,
        }
        if self.fail:
            from olala.chain.solana_tracker import SolanaTrackerError
            raise SolanaTrackerError("scripted failure")
        # Mirror the real client: every ceiling is applied client-side
        # from payload data, so tests see the same shape of result.
        import time as _time
        from olala.chain.solana_tracker import _tradable
        now = _time.time()
        kept = [t for t in self.traders
                if _tradable(t, max_trades_per_day, min_avg_buy_usd,
                             max_last_trade_age_sec, now, min_volume_usd,
                             require_closed_trades, min_trades_per_day,
                             max_tokens_per_day, max_win_rate,
                             min_profitable_days_ratio)]
        return kept[:limit]


class FakeJupiterTokens:
    """JupiterClient replacement for the trending-tokens surface."""

    def __init__(self, tokens=None, fail=False):
        self.tokens = tokens or []
        self.fail = fail

    def top_tokens(self, interval="24h", limit=60):
        if self.fail:
            from olala.chain.jupiter import JupiterError
            raise JupiterError("scripted failure")
        return self.tokens[:limit]


class FakeMarketData:
    """MarketDataService replacement returning scripted TokenInfo."""

    def __init__(self, tokens: dict[str, TokenInfo] | None = None) -> None:
        self.tokens = tokens or {}

    def get_token_info(self, mint: str,
                       max_age: float | None = None) -> TokenInfo | None:
        return self.tokens.get(mint)

    def search_winners(self, min_liquidity_usd, min_change_pct, limit=8):
        return []


def make_swap_tx(trader: str, sol_delta_lamports: int, mint: str,
                 token_delta: float, fee: int = 5000,
                 extra_mint_delta: tuple[str, float] | None = None,
                 failed: bool = False) -> dict[str, Any]:
    """Build a jsonParsed-shaped transaction where `trader` is fee payer.

    ``sol_delta_lamports`` is the trader's raw balance change INCLUDING the
    fee they paid (as it appears on chain).
    """
    pre_token, post_token = [], []

    def add_token(owner, token_mint, pre_amount, post_amount):
        pre_token.append({"owner": owner, "mint": token_mint,
                          "uiTokenAmount": {"uiAmount": pre_amount}})
        post_token.append({"owner": owner, "mint": token_mint,
                           "uiTokenAmount": {"uiAmount": post_amount}})

    base = 1000.0
    add_token(trader, mint, base, base + token_delta)
    if extra_mint_delta:
        extra_mint, delta = extra_mint_delta
        add_token(trader, extra_mint, base, base + delta)

    pre_balance = 50_000_000_000
    return {
        "blockTime": 1_755_000_000,
        "meta": {
            "err": {"InstructionError": []} if failed else None,
            "fee": fee,
            "preBalances": [pre_balance, 0],
            "postBalances": [pre_balance + sol_delta_lamports, 0],
            "preTokenBalances": pre_token,
            "postTokenBalances": post_token,
        },
        "transaction": {"message": {"accountKeys": [
            {"pubkey": trader, "signer": True},
            {"pubkey": "SomePool11111111111111111111111111111111111",
             "signer": False},
        ]}},
    }
