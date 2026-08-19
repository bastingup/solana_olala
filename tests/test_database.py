import pytest

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
    db.upsert_trader(profile, history_cursor="sigX", history_complete=True)
    # The tracking watermark is written separately, by its only owner.
    db.update_watermarks([("t1", 4242, "sigY")])
    row = db.load_traders()[0]
    assert row["history_cursor"] == "sigX"
    assert db.load_watermarks()["t1"] == (4242, "sigY")
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


# -- durability & atomicity foundations ------------------------------------

def test_wal_and_relaxed_sync_are_enabled(db):
    """Readers must not block behind a writing daemon."""
    mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    # synchronous=NORMAL is 1; FULL (2) would fsync on every commit.
    assert db._conn.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_schema_version_is_stamped(db):
    from olala.persistence.database import SCHEMA_VERSION
    assert db.schema_version == SCHEMA_VERSION


def test_migration_is_idempotent_across_reopen(tmp_path):
    path = tmp_path / "m.db"
    first = Database(path)
    first.upsert_wallet("w1", "Scout", "addr", True, 1.0)
    second = Database(path)          # re-runs schema + migrations
    assert len(second.load_wallets()) == 1
    assert second.schema_version == first.schema_version


def test_transaction_commits_grouped_writes_together(db):
    fill = Fill(order_id="o1", mint="m1", side=TradeSide.BUY, quantity=1.0,
                price_sol=0.5, sol_amount=0.5, fee_sol=0.0)
    position = Position(id="p1", wallet_id="w1", trader="t1", mint="m1",
                        symbol="S", quantity=1.0, entry_price_sol=0.5,
                        sol_invested=0.5, opened_at=1.0)
    with db.transaction():
        db.save_position(position)
        db.save_fill("w1", fill)
    assert len(db.load_positions()) == 1
    assert len(db.load_fills()) == 1


def test_transaction_rolls_back_every_write_on_failure(db):
    """A position must never outlive the fill that explains it."""
    position = Position(id="p9", wallet_id="w1", trader="t1", mint="m1",
                        symbol="S", quantity=1.0, entry_price_sol=0.5,
                        sol_invested=0.5, opened_at=1.0)
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.save_position(position)
            raise RuntimeError("executor blew up between the two writes")
    assert db.load_positions() == []


def test_nested_transaction_does_not_commit_early(db):
    position = Position(id="p2", wallet_id="w1", trader="t1", mint="m1",
                        symbol="S", quantity=1.0, entry_price_sol=0.5,
                        sol_invested=0.5, opened_at=1.0)
    with pytest.raises(RuntimeError):
        with db.transaction():
            with db.transaction():          # inner joins, must not commit
                db.save_position(position)
            raise RuntimeError("outer failed after inner block closed")
    assert db.load_positions() == []


def test_a_profile_update_never_clobbers_the_tracking_watermark(db):
    """Found live: the registry re-persisted a cached follow cursor on
    every status or score change, so an ordinary discovery update wiped
    the tracker's progress and it re-armed at the newest signature —
    silently skipping every trade in between."""
    profile = TraderProfile(address="t9", status=TraderStatus.FOLLOWED,
                            score=0.5)
    db.upsert_trader(profile)
    db.update_watermarks([("t9", 900, "watermark-sig")])

    profile.score = 0.9                      # an ordinary discovery update
    profile.status = TraderStatus.FOLLOWED
    db.upsert_trader(profile)

    assert db.load_watermarks()["t9"] == (900, "watermark-sig")
