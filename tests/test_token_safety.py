from olala.config import FilterConfig, RiskConfig
from olala.risk.token_safety import TokenSafetyScreen

from conftest import make_token
from fakes import FakeProvider


def clean_provider(mint="MintA111", supply=1_000_000.0):
    provider = FakeProvider()
    provider.account_info[mint] = {
        "data": {"parsed": {"info": {"mintAuthority": None,
                                     "freezeAuthority": None}}}}
    provider.token_supply[mint] = supply
    provider.largest_accounts[mint] = [
        {"uiAmount": supply * 0.02} for _ in range(10)]
    return provider


def check(provider, token=None):
    screen = TokenSafetyScreen(provider)
    return screen.check(token or make_token(), FilterConfig(), RiskConfig())


def test_clean_token_passes():
    report = check(clean_provider())
    assert report.safe, report.reason


def test_active_mint_authority_fails():
    provider = clean_provider()
    provider.account_info["MintA111"]["data"]["parsed"]["info"][
        "mintAuthority"] = "SomeAuthority"
    report = check(provider)
    assert not report.safe
    assert "mint authority" in report.reason


def test_active_freeze_authority_fails():
    provider = clean_provider()
    provider.account_info["MintA111"]["data"]["parsed"]["info"][
        "freezeAuthority"] = "SomeAuthority"
    report = check(provider)
    assert not report.safe
    assert "freeze" in report.reason


def test_holder_concentration_fails():
    provider = clean_provider()
    provider.largest_accounts["MintA111"] = [
        {"uiAmount": 100_000.0} for _ in range(10)]  # top10 own 100%
    report = check(provider)
    assert not report.safe
    assert "holders" in report.reason


def test_thin_liquidity_fails_without_rpc():
    token = make_token(liquidity_usd=1_000.0)
    report = check(FakeProvider(), token=token)  # no RPC data needed
    assert not report.safe
    assert "liquidity" in report.reason


def test_market_cap_band_fails():
    token = make_token(market_cap_usd=1_000_000.0)
    report = check(FakeProvider(), token=token)
    assert not report.safe


def test_young_pair_fails():
    token = make_token(pair_age_days=3.0)
    report = check(clean_provider(), token=token)
    assert not report.safe
    assert "old" in report.reason


def test_result_cached_per_mint():
    provider = clean_provider()
    screen = TokenSafetyScreen(provider)
    token = make_token()
    assert screen.check(token, FilterConfig(), RiskConfig()).safe
    # Poison the provider: cached verdict should still be served.
    provider.account_info["MintA111"]["data"]["parsed"]["info"][
        "mintAuthority"] = "Auth"
    assert screen.check(token, FilterConfig(), RiskConfig()).safe
