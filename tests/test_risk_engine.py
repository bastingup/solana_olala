import pytest

from olala.config import AppConfig
from olala.risk.engine import RiskEngine, WalletExposure

from conftest import make_token


def exposure(cash=10.0, equity=10.0, open_positions=0, invested=0.0):
    return WalletExposure(wallet_id="w1", cash_sol=cash, equity_sol=equity,
                          open_positions=open_positions,
                          invested_in_mint_sol=invested)


def evaluate(token=None, expo=None, is_resize=False, config=None):
    return RiskEngine().evaluate_entry(
        config or AppConfig(), token or make_token(), expo or exposure(),
        is_resize)


def test_normal_entry_sized_at_per_trade_fraction():
    verdict = evaluate()
    assert verdict.approved
    # 5% of 10 SOL equity, deep pool: target wins.
    assert abs(verdict.size_sol - 0.5) < 1e-6


def test_liquidity_ceiling_binds():
    # Pool of $10k, SOL at $200: 1% = $100 = 0.5 SOL max; with 0.4 SOL
    # already in the pool only ~0.1 SOL of headroom remains.
    token = make_token(liquidity_usd=10_000.0, price_usd=1.0,
                       price_sol=0.005)  # SOL = $200
    verdict = evaluate(token=token, expo=exposure(equity=100.0, cash=100.0,
                                                  invested=0.40))
    assert verdict.approved
    assert verdict.size_sol == pytest.approx(0.1, abs=1e-6)

    verdict = evaluate(token=token, expo=exposure(equity=100.0, cash=100.0,
                                                  invested=0.49))
    assert not verdict.approved
    assert "liquidity" in verdict.reason


def test_reserve_blocks_new_entries_but_not_resizes():
    config = AppConfig()
    # Cash 3, equity 10 -> reserve 3: nothing left for a new entry.
    expo = exposure(cash=3.0, equity=10.0)
    verdict = evaluate(expo=expo, config=config)
    assert not verdict.approved
    assert "reserve" in verdict.reason

    resize = evaluate(expo=exposure(cash=3.0, equity=10.0, invested=0.2),
                      is_resize=True, config=config)
    assert resize.approved


def test_max_positions_per_wallet():
    config = AppConfig()
    expo = exposure(open_positions=config.risk.max_positions_per_wallet)
    verdict = evaluate(expo=expo, config=config)
    assert not verdict.approved
    assert "max positions" in verdict.reason
    # A resize is exempt from the position-count ceiling.
    assert evaluate(expo=exposure(
        open_positions=config.risk.max_positions_per_wallet, invested=0.3),
        is_resize=True, config=config).approved


def test_per_position_ceiling_binds_on_resize():
    # Already invested 2x per-trade-fraction of equity -> ceiling reached.
    expo = exposure(cash=10.0, equity=10.0, invested=1.0)
    verdict = evaluate(expo=expo, is_resize=True)
    assert not verdict.approved
    assert "per-position" in verdict.reason


def test_unpriceable_token_rejected():
    token = make_token(price_sol=0.0, price_usd=0.0)
    verdict = evaluate(token=token)
    assert not verdict.approved
