from fastapi.testclient import TestClient

from app import killswitch
from app.main import app

client = TestClient(app)

VALID = {
    "secret": "test-secret-123",
    "symbol": "AAPL",
    "price": 190.5,
    "timeframe": "5",
    "action": "BUY",
    "strategy": "ema-cross",
}


def test_missing_secret_returns_401():
    payload = {k: v for k, v in VALID.items() if k != "secret"}
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 401
    assert resp.json()["status"] == "rejected"


def test_wrong_secret_returns_401():
    resp = client.post("/webhook", json={**VALID, "secret": "nope"})
    assert resp.status_code == 401


def test_malformed_payload_returns_422():
    resp = client.post("/webhook", json={**VALID, "action": "HOLD"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == "rejected"
    assert "action" in body["reason"]


def test_invalid_json_body_returns_400():
    resp = client.post("/webhook", content=b"not json", headers={"content-type": "application/json"})
    assert resp.status_code == 400


def test_valid_signal_is_accepted_and_executed():
    resp = client.post("/webhook", json=VALID)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["execution"]["executed"] is True


def test_duplicate_signal_within_window_is_ignored():
    client.post("/webhook", json=VALID)
    resp = client.post("/webhook", json=VALID)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected"
    assert "duplicate" in body["reason"]


def test_kill_switch_blocks_processing():
    killswitch.set_active(True)
    try:
        resp = client.post("/webhook", json=VALID)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "rejected"
        assert "kill switch" in body["reason"]
    finally:
        killswitch.set_active(False)


def test_status_and_inspection_endpoints_work():
    client.post("/webhook", json=VALID)
    assert client.get("/status").status_code == 200
    assert client.get("/signals").status_code == 200
    assert client.get("/positions").status_code == 200
    assert client.get("/trades").status_code == 200
    assert client.get("/performance").status_code == 200
    assert client.get("/quotes").status_code == 200
    assert client.get("/session").status_code == 200


def test_suspicious_price_is_flagged_but_still_executes(monkeypatch):
    from app import market_data

    async def _fake_live_price(symbol):
        return 999.0  # wildly different from VALID's price=190.5

    monkeypatch.setattr(market_data, "get_live_price_async", _fake_live_price)

    resp = client.post("/webhook", json=VALID)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["price_check"].startswith("SUSPICIOUS")
    # the flag is informational only — execution still goes through at the
    # submitted price, never silently substituted with the live quote
    assert body["execution"]["executed"] is True
    assert body["execution"]["price"] == 190.5

    signals = client.get("/signals?limit=1").json()
    assert signals[0]["price_check"].startswith("SUSPICIOUS")
