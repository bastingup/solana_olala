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


# -- the hot path must never block on the chain ----------------------------

def test_snapshot_does_not_hold_the_lock_across_a_chain_read(db, bus,
                                                             config_store):
    """`snapshot()` used to call base_balance() — a blocking RPC for a
    live wallet — while holding the portfolio lock, so a slow node
    stalled every buy, sell and panic stop in the system."""
    import threading
    import time

    from solders.keypair import Keypair

    from fakes import FakeProvider

    class SlowProvider(FakeProvider):
        def get_sol_balance(self, address):
            time.sleep(0.3)
            return 5.0

    portfolio = PortfolioManager(db, bus, config_store, SlowProvider())
    portfolio.add_live_wallet("Vault", str(Keypair().pubkey()))

    acquired = threading.Event()

    def grab_lock():
        # Whatever snapshot() is doing, the lock must be obtainable.
        with portfolio._lock:
            acquired.set()

    snapshotter = threading.Thread(target=portfolio.snapshot)
    snapshotter.start()
    time.sleep(0.05)                       # let it get into the slow read
    threading.Thread(target=grab_lock).start()

    assert acquired.wait(timeout=0.2), \
        "portfolio lock was held across a chain read"
    snapshotter.join(timeout=2)


def test_live_balance_is_cached_between_reads(db, bus, config_store):
    from solders.keypair import Keypair

    from fakes import FakeProvider

    class CountingProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.reads = 0

        def get_sol_balance(self, address):
            self.reads += 1
            return 7.0

    provider = CountingProvider()
    portfolio = PortfolioManager(db, bus, config_store, provider)
    wallet = portfolio.add_live_wallet("Vault", str(Keypair().pubkey()))

    before = provider.reads
    for _ in range(5):
        wallet.base_balance()
    assert provider.reads == before        # served from cache

    # Sizing demands certainty and pays for it.
    assert wallet.base_balance(max_age_sec=0.0) == 7.0
    assert provider.reads == before + 1


def test_a_failed_balance_read_keeps_the_last_known_value(db, bus,
                                                          config_store):
    from solders.keypair import Keypair

    from fakes import FakeProvider

    class FlakyProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.fail = False

        def get_sol_balance(self, address):
            if self.fail:
                raise RuntimeError("node is down")
            return 3.0

    provider = FlakyProvider()
    portfolio = PortfolioManager(db, bus, config_store, provider)
    wallet = portfolio.add_live_wallet("Vault", str(Keypair().pubkey()))
    assert wallet.base_balance(max_age_sec=0.0) == 3.0

    provider.fail = True
    # An outage must not read as "the wallet is empty".
    assert wallet.base_balance(max_age_sec=0.0) == 3.0
