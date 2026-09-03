from app import paper_engine, storage
from app.schemas import Action


def test_buy_on_flat_opens_long():
    result = paper_engine.execute_signal("AAPL", Action.BUY, 100.0, "s", signal_id=None)
    assert result.executed is True
    assert result.side == "BUY"
    pos = storage.get_open_position("AAPL")
    assert pos is not None
    assert pos["side"] == "long"


def test_buy_when_long_already_open_is_rejected():
    paper_engine.execute_signal("AAPL", Action.BUY, 100.0, "s", signal_id=None)
    result = paper_engine.execute_signal("AAPL", Action.BUY, 105.0, "s", signal_id=None)
    assert result.executed is False
    assert "already open" in result.reason


def test_sell_closes_long_with_correct_pnl():
    open_result = paper_engine.execute_signal("AAPL", Action.BUY, 100.0, "s", signal_id=None)
    qty = open_result.qty
    close_result = paper_engine.execute_signal("AAPL", Action.SELL, 110.0, "s", signal_id=None)
    assert close_result.executed is True
    assert close_result.realized_pnl == round((110.0 - 100.0) * qty, 6)
    assert storage.get_open_position("AAPL") is None


def test_sell_on_flat_without_shorts_enabled_is_rejected():
    result = paper_engine.execute_signal("MSFT", Action.SELL, 300.0, "s", signal_id=None)
    assert result.executed is False
    assert "ALLOW_SHORTS" in result.reason


def test_cash_decreases_on_buy_and_increases_on_sell():
    cash_before = storage.get_cash()
    open_result = paper_engine.execute_signal("AAPL", Action.BUY, 100.0, "s", signal_id=None)
    cash_after_buy = storage.get_cash()
    assert cash_after_buy == cash_before - (open_result.qty * 100.0)

    paper_engine.execute_signal("AAPL", Action.SELL, 110.0, "s", signal_id=None)
    cash_after_sell = storage.get_cash()
    assert cash_after_sell == cash_after_buy + (open_result.qty * 110.0)
