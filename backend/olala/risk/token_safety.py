"""Token safety screening.

Structural checks against honeypots and ruggable contracts, using only
on-chain facts: active mint/freeze authority, holder concentration, pool
depth, market-cap band, and pair age. A token failing any check is
untradeable — there is no override.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..chain.provider import ChainError, RpcProvider
from ..config import FilterConfig, RiskConfig
from ..domain.models import TokenInfo

logger = logging.getLogger(__name__)

SAFETY_CACHE_TTL_SEC = 1800.0


@dataclass
class SafetyReport:
    safe: bool
    reason: str = ""


class TokenSafetyScreen:
    def __init__(self, provider: RpcProvider) -> None:
        self._provider = provider
        self._cache: dict[str, tuple[float, SafetyReport]] = {}

    def check(self, token: TokenInfo, filters: FilterConfig,
              risk: RiskConfig) -> SafetyReport:
        # Market-shape checks are cheap and use fresh data — always run.
        if token.liquidity_usd < filters.min_token_liquidity_usd:
            return SafetyReport(False, (
                f"liquidity ${token.liquidity_usd:,.0f} below floor"))
        if not (filters.min_token_market_cap_usd <= token.market_cap_usd
                <= filters.max_token_market_cap_usd):
            return SafetyReport(False, (
                f"market cap ${token.market_cap_usd:,.0f} outside band"))
        if token.pair_created_at and risk.min_pair_age_days > 0:
            age_days = (time.time() - token.pair_created_at) / 86_400.0
            if age_days < risk.min_pair_age_days:
                return SafetyReport(False, f"pair only {age_days:.1f}d old")

        cached = self._cache.get(token.mint)
        if cached and time.time() - cached[0] < SAFETY_CACHE_TTL_SEC:
            return cached[1]
        report = self._contract_checks(token, risk)
        self._cache[token.mint] = (time.time(), report)
        return report

    def _contract_checks(self, token: TokenInfo,
                         risk: RiskConfig) -> SafetyReport:
        try:
            account = self._provider.get_account_info(token.mint)
            info = (((account or {}).get("data") or {}).get("parsed")
                    or {}).get("info") or {}
            if info.get("mintAuthority"):
                return SafetyReport(False, "mint authority still active")
            if info.get("freezeAuthority"):
                return SafetyReport(False, "freeze authority still active")

            supply = self._provider.get_token_supply(token.mint)
            if supply > 0:
                largest = self._provider.get_token_largest_accounts(token.mint)
                top10 = sum(float(e.get("uiAmount") or 0.0)
                            for e in largest[:10])
                concentration = top10 / supply
                if concentration > risk.max_token_top10_holder_fraction:
                    return SafetyReport(False, (
                        f"top-10 holders own {concentration:.0%} of supply"))
        except ChainError as exc:
            logger.warning("safety check degraded for %s: %s", token.mint, exc)
            return SafetyReport(False, "safety data unavailable")
        return SafetyReport(True)
