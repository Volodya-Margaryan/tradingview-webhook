import pytest
from pydantic import ValidationError

from app.schemas import WebhookAlert

VALID = {
    "secret": "test-secret-123",
    "symbol": "AAPL",
    "price": 190.5,
    "timeframe": "5",
    "action": "BUY",
    "strategy": "ema-cross",
}


def test_valid_payload_parses():
    alert = WebhookAlert(**VALID)
    assert alert.symbol == "AAPL"
    assert alert.action.value == "BUY"


def test_exchange_prefixed_symbol_ok():
    alert = WebhookAlert(**{**VALID, "symbol": "nasdaq:aapl"})
    assert alert.symbol == "NASDAQ:AAPL"


@pytest.mark.parametrize("bad_symbol", ["", "AAPL!", "TOO-LONG-SYMBOL-NAME-HERE", "$$$"])
def test_invalid_symbol_rejected(bad_symbol):
    with pytest.raises(ValidationError):
        WebhookAlert(**{**VALID, "symbol": bad_symbol})


def test_negative_price_rejected():
    with pytest.raises(ValidationError):
        WebhookAlert(**{**VALID, "price": -5})


def test_zero_price_rejected():
    with pytest.raises(ValidationError):
        WebhookAlert(**{**VALID, "price": 0})


def test_invalid_action_rejected():
    with pytest.raises(ValidationError):
        WebhookAlert(**{**VALID, "action": "HOLD"})


def test_empty_strategy_rejected():
    with pytest.raises(ValidationError):
        WebhookAlert(**{**VALID, "strategy": ""})


def test_missing_secret_rejected():
    payload = {k: v for k, v in VALID.items() if k != "secret"}
    with pytest.raises(ValidationError):
        WebhookAlert(**payload)


def test_bad_timeframe_rejected():
    with pytest.raises(ValidationError):
        WebhookAlert(**{**VALID, "timeframe": "way-too-long-tf"})


def test_extra_fields_ignored():
    alert = WebhookAlert(**{**VALID, "exchange": "NASDAQ", "note": "extra field TradingView might add"})
    assert alert.symbol == "AAPL"
