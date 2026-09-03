from app import dedup, storage


def _log_accepted(symbol="AAPL", action="BUY"):
    return storage.log_signal(
        symbol=symbol, price=100.0, timeframe="5", action=action, strategy="s",
        status="accepted", reason=None, raw_payload={},
    )


def test_no_prior_signal_is_not_duplicate():
    assert dedup.is_duplicate("AAPL", "BUY") is False


def test_repeat_within_window_is_duplicate():
    _log_accepted("AAPL", "BUY")
    assert dedup.is_duplicate("AAPL", "BUY") is True


def test_different_action_is_not_duplicate():
    _log_accepted("AAPL", "BUY")
    assert dedup.is_duplicate("AAPL", "SELL") is False


def test_different_symbol_is_not_duplicate():
    _log_accepted("AAPL", "BUY")
    assert dedup.is_duplicate("MSFT", "BUY") is False


def test_rejected_signals_do_not_count_as_prior():
    storage.log_signal(
        symbol="AAPL", price=100.0, timeframe="5", action="BUY", strategy="s",
        status="rejected", reason="bad secret", raw_payload={},
    )
    assert dedup.is_duplicate("AAPL", "BUY") is False
