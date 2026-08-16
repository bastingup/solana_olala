import pytest

from olala.domain.wallet import PaperSolanaWallet
from olala.trading.executor import (PAPER_FEE_SOL, ExecutionError,
                                    PaperExecutor)

from conftest import make_token


@pytest.fixture
def wallet():
    return PaperSolanaWallet.create("Test", 10.0)


def test_buy_pays_spread_and_fee(wallet, token):
    fill = PaperExecutor().buy(wallet, token, 1.0)
    assert fill.side.value == "buy"
    assert fill.sol_amount == 1.0
    assert fill.fee_sol == PAPER_FEE_SOL
    # Execution price is worse than mid for a buy.
    assert fill.price_sol > token.price_sol
    assert fill.quantity * fill.price_sol <= 1.0


def test_sell_receives_less_than_mid(wallet, token):
    fill = PaperExecutor().sell(wallet, token, 100.0)
    assert fill.side.value == "sell"
    assert fill.price_sol < token.price_sol
    assert fill.sol_amount < 100.0 * token.price_sol


def test_large_trade_pays_more_impact(wallet):
    shallow = make_token(liquidity_usd=50_000.0)
    small = PaperExecutor().buy(wallet, shallow, 0.1)
    big = PaperExecutor().buy(wallet, shallow, 5.0)
    assert big.price_sol > small.price_sol


def test_zero_liquidity_worst_case_impact(wallet):
    token = make_token(liquidity_usd=0.0)
    fill = PaperExecutor().buy(wallet, token, 1.0)
    assert fill.price_sol == pytest.approx(
        token.price_sol * (1.0 + 0.001 + 0.05))


def test_dust_order_raises(wallet, token):
    with pytest.raises(ExecutionError):
        PaperExecutor().buy(wallet, token, PAPER_FEE_SOL / 2)
