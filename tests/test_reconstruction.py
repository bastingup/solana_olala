from olala.discovery.reconstruction import TradeReconstructor

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
