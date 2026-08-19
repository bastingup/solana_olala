"""Shared fixtures: everything runs offline against temp storage."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from olala.config import ConfigStore  # noqa: E402
from olala.domain.models import TokenInfo  # noqa: E402
from olala.events import EventBus  # noqa: E402
from olala.persistence.database import Database  # noqa: E402


@pytest.fixture
def config_store(tmp_path) -> ConfigStore:
    """Filters APPLIED (dev_mode: true) — the strict pipeline is what
    most tests are asserting against."""
    path = tmp_path / "config.yaml"
    path.write_text("dev_mode: true\n")
    return ConfigStore(path=path)


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(path=tmp_path / "test.db")


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


def make_token(mint: str = "MintA111", symbol: str = "TOKA",
               price_sol: float = 0.005, price_usd: float = 1.0,
               liquidity_usd: float = 800_000.0,
               market_cap_usd: float = 50_000_000.0,
               pair_age_days: float = 120.0) -> TokenInfo:
    return TokenInfo(
        mint=mint, symbol=symbol, name=symbol,
        price_usd=price_usd, price_sol=price_sol,
        liquidity_usd=liquidity_usd, market_cap_usd=market_cap_usd,
        pair_address=f"pair-{mint}", dex="raydium",
        pair_created_at=time.time() - pair_age_days * 86_400)


@pytest.fixture
def token() -> TokenInfo:
    return make_token()
