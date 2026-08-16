import pytest

from olala.domain.models import ExitReason, Fill, TradeSide
from olala.trading.portfolio import PortfolioManager

from conftest import make_token
from fakes import FakeProvider


@pytest.fixture
def portfolio(db, bus, config_store):
    return PortfolioManager(db, bus, config_store, FakeProvider())


def buy_fill(sol=1.0, price=0.005):
    return Fill(order_id="o1", side=TradeSide.BUY, mint="MintA111",
                quantity=(sol - 0.0001) / price, price_sol=price,
                sol_amount=sol, fee_sol=0.0001)


def test_paper_wallets_seeded_from_config(portfolio, config_store):
    wallets = portfolio.wallets()
    assert len(wallets) == config_store.config.paper.wallet_count
    assert all(w.is_paper for w in wallets)
    assert all(w.base_balance() == 10.0 for w in wallets)


def test_buy_debits_wallet_and_opens_position(portfolio, token):
    wallet = portfolio.wallets()[0]
    position = portfolio.apply_buy(wallet, "t1", token, buy_fill(sol=2.0))
    assert wallet.base_balance() == pytest.approx(8.0)
    assert position.sol_invested == 2.0
    assert portfolio.find_open(wallet.id, "t1", token.mint) is position


def test_resize_merges_position_with_weighted_entry(portfolio, token):
    wallet = portfolio.wallets()[0]
    portfolio.apply_buy(wallet, "t1", token, Fill(
        order_id="o1", side=TradeSide.BUY, mint=token.mint, quantity=100,
        price_sol=0.010, sol_amount=1.0, fee_sol=0))
    position = portfolio.apply_buy(wallet, "t1", token, Fill(
        order_id="o2", side=TradeSide.BUY, mint=token.mint, quantity=100,
        price_sol=0.020, sol_amount=2.0, fee_sol=0))
    assert position.quantity == 200
    assert position.entry_price_sol == pytest.approx(0.015)
    assert position.sol_invested == 3.0
    assert len(portfolio.open_positions(wallet.id)) == 1


def test_close_credits_wallet_and_realizes_pnl(portfolio, token):
    wallet = portfolio.wallets()[0]
    position = portfolio.apply_buy(wallet, "t1", token, buy_fill(sol=2.0))
    sell = Fill(order_id="o2", side=TradeSide.SELL, mint=token.mint,
                quantity=position.quantity, price_sol=0.006,
                sol_amount=2.35, fee_sol=0.0001)
    portfolio.apply_close(wallet, position, sell, ExitReason.TRADER_EXIT)
    assert position.status.value == "closed"
    assert position.realized_pnl_sol == pytest.approx(0.35)
    assert wallet.base_balance() == pytest.approx(8.0 + 2.35)
    assert portfolio.find_open(wallet.id, "t1", token.mint) is None


def test_exposure_accounts_for_positions(portfolio, token):
    wallet = portfolio.wallets()[0]
    portfolio.apply_buy(wallet, "t1", token, buy_fill(sol=2.0))
    portfolio.mark_price(token.mint, 0.006)
    exposure = portfolio.exposure(wallet.id, token.mint)
    assert exposure.cash_sol == pytest.approx(8.0)
    assert exposure.invested_in_mint_sol == 2.0
    assert exposure.open_positions == 1
    assert exposure.equity_sol > 8.0


def test_mark_price_trails_peak(portfolio, token):
    wallet = portfolio.wallets()[0]
    position = portfolio.apply_buy(wallet, "t1", token, buy_fill())
    portfolio.mark_price(token.mint, 0.008)
    portfolio.mark_price(token.mint, 0.006)
    assert position.peak_price_sol == 0.008
    assert position.last_price_sol == 0.006


def test_state_survives_reload(db, bus, config_store, token):
    portfolio = PortfolioManager(db, bus, config_store, FakeProvider())
    wallet = portfolio.wallets()[0]
    portfolio.apply_buy(wallet, "t1", token, buy_fill(sol=2.0))

    reloaded = PortfolioManager(db, bus, config_store, FakeProvider())
    match = reloaded.get_wallet(wallet.id)
    assert match is not None
    assert match.base_balance() == pytest.approx(8.0)
    assert len(reloaded.open_positions(wallet.id)) == 1
    # No extra paper wallets were minted on reload.
    assert len(reloaded.wallets()) == len(portfolio.wallets())
