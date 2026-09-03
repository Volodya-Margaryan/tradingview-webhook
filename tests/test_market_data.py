import pytest

from app import market_data


@pytest.mark.parametrize("raw,expected", [
    ("AAPL", "AAPL"),
    ("NASDAQ:AAPL", "AAPL"),
    ("nasdaq:aapl", "AAPL"),
    ("BTCUSD", "BTC-USD"),
    ("BTCUSDT", "BTC-USD"),
    ("ETHUSDC", "ETH-USD"),
    ("BTC-USD", "BTC-USD"),
    ("MSFT", "MSFT"),
])
def test_normalize_symbol(raw, expected):
    assert market_data.normalize_symbol(raw) == expected


def test_price_sanity_note_no_live_price():
    note = market_data.price_sanity_note(100.0, None)
    assert "no live quote" in note


def test_price_sanity_note_within_tolerance():
    note = market_data.price_sanity_note(101.0, 100.0)
    assert note.startswith("OK")


def test_price_sanity_note_flags_large_divergence():
    note = market_data.price_sanity_note(135.0, 225.30)
    assert note.startswith("SUSPICIOUS")
    assert "135" in note
    assert "225.30" in note


def test_get_live_price_returns_none_on_lookup_failure(monkeypatch):
    class _BoomTicker:
        def __init__(self, symbol):
            raise RuntimeError("network unreachable")

    monkeypatch.setattr(market_data.yf, "Ticker", _BoomTicker)
    market_data._cache.clear()
    assert market_data.get_live_price("AAPL") is None


def test_get_live_price_uses_fast_info(monkeypatch):
    class _FakeTicker:
        def __init__(self, symbol):
            self.fast_info = {"lastPrice": 225.3}

    monkeypatch.setattr(market_data.yf, "Ticker", _FakeTicker)
    market_data._cache.clear()
    assert market_data.get_live_price("NVDA") == 225.3


def test_get_live_price_is_cached(monkeypatch):
    calls = {"n": 0}

    class _FakeTicker:
        def __init__(self, symbol):
            calls["n"] += 1
            self.fast_info = {"lastPrice": 100.0}

    monkeypatch.setattr(market_data.yf, "Ticker", _FakeTicker)
    market_data._cache.clear()
    market_data.get_live_price("MSFT")
    market_data.get_live_price("MSFT")
    assert calls["n"] == 1  # second call served from cache, no new lookup
