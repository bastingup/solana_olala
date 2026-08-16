from olala.domain.models import (Fill, ObservedTrade, Position,
                                 TraderProfile, TraderStats, TraderStatus,
                                 TradeSide)
from olala.persistence.database import Database


def test_wallet_roundtrip(db):
    db.upsert_wallet("w1", "Scout", "addr", True, 9.5)
    db.upsert_wallet("w1", "Scout Renamed", "addr", True, 8.0)
    rows = db.load_wallets()
    assert len(rows) == 1
    assert rows[0]["label"] == "Scout Renamed"
    assert rows[0]["sol_balance"] == 8.0


def test_trader_roundtrip_with_stats_and_cursors(db):
    stats = TraderStats(address="t1", first_trade_at=1.0, last_trade_at=2.0,
                        total_trades=10, closed_round_trips=4, wins=3,
                        realized_pnl_sol=1.5, distinct_tokens=2)
    profile = TraderProfile(address="t1", status=TraderStatus.FOLLOWED,
                            stats=stats, score=0.8,
                            assigned_wallet_id="w1")
    db.upsert_trader(profile, history_cursor="sigX", history_complete=True,
                     follow_cursor="sigY")
    row = db.load_traders()[0]
    assert row["history_cursor"] == "sigX"
    assert row["follow_cursor"] == "sigY"
    restored = Database.trader_from_row(row)
    assert restored.status is TraderStatus.FOLLOWED
    assert restored.stats.wins == 3
    assert restored.stats.win_rate == 0.75


def test_observed_trades_dedupe_and_order(db):
    trades = [ObservedTrade(trader="t1", signature=f"s{i}",
                            side=TradeSide.BUY, mint="m1", token_amount=1,
                            sol_amount=0.1, price_sol=0.1,
                            block_time=100 - i)
              for i in range(3)]
    db.insert_observed_trades(trades)
    db.insert_observed_trades(trades)  # duplicates ignored
    loaded = db.load_observed_trades("t1")
    assert len(loaded) == 3
    assert [t.block_time for t in loaded] == sorted(
        t.block_time for t in loaded)


def test_position_and_fill_roundtrip(db):
    position = Position.open_new("w1", "t1", "m1", "TOK", 100, 0.01, 1.0)
    db.save_position(position)
    position.quantity = 150
    db.save_position(position)
    loaded = db.load_positions()
    assert len(loaded) == 1
    assert loaded[0].quantity == 150

    fill = Fill(order_id="o1", side=TradeSide.BUY, mint="m1", quantity=100,
                price_sol=0.01, sol_amount=1.0, fee_sol=0.001)
    db.save_fill("w1", fill)
    fills = db.load_fills()
    assert fills[0]["order_id"] == "o1"
