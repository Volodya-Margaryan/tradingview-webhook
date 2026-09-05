#!/usr/bin/env python3
"""Bracket-exit strategy (20-day window, 5% threshold, SMA20>SMA50) with
capital capped at $3,000 total instead of scaling with account equity,
split into 10 slots of $300 each. Runs over a 3-month period and a broader
universe that adds small/mid-cap speculative names and altcoins alongside
the core large-cap universe. Isolated DB, separate from live trading and
other backtests.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402

BACKTEST_DB = Path(__file__).resolve().parent / "data" / "backtest_realistic.db"
object.__setattr__(settings, "db_path", BACKTEST_DB)

from app import storage  # noqa: E402

WINDOW = 20          # the parameter set that actually worked (profit factor 1.67)
THRESHOLD = 0.05
SMA_SHORT = 20
SMA_LONG = 50

TOTAL_BUY_LIMIT = 3000.0
SLICE_SIZE = 300.0   # -> 10 slots, mirrors the original 10-position cap

BLUE_CHIPS = [
    "NVDA", "MSFT", "AMZN", "JPM", "XOM", "DIS", "CRM", "ADBE", "PYPL", "MRK",
    "AAPL", "SPY", "GOOGL", "META", "AMD", "TSLA", "QQQ", "KO",
]
SPECULATIVE = [
    "RIVN", "SOFI", "PLUG", "RIOT", "MARA", "UPST", "RKLB", "IONQ", "LCID",
    "NIO", "CHPT", "SOUN", "IREN", "ASTS", "AFRM", "PLTR", "COIN", "MSTR", "SMCI",
]
CRYPTO_MAJOR = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "ADA-USD"]
CRYPTO_ALT = ["AVAX-USD", "LINK-USD", "LTC-USD", "UNI-USD", "ATOM-USD"]
DEFAULT_SYMBOLS = BLUE_CHIPS + SPECULATIVE + CRYPTO_MAJOR + CRYPTO_ALT


def load_history(symbol: str, period: str) -> pd.DataFrame:
    hist = yf.Ticker(symbol).history(period=period, interval="1d")
    if not hist.empty and getattr(hist.index, "tz", None) is not None:
        hist.index = hist.index.tz_localize(None)
    return hist


def is_clean_bull(close_so_far: pd.Series) -> bool:
    if len(close_so_far) < WINDOW + 1:
        return False
    roll_ret = close_so_far.pct_change(WINDOW).iloc[-1]
    sma_s = close_so_far.rolling(SMA_SHORT).mean().iloc[-1]
    sma_l = close_so_far.rolling(SMA_LONG).mean().iloc[-1]
    if pd.isna(roll_ret) or pd.isna(sma_s) or pd.isna(sma_l):
        return False
    return bool(roll_ret > THRESHOLD and sma_s > sma_l)


def current_open_notional() -> float:
    return sum(p["qty"] * p["avg_price"] for p in storage.list_positions(status="open"))


def try_open(symbol: str, price: float, at_iso: str) -> bool:
    remaining = TOTAL_BUY_LIMIT - current_open_notional()
    if remaining < 1.0:
        return False
    slice_dollars = min(SLICE_SIZE, remaining)
    qty = round(slice_dollars / price, 6)
    if qty <= 0:
        return False
    position_id = storage.open_position(symbol, "long", qty, price, "realistic-3mo-3k-cap", at=at_iso)
    storage.record_trade(None, position_id, symbol, "BUY", qty, price, notes="open", at=at_iso)
    storage.adjust_cash(-price * qty)
    return True


def close(position: dict, price: float, at_iso: str) -> None:
    qty = position["qty"]
    entry = position["avg_price"]
    realized_pnl = (price - entry) * qty
    storage.close_position(position["id"], price, realized_pnl, at=at_iso)
    storage.add_realized_pnl(realized_pnl)
    storage.record_trade(None, position["id"], position["symbol"], "SELL", qty, price,
                          notes="close", at=at_iso)
    storage.adjust_cash(price * qty)


def run(symbols: list[str], period: str) -> None:
    BACKTEST_DB.parent.mkdir(parents=True, exist_ok=True)
    if BACKTEST_DB.exists():
        BACKTEST_DB.unlink()
    storage._conn = None
    storage.init_db()

    print(f"Realistic-capital backtest: {len(symbols)} symbols "
          f"({len(SPECULATIVE)} speculative equities, {len(CRYPTO_MAJOR)+len(CRYPTO_ALT)} crypto), "
          f"{WINDOW}-day window (the proven parameter set), "
          f"${TOTAL_BUY_LIMIT:.0f} total cap in ${SLICE_SIZE:.0f} slices, over {period}")
    print(f"Isolated DB: {BACKTEST_DB}")

    data: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        h = load_history(sym, period)
        if h.empty:
            print(f"  {sym}: no data, skipping")
            continue
        data[sym] = h

    all_dates = sorted(set().union(*(h.index for h in data.values())))
    print(f"Calendar days in range: {len(all_dates)} ({all_dates[0].date()} to {all_dates[-1].date()})")

    opens = closes = blocked = 0

    for date in all_dates:
        date_iso = date.tz_localize("UTC").isoformat() if date.tzinfo is None else date.isoformat()
        for sym, h in data.items():
            if date not in h.index:
                continue
            idx = h.index.get_loc(date)
            close_so_far = h["Close"].iloc[: idx + 1]
            price = float(h["Close"].iloc[idx])
            bull = is_clean_bull(close_so_far)

            existing = storage.get_open_position(sym)
            if bull and existing is None:
                if try_open(sym, price, date_iso):
                    opens += 1
                else:
                    blocked += 1
            elif not bull and existing is not None:
                close(existing, price, date_iso)
                closes += 1

    last_date_iso = (
        all_dates[-1].tz_localize("UTC").isoformat()
        if all_dates[-1].tzinfo is None else all_dates[-1].isoformat()
    )
    for sym, h in data.items():
        existing = storage.get_open_position(sym)
        if existing:
            last_price = float(h["Close"].iloc[-1])
            close(existing, last_price, last_date_iso)
            closes += 1

    from app import performance
    stats = performance.compute_performance()
    print()
    print("=" * 70)
    print("REALISTIC-CAPITAL BACKTEST RESULTS ($3,000 cap, 3mo, broader universe)")
    print("=" * 70)
    print(f"Opens: {opens}   Closes: {closes}   Blocked by $3,000 total cap: {blocked}")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", default="3mo", help="yfinance period, e.g. 3mo, 6mo")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    args = parser.parse_args()
    run([s.strip().upper() for s in args.symbols.split(",") if s.strip()], args.period)
