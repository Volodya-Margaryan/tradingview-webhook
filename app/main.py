"""TradingView webhook receiver.

POST /webhook          - receives TradingView alert JSON, validates, executes
                          paper trades. No real orders are ever placed.
GET  /health            - liveness check
GET  /status             - kill switch state + account summary
GET  /signals            - recent signals (accepted + rejected)
GET  /positions          - open/closed paper positions
GET  /trades             - simulated fills
GET  /performance         - paper-trading performance stats (incl. unrealized P&L)
GET  /quotes              - live (delayed) market quotes, informational only
GET  /dashboard           - simple HTML/JS inspection UI
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from . import dedup, killswitch, market_data, paper_engine, storage
from .config import settings
from .logging_conf import signal_logger
from .schemas import WebhookAlert

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    storage.init_db()
    if not settings.webhook_secret:
        signal_logger.warning(
            "WEBHOOK_SECRET is not set — every incoming webhook will be rejected. "
            "Set it in .env."
        )
    yield


app = FastAPI(title="TradingView Paper-Trading Webhook Receiver", lifespan=lifespan)


def _redact(payload: dict) -> dict:
    redacted = dict(payload)
    if "secret" in redacted:
        redacted["secret"] = "***"
    return redacted


def _reject(
    *,
    http_status: int,
    reason: str,
    raw_payload: dict,
    symbol: Optional[str] = None,
    price: Optional[float] = None,
    timeframe: Optional[str] = None,
    action: Optional[str] = None,
    strategy: Optional[str] = None,
) -> JSONResponse:
    signal_id = storage.log_signal(
        symbol=symbol,
        price=price,
        timeframe=timeframe,
        action=action,
        strategy=strategy,
        status="rejected",
        reason=reason,
        raw_payload=_redact(raw_payload),
    )
    signal_logger.warning("REJECTED signal #%s: %s | payload=%s", signal_id, reason, _redact(raw_payload))
    return JSONResponse(
        status_code=http_status,
        content={"status": "rejected", "reason": reason, "signal_id": signal_id},
    )


@app.post("/webhook")
async def receive_webhook(request: Request) -> Response:
    raw_body = await request.body()

    try:
        raw_payload = json.loads(raw_body)
        if not isinstance(raw_payload, dict):
            raise ValueError("payload must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        return _reject(
            http_status=400,
            reason=f"invalid JSON body: {exc}",
            raw_payload={"_raw": raw_body.decode("utf-8", errors="replace")[:2000]},
        )

    # --- 1. shared secret required on every request ---
    provided_secret = raw_payload.get("secret")
    from .security import is_valid_secret  # local import avoids import cycle on startup

    if not is_valid_secret(provided_secret):
        return _reject(
            http_status=401,
            reason="missing or invalid shared secret",
            raw_payload=raw_payload,
            symbol=raw_payload.get("symbol"),
            action=raw_payload.get("action"),
        )

    # --- 2. schema / type / value validation ---
    try:
        alert = WebhookAlert(**raw_payload)
    except ValidationError as exc:
        errors = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        return _reject(
            http_status=422,
            reason="validation failed: " + "; ".join(errors),
            raw_payload=raw_payload,
            symbol=raw_payload.get("symbol"),
            action=raw_payload.get("action"),
        )

    # --- 3. kill switch ---
    if killswitch.is_active():
        return _reject(
            http_status=200,
            reason="kill switch active — signal processing halted",
            raw_payload=raw_payload,
            symbol=alert.symbol,
            price=alert.price,
            timeframe=alert.timeframe,
            action=alert.action.value,
            strategy=alert.strategy,
        )

    # --- 4. duplicate-signal protection ---
    if dedup.is_duplicate(alert.symbol, alert.action.value):
        return _reject(
            http_status=200,
            reason=f"duplicate signal for {alert.symbol}/{alert.action.value} "
            f"within {settings.dedup_window_seconds}s window",
            raw_payload=raw_payload,
            symbol=alert.symbol,
            price=alert.price,
            timeframe=alert.timeframe,
            action=alert.action.value,
            strategy=alert.strategy,
        )

    # --- accepted: log (with an informational live-price sanity check —
    # never blocks or delays execution beyond a 3s timeout, never changes
    # the price actually used to fill the paper trade) ---
    live_price = await market_data.get_live_price_async(alert.symbol)
    price_check = market_data.price_sanity_note(alert.price, live_price)

    signal_id = storage.log_signal(
        symbol=alert.symbol,
        price=alert.price,
        timeframe=alert.timeframe,
        action=alert.action.value,
        strategy=alert.strategy,
        status="accepted",
        reason=None,
        raw_payload=_redact(raw_payload),
        price_check=price_check,
    )
    signal_logger.info(
        "ACCEPTED signal #%s: %s %s @ %s (%s, tf=%s) | price check: %s",
        signal_id, alert.action.value, alert.symbol, alert.price, alert.strategy,
        alert.timeframe, price_check,
    )
    if price_check.startswith("SUSPICIOUS"):
        signal_logger.warning("Signal #%s price check flagged: %s", signal_id, price_check)

    result = paper_engine.execute_signal(
        symbol=alert.symbol,
        action=alert.action,
        price=alert.price,
        strategy=alert.strategy,
        signal_id=signal_id,
    )
    signal_logger.info("Execution for signal #%s: %s", signal_id, result)

    return JSONResponse(
        status_code=200,
        content={
            "status": "accepted",
            "signal_id": signal_id,
            "price_check": price_check,
            "execution": {
                "executed": result.executed,
                "reason": result.reason,
                "side": result.side,
                "qty": result.qty,
                "price": result.price,
                "realized_pnl": result.realized_pnl,
            },
        },
    )


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/status")
def status() -> dict:
    return {
        "kill_switch_active": killswitch.is_active(),
        "kill_switch_env": settings.kill_switch_env,
        "kill_switch_runtime": storage.is_kill_switch_on(),
        "account": storage.get_account_summary(),
        "dedup_window_seconds": settings.dedup_window_seconds,
        "risk": {
            "max_risk_per_trade_pct": settings.max_risk_per_trade_pct,
            "max_position_notional_pct": settings.max_position_notional_pct,
            "max_open_positions": settings.max_open_positions,
            "max_daily_loss_pct": settings.max_daily_loss_pct,
            "allow_shorts": settings.allow_shorts,
        },
    }


@app.get("/signals")
def signals(limit: int = 50) -> list[dict]:
    return storage.list_signals(limit=limit)


@app.get("/positions")
def positions(status_filter: Optional[str] = None, live: bool = False) -> list[dict]:
    rows = storage.list_positions(status=status_filter)
    if live and rows:
        from . import performance as perf_module

        rows = perf_module.enrich_positions_with_quotes(rows)
    return rows


@app.get("/quotes")
async def quotes(symbols: Optional[str] = None) -> dict:
    """Live (delayed, informational-only) quotes. Defaults to symbols of
    currently open positions if none are given."""
    if symbols:
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    else:
        symbol_list = sorted({p["symbol"] for p in storage.list_positions(status="open")})

    results = await asyncio.gather(
        *(market_data.get_live_price_async(s) for s in symbol_list)
    )
    return {
        "quotes": dict(zip(symbol_list, results)),
        "note": "delayed quotes (Yahoo Finance), informational only — not used for trade execution",
    }


@app.get("/trades")
def trades(limit: int = 100) -> list[dict]:
    return storage.list_trades(limit=limit)


@app.get("/performance")
def performance() -> dict:
    from . import performance as perf_module

    stats = perf_module.compute_performance()
    open_positions = storage.list_positions(status="open")
    enriched = perf_module.enrich_positions_with_quotes(open_positions)
    stats.update(perf_module.compute_unrealized_summary(enriched))
    return stats


@app.get("/session")
def session() -> dict:
    return {"marker": storage.get_session_marker()}


@app.get("/dashboard")
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")
