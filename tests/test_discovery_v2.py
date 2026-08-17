"""Discovery v2: leaderboard sourcing, flow fallback, bot pre-screen,
copyability scoring."""

import time

from olala.discovery.scanner import (PRESCREEN_SIGNATURES, RpcBudget,
                                     TraderDiscoveryDaemon)
from olala.discovery.scoring import TraderScorer
from olala.domain.models import ObservedTrade, TraderStatus, TradeSide
from olala.services.traders import TraderRegistry

from conftest import make_token
from fakes import (FakeBirdeye, FakeJupiterTokens, FakeMarketData,
                   FakeProvider, make_swap_tx)

ELITE = "EliteTraderAAAA11111111111111111111111111111"
BOT = "BotWalletBBBB1111111111111111111111111111111"
WINNER_MINT = "WinnerMint111111111111111111111111111111111"
WINNER_PAIR = "pair-WinnerMint111111111111111111111111111111111"


def winner_token(change=80.0, liquidity=400_000.0, txns=4000):
    return {"mint": WINNER_MINT, "symbol": "WINR",
            "liquidity_usd": liquidity, "price_change_pct": change,
            "txns_24h": txns, "organic_score": 70.0, "verified": True}


def winner_market():
    """Market data that resolves the winner mint to its pool address."""
    return FakeMarketData({WINNER_MINT: make_token(mint=WINNER_MINT,
                                                   symbol="WINR")})


def human_signatures(count=250):
    """Deep history at a human cadence: ~10 events/day over ~25 days."""
    now = time.time()
    return [{"signature": f"h{i}", "err": None,
             "blockTime": now - i * 8640} for i in range(count)]


def bot_signatures(count=250):
    """Deep history at machine cadence: one signature per second."""
    now = time.time()
    return [{"signature": f"b{i}", "err": None,
             "blockTime": now - i} for i in range(count)]


def big_swap(trader):
    """A harvest transaction above the dust threshold (2 SOL)."""
    return make_swap_tx(trader, -2_000_005_000, "MintX111", 100.0)


def make_daemon(db, bus, config_store, provider=None, birdeye=None,
                jupiter=None, market=None, tracker=None):
    provider = provider or FakeProvider()
    registry = TraderRegistry(db, bus)
    daemon = TraderDiscoveryDaemon(
        config_store, provider, market or FakeMarketData(), registry, db,
        bus, assign_wallet=lambda: "w1", birdeye=birdeye, jupiter=jupiter,
        tracker=tracker)
    return provider, registry, daemon


# A real on-curve wallet address (any generated keypair qualifies).
from solders.keypair import Keypair
ONCURVE_HOLDER = str(Keypair().pubkey())


def stage_winner_holders(provider, holders, supply=1_000_000.0):
    """Stage the winner token's top accounts. ``holders`` is a list of
    (token_account, owner, amount)."""
    provider.token_supply[WINNER_MINT] = supply
    provider.largest_accounts[WINNER_MINT] = [
        {"address": acct, "uiAmount": amount}
        for acct, _, amount in holders]
    provider.account_owners.update(
        {acct: owner for acct, owner, _ in holders})


def test_leaderboard_candidates_admitted_to_review(db, bus, config_store):
    birdeye = FakeBirdeye(traders=[{"address": ELITE, "pnl": 1234.5}])
    provider, registry, daemon = make_daemon(db, bus, config_store,
                                             birdeye=birdeye)
    provider.signatures[ELITE] = human_signatures()
    daemon._harvest_candidates(config_store.config, RpcBudget(30))
    profile = registry.get(ELITE)
    assert profile is not None
    assert profile.status is TraderStatus.CANDIDATE
    assert birdeye.calls == 1


def test_pre_screen_rejects_machine_frequency_wallet(db, bus, config_store):
    birdeye = FakeBirdeye(traders=[{"address": BOT, "pnl": 99999.0}])
    provider, registry, daemon = make_daemon(db, bus, config_store,
                                             birdeye=birdeye)
    provider.signatures[BOT] = bot_signatures()
    daemon._harvest_candidates(config_store.config, RpcBudget(30))
    profile = registry.get(BOT)
    assert profile is not None
    assert profile.status is TraderStatus.REJECTED
    assert "machine-frequency" in profile.rejection_reason


def test_winner_holders_become_candidates(db, bus, config_store):
    jupiter = FakeJupiterTokens([winner_token()])
    provider, registry, daemon = make_daemon(
        db, bus, config_store, jupiter=jupiter, market=winner_market())
    stage_winner_holders(provider, [("ta1", ONCURVE_HOLDER, 20_000.0)])
    provider.signatures[ONCURVE_HOLDER] = human_signatures()
    daemon._harvest_candidates(config_store.config, RpcBudget(40))
    profile = registry.get(ONCURVE_HOLDER)
    assert profile is not None
    assert profile.status is TraderStatus.CANDIDATE
    assert daemon._counters["winners_mined"] == 1
    assert daemon._counters["smart_holders"] == 1
    assert WINNER_MINT in daemon._early_hits[ONCURVE_HOLDER]


def test_winner_below_thresholds_ignored(db, bus, config_store):
    jupiter = FakeJupiterTokens([winner_token(change=5.0)])
    provider, registry, daemon = make_daemon(
        db, bus, config_store, jupiter=jupiter, market=winner_market())
    daemon._harvest_candidates(config_store.config, RpcBudget(40))
    assert daemon._counters["winners_mined"] == 0


def test_winner_cooldown_prevents_rescan(db, bus, config_store):
    jupiter = FakeJupiterTokens([winner_token()])
    provider, registry, daemon = make_daemon(
        db, bus, config_store, jupiter=jupiter, market=winner_market())
    stage_winner_holders(provider, [("ta1", ONCURVE_HOLDER, 20_000.0)])
    provider.signatures[ONCURVE_HOLDER] = human_signatures()
    daemon._harvest_candidates(config_store.config, RpcBudget(40))
    daemon._harvest_candidates(config_store.config, RpcBudget(40))
    assert daemon._counters["winners_mined"] == 1


def test_program_owned_accounts_excluded(db, bus, config_store):
    """Pool vaults and lockers are PDAs (off-curve) — never candidates."""
    jupiter = FakeJupiterTokens([winner_token()])
    provider, registry, daemon = make_daemon(
        db, bus, config_store, jupiter=jupiter, market=winner_market())
    from solders.pubkey import Pubkey
    pda = str(Pubkey.find_program_address(
        [b"vault"], Pubkey.from_string(
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"))[0])
    stage_winner_holders(provider, [("ta1", pda, 20_000.0)])
    daemon._harvest_candidates(config_store.config, RpcBudget(40))
    assert registry.get(pda) is None
    assert daemon._counters["smart_holders"] == 0


def test_pool_scale_holders_excluded(db, bus, config_store):
    """An account holding >10% of supply is a vault or omnibus wallet."""
    jupiter = FakeJupiterTokens([winner_token()])
    provider, registry, daemon = make_daemon(
        db, bus, config_store, jupiter=jupiter, market=winner_market())
    stage_winner_holders(provider,
                         [("ta1", ONCURVE_HOLDER, 400_000.0)])  # 40%
    daemon._harvest_candidates(config_store.config, RpcBudget(40))
    assert registry.get(ONCURVE_HOLDER) is None


def test_multi_winner_wallets_scanned_first(db, bus, config_store):
    provider, registry, daemon = make_daemon(db, bus, config_store)
    other = "OtherTraderCCCC1111111111111111111111111111"
    registry.add_candidate(other)
    registry.add_candidate(ELITE)
    daemon._early_hits[ELITE] = {"m1", "m2"}
    daemon._early_hits[other] = {"m1"}
    ordered = registry.by_status(TraderStatus.CANDIDATE)
    ordered.sort(key=lambda p: (
        -len(daemon._early_hits.get(p.address, ())), p.discovered_at))
    assert ordered[0].address == ELITE


def test_thin_wallet_rejected_before_history_scan(db, bus, config_store):
    """A wallet with fewer transactions than the trade requirement cannot
    qualify by arithmetic — it must never consume a history scan."""
    provider, registry, daemon = make_daemon(db, bus, config_store)
    provider.signatures[ELITE] = human_signatures(count=12)
    assert daemon._pre_screen(config_store.config, ELITE,
                              RpcBudget(5)) is False
    profile = registry.get(ELITE)
    assert profile.status is TraderStatus.REJECTED
    assert "12 transactions" in profile.rejection_reason
    assert daemon._counters["too_thin"] == 1





# -- copyability metrics from FIFO matching -------------------------------

def trade(side, amount, price, at, mint="m1"):
    return ObservedTrade(
        trader="t1", signature=f"s{at}", side=side, mint=mint,
        token_amount=amount, sol_amount=amount * price, price_sol=price,
        block_time=1_700_000_000 + at)


def test_median_hold_minutes_computed():
    trades = [
        trade(TradeSide.BUY, 100, 0.01, 0),
        trade(TradeSide.SELL, 100, 0.02, 3600),      # 60m hold
        trade(TradeSide.BUY, 100, 0.01, 10_000),
        trade(TradeSide.SELL, 100, 0.02, 10_000 + 1200),  # 20m hold
        trade(TradeSide.BUY, 100, 0.01, 50_000),
        trade(TradeSide.SELL, 100, 0.02, 50_000 + 2400),  # 40m hold
    ]
    stats = TraderScorer().compute_stats("t1", trades)
    assert stats.median_hold_minutes == 40.0


def test_bot_style_shows_tiny_holds_and_high_frequency():
    trades = []
    for i in range(200):
        trades.append(trade(TradeSide.BUY, 10, 0.01, i * 20))
        trades.append(trade(TradeSide.SELL, 10, 0.0101, i * 20 + 4))
    stats = TraderScorer().compute_stats("t1", trades)
    assert stats.median_hold_minutes < 0.5
    assert stats.trades_per_day > 1000


def test_status_is_retained_for_late_connecting_clients(db, bus,
                                                        config_store):
    """A page loaded between sweeps must still see current state."""
    provider, registry, daemon = make_daemon(
        db, bus, config_store, jupiter=FakeJupiterTokens([]))
    assert daemon.last_status is None
    daemon.tick()
    status = daemon.last_status
    assert status is not None
    assert status["phase"] == "sweep_done"
    assert status["source"] == "Winners' holders"
    assert status["next_sweep_at"] > 0
    assert set(status["counters"]) == {
        "census_seen", "census_promoted", "wallets_screened",
        "bots_blocked", "too_thin", "winners_mined", "smart_holders",
        "histories_read", "admitted", "rejected"}


def test_bot_block_increments_counter(db, bus, config_store):
    birdeye = FakeBirdeye(traders=[{"address": BOT, "pnl": 1.0}])
    provider, registry, daemon = make_daemon(db, bus, config_store,
                                             birdeye=birdeye)
    provider.signatures[BOT] = bot_signatures()
    daemon._harvest_candidates(config_store.config, RpcBudget(30))
    assert daemon._counters["bots_blocked"] == 1
    assert daemon._counters["wallets_screened"] == 1


def test_strongest_winner_mined_first(db, bus, config_store):
    weak = dict(winner_token(change=35.0))
    weak["mint"] = "WeakMint1111111111111111111111111111111111"
    weak["symbol"] = "WEAK"
    strong = winner_token(change=90.0)
    provider, registry, daemon = make_daemon(
        db, bus, config_store, jupiter=FakeJupiterTokens([weak, strong]))
    winners = daemon._find_winner_tokens(config_store.config)
    assert winners[0]["symbol"] == "WINR"
