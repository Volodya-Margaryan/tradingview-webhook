from app import risk, storage
from app.config import settings


def test_sizing_respects_risk_pct_of_equity():
    result = risk.size_position(price=100.0)
    assert result.approved is True
    # equity=100000, risk 1% -> notional 1000, cap 20% -> 20000, min = 1000
    assert result.qty == 10.0


def test_sizing_blocked_when_max_open_positions_reached():
    for i in range(settings.max_open_positions):
        storage.open_position(f"SYM{i}", "long", 1.0, 100.0, "s")
    result = risk.size_position(price=100.0)
    assert result.approved is False
    assert "max open positions" in result.reason


def test_sizing_blocked_by_daily_loss_circuit_breaker():
    pos_id = storage.open_position("AAPL", "long", 100.0, 100.0, "s")
    big_loss = -(settings.starting_equity * settings.max_daily_loss_pct / 100) - 1
    storage.close_position(pos_id, 50.0, big_loss)
    result = risk.size_position(price=100.0)
    assert result.approved is False
    assert "daily loss limit" in result.reason


def test_sizing_caps_to_available_cash():
    # spend almost all cash on one big position, leaving little cash
    storage.adjust_cash(-(storage.get_cash() - 50))
    result = risk.size_position(price=10.0)
    assert result.approved is True
    assert result.qty * 10.0 <= 50.0 + 1e-9
