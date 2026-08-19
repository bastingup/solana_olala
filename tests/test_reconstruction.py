import pytest

from olala.discovery.reconstruction import TradeReconstructor
from olala.domain.models import TradeSide

from fakes import make_swap_tx

TRADER = "TraderAAAA1111111111111111111111111111111111"
MINT = "MintAAAA111111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"


def reconstruct(tx):
    return TradeReconstructor().reconstruct(TRADER, "sig1", tx)


def test_buy_detected_with_fee_excluded():
    # Trader spent 2 SOL on tokens + 0.000005 fee: raw delta includes fee.
    tx = make_swap_tx(TRADER, sol_delta_lamports=-2_000_005_000,
                      mint=MINT, token_delta=400.0, fee=5000)
    trade = reconstruct(tx)
    assert trade is not None
    assert trade.side.value == "buy"
    assert abs(trade.sol_amount - 2.0) < 1e-9  # fee not counted as traded
    assert trade.token_amount == 400.0
    assert abs(trade.price_sol - 0.005) < 1e-12


def test_sell_detected():
    tx = make_swap_tx(TRADER, sol_delta_lamports=1_500_000_000 - 5000,
                      mint=MINT, token_delta=-300.0)
    trade = reconstruct(tx)
    assert trade is not None
    assert trade.side.value == "sell"
    assert abs(trade.sol_amount - 1.5) < 1e-9


def test_failed_transaction_ignored():
    tx = make_swap_tx(TRADER, -2_000_000_000, MINT, 400.0, failed=True)
    assert reconstruct(tx) is None


def test_multi_token_swap_ignored():
    tx = make_swap_tx(TRADER, -2_000_000_000, MINT, 400.0,
                      extra_mint_delta=("OtherMint111", 5.0))
    assert reconstruct(tx) is None


def test_dust_ignored():
    tx = make_swap_tx(TRADER, -5_000_000, MINT, 1.0)  # 0.005 SOL < floor
    assert reconstruct(tx) is None


def test_wsol_counts_toward_sol_leg():
    # All value moved via wSOL account, raw lamports only paid the fee.
    tx = make_swap_tx(TRADER, sol_delta_lamports=-5000, mint=MINT,
                      token_delta=400.0,
                      extra_mint_delta=(WSOL, -2.0))
    trade = reconstruct(tx)
    assert trade is not None
    assert trade.side.value == "buy"
    assert abs(trade.sol_amount - 2.0) < 1e-9


def test_same_direction_transfer_not_a_trade():
    # Token up AND SOL up is an airdrop/transfer, not a swap.
    tx = make_swap_tx(TRADER, sol_delta_lamports=1_000_000_000,
                      mint=MINT, token_delta=400.0)
    assert reconstruct(tx) is None


def test_uninvolved_transaction_ignored():
    tx = make_swap_tx("SomeoneElse1111111111111111111111111111111",
                      -2_000_000_000, MINT, 400.0)
    assert TradeReconstructor().reconstruct(TRADER, "sig1", tx) is None
    assert reconstruct(None) is None


# -- dollar-quoted swaps ---------------------------------------------------
#
# Found by cross-checking a live followed wallet against Solana Tracker:
# it reported 12 trades where we reconstructed ZERO, because every swap
# was quoted in USDT rather than SOL. Missing such an ENTRY is merely a
# dead seat. Missing such an EXIT means we keep holding a token the
# trader has already sold, which is a real loss.

from olala.constants import SOL_MINT, USDC_MINT, USDT_MINT


def stable_swap(token_delta, quote_delta, quote_mint=USDC_MINT):
    """A swap with no SOL leg at all — only a fee is paid in SOL."""
    return make_swap_tx(TRADER, -5000, MINT, token_delta,
                        extra_mint_delta=(quote_mint, quote_delta))


def test_a_usdc_entry_is_recognised():
    trade = TradeReconstructor().reconstruct(
        TRADER, "sig", stable_swap(token_delta=500.0, quote_delta=-300.0))
    assert trade is not None
    assert trade.side is TradeSide.BUY
    assert trade.mint == MINT
    assert trade.quote_mint == USDC_MINT
    assert trade.quote_amount == 300.0


def test_a_usdt_exit_is_recognised():
    """The dangerous half: without this we hold the bag forever."""
    trade = TradeReconstructor().reconstruct(
        TRADER, "sig",
        stable_swap(token_delta=-500.0, quote_delta=296.55,
                    quote_mint=USDT_MINT))
    assert trade is not None
    assert trade.side is TradeSide.SELL
    assert trade.quote_mint == USDT_MINT


def test_a_dollar_quoted_trade_is_not_scoreable():
    """It has no SOL price, and inventing an exchange rate would distort
    every win rate we compute."""
    trade = TradeReconstructor().reconstruct(
        TRADER, "sig", stable_swap(token_delta=500.0, quote_delta=-300.0))
    assert trade.sol_denominated is False
    assert trade.price_sol == 0.0
    assert trade.sol_amount == 0.0


def test_a_sol_quoted_trade_is_scoreable_and_unchanged():
    trade = TradeReconstructor().reconstruct(
        TRADER, "sig",
        make_swap_tx(TRADER, -2_000_005_000, MINT, 400.0))
    assert trade.sol_denominated is True
    assert trade.quote_mint == SOL_MINT
    assert trade.quote_amount == pytest.approx(trade.sol_amount)
    assert trade.price_sol == pytest.approx(2.0 / 400.0)


def test_dollar_dust_is_not_a_trade():
    assert TradeReconstructor().reconstruct(
        TRADER, "sig",
        stable_swap(token_delta=1.0, quote_delta=-0.2)) is None


def test_a_sol_leg_still_wins_when_a_route_passes_through_a_stablecoin():
    """SOL is the leg we can price, so it decides the trade."""
    tx = make_swap_tx(TRADER, -2_000_005_000, MINT, 400.0,
                      extra_mint_delta=(USDC_MINT, 0.0))
    trade = TradeReconstructor().reconstruct(TRADER, "sig", tx)
    assert trade is not None
    assert trade.quote_mint == SOL_MINT
    assert trade.sol_denominated is True


def test_two_non_quote_tokens_moving_is_still_not_a_swap_we_copy():
    """A multi-token transaction is not something we can mirror."""
    tx = make_swap_tx(TRADER, -2_000_005_000, MINT, 400.0,
                      extra_mint_delta=("OtherMint1111", 50.0))
    assert TradeReconstructor().reconstruct(TRADER, "sig", tx) is None


def test_unpriceable_trades_are_excluded_from_scoring():
    from olala.discovery.scoring import TraderScorer

    reconstructor = TradeReconstructor()
    buy = reconstructor.reconstruct(
        TRADER, "s1", stable_swap(token_delta=500.0, quote_delta=-300.0))
    sell = reconstructor.reconstruct(
        TRADER, "s2", stable_swap(token_delta=-500.0, quote_delta=400.0))

    stats = TraderScorer().compute_stats(TRADER, [buy, sell])
    # Nothing was priced, so nothing is claimed: no invented PnL, no
    # invented win rate.
    assert stats.total_trades == 0
    assert stats.realized_pnl_sol == 0.0
