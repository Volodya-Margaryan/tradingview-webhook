import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("WEBHOOK_SECRET", "test-secret-123")
os.environ.setdefault("STARTING_EQUITY", "100000")
os.environ.setdefault("DEDUP_WINDOW_SECONDS", "30")
os.environ.setdefault("MAX_OPEN_POSITIONS", "10")
os.environ.setdefault("MAX_RISK_PER_TRADE_PCT", "1.0")
os.environ.setdefault("MAX_POSITION_NOTIONAL_PCT", "20.0")
os.environ.setdefault("MAX_DAILY_LOSS_PCT", "5.0")
os.environ.setdefault("ALLOW_SHORTS", "false")
os.environ.setdefault("KILL_SWITCH", "false")

from app import storage  # noqa: E402
from app.config import settings  # noqa: E402

# Redirect logging to a throwaway file BEFORE anything creates the shared
# signal logger (app.main binds it at import time, which happens as soon as
# pytest collects test_webhook_api.py). Without this, running the test suite
# writes fake test signal lines into the real production webhook.log.
_TEST_LOG_DIR = Path(tempfile.mkdtemp(prefix="tv-webhook-test-logs-"))
object.__setattr__(settings, "log_path", _TEST_LOG_DIR / "webhook.log")
settings.log_path.parent.mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Give every test a fresh, throwaway SQLite DB so tests never touch real
    data. storage._get_conn() auto-reconnects whenever settings.db_path
    differs from a thread's cached connection, so this is enough even though
    TestClient requests may run on reused threadpool worker threads."""
    db_path = tmp_path / "test.db"
    object.__setattr__(settings, "db_path", db_path)
    storage.init_db()
    yield


@pytest.fixture(autouse=True)
def no_network_market_data(monkeypatch):
    """Keep the suite hermetic: nothing here should depend on reaching Yahoo
    Finance over the network. Patches at the network boundary (yf.Ticker),
    not the higher-level get_live_price[/_async] functions themselves — so
    the real caching/error-handling logic in market_data.py still runs (and
    is what test_market_data.py exercises), it just never makes a real HTTP
    call unless a test explicitly re-patches yf.Ticker with its own fake."""
    from app import market_data

    class _UnreachableTicker:
        def __init__(self, symbol):
            raise RuntimeError("network disabled in test suite")

    monkeypatch.setattr(market_data.yf, "Ticker", _UnreachableTicker)
    market_data._cache.clear()
    yield
    market_data._cache.clear()
