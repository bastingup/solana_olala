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


def test_entry_is_sized_by_the_token_not_the_wallet():
    """A $50M-cap token in a deep pool: the market-cap ladder decides,
    and the same wallet would get a far smaller order in a $10k token."""
    from olala.risk.engine import target_size_sol

    config = AppConfig()
    verdict = evaluate(config=config)
    assert verdict.approved
    expected = target_size_sol(config.risk, 50_000_000.0)
    assert abs(verdict.size_sol - expected) < 1e-6
    assert 0.5 < verdict.size_sol < 0.9        # mid-ladder, not the floor


def test_the_size_ladder_spans_floor_to_ceiling_logarithmically():
    """One number cannot serve a $2k launch and a $1B major. Six orders
    of magnitude of market cap need a log ramp, or everything below
    $100M sits on the floor."""
    from olala.risk.engine import target_size_sol

    r = AppConfig().risk
    assert target_size_sol(r, 0) == r.min_trade_sol            # unknown
    assert target_size_sol(r, 2_409) == r.min_trade_sol        # a real one
    assert target_size_sol(r, 10 ** 12) == r.max_trade_sol     # clamped

    ladder = [target_size_sol(r, mc)
              for mc in (10_000, 100_000, 1_000_000, 10_000_000,
                         100_000_000, 1_000_000_000)]
    assert ladder == sorted(ladder)                # monotonic
    assert ladder[0] == r.min_trade_sol
    assert ladder[-1] == r.max_trade_sol
    # Each decade of market cap adds roughly the same amount of size —
    # that is what "logarithmic" buys us.
    steps = [b - a for a, b in zip(ladder, ladder[1:])]
    assert max(steps) - min(steps) < 1e-6


def test_a_tiny_token_is_traded_small_rather_than_refused():
    """The point of the ladder: a $20k pump.fun token with a shallow but
    real pool gets a small order instead of being excluded."""
    token = make_token(market_cap_usd=20_000.0, liquidity_usd=5_000.0,
                       price_usd=1.0, price_sol=0.005)
    verdict = evaluate(token=token)
    assert verdict.approved
    assert verdict.size_sol <= 0.1


def test_a_token_with_no_pool_is_still_refused():
    """Sizing down is not the same as trading into nothing."""
    token = make_token(market_cap_usd=2_409.0, liquidity_usd=0.0)
    verdict = evaluate(token=token)
    assert not verdict.approved
    assert "liquidity" in verdict.reason


def test_liquidity_ceiling_binds():
    # Pool of $10k, SOL at $200: 1% = $100 = 0.5 SOL max; with 0.4 SOL
    # already in the pool only ~0.1 SOL of headroom remains.
    token = make_token(liquidity_usd=10_000.0, price_usd=1.0,
                       price_sol=0.005)  # SOL = $200
    verdict = evaluate(token=token, expo=exposure(equity=100.0, cash=100.0,
                                                  invested=0.40))
    assert verdict.approved
    assert verdict.size_sol == pytest.approx(0.1, abs=1e-6)

    # Headroom shrinks to a real but tiny amount: we now TRADE it small
    # rather than refuse, which is the point of the market-cap ladder.
    verdict = evaluate(token=token, expo=exposure(equity=100.0, cash=100.0,
                                                  invested=0.48))
    assert verdict.approved
    assert verdict.size_sol == pytest.approx(0.02, abs=1e-6)

    # Below the minimum order it is still refused — sizing down is not
    # the same as trading dust.
    verdict = evaluate(token=token, expo=exposure(equity=100.0, cash=100.0,
                                                  invested=0.4999))
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
    """The ceiling is measured against a normal entry in THIS token, so
    it scales with the token the way the target does."""
    from olala.risk.engine import target_size_sol

    config = AppConfig()
    already = (target_size_sol(config.risk, 50_000_000.0)
               * config.risk.max_position_equity_multiple)
    expo = exposure(cash=10.0, equity=10.0, invested=already)
    verdict = evaluate(expo=expo, is_resize=True, config=config)
    assert not verdict.approved
    assert "per-position" in verdict.reason


def test_unpriceable_token_rejected():
    token = make_token(price_sol=0.0, price_usd=0.0)
    verdict = evaluate(token=token)
    assert not verdict.approved
