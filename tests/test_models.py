import time

from olala.domain.models import Position, PositionStatus, TraderStats


def test_win_rate_guards_division_by_zero():
    stats = TraderStats(address="t1")
    assert stats.win_rate == 0.0


def test_win_rate_and_history_days():
    now = time.time()
    stats = TraderStats(address="t1", first_trade_at=now - 100 * 86_400,
                        last_trade_at=now, closed_round_trips=10, wins=7)
    assert stats.win_rate == 0.7
    assert 99.9 < stats.history_days < 100.1
    assert stats.inactive_hours < 0.1


def test_position_pnl_math():
    position = Position.open_new(
        wallet_id="w1", trader="t1", mint="m1", symbol="TOK",
        quantity=200.0, price_sol=0.01, sol_invested=2.0)
    position.last_price_sol = 0.012
    assert abs(position.market_value_sol - 2.4) < 1e-9
    assert abs(position.unrealized_pnl_sol - 0.4) < 1e-9

    position.status = PositionStatus.CLOSED
    assert position.unrealized_pnl_sol == 0.0


def test_position_to_dict_carries_derived_fields():
    position = Position.open_new("w1", "t1", "m1", "TOK", 100, 0.01, 1.0)
    d = position.to_dict()
    assert d["status"] == "open"
    assert "market_value_sol" in d and "unrealized_pnl_sol" in d
