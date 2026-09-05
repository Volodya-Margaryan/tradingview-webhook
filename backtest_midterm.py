#!/usr/bin/env python3
"""Bracket-exit strategy with a 2-week (10-trading-day) entry window
instead of 20 days, using the same risk-based percent-of-equity position
sizing and cap as the live trading engine (see app/risk.py). Universe
includes crypto. Isolated DB, separate from live trading and other
backtests.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402

BACKTEST_DB = Path(__file__).resolve().parent / "data" / "backtest_midterm.db"
object.__setattr__(settings, "db_path", BACKTEST_DB)

from app import paper_engine, performance, storage  # noqa: E402
from app.schemas import Action  # noqa: E402

WINDOW = 10          # ~2 trading weeks
THRESHOLD = 0.03     # 3%, between the 20d strategy's 5% and the weekly 2%
SMA_SHORT = 10
SMA_LONG = 20

EQUITIES = [
    "NVDA", "MSFT", "AMZN", "JPM", "XOM", "DIS", "CRM", "ADBE", "PYPL", "MRK",
    "AAPL", "SPY", "GOOGL", "META", "AMD", "TSLA", "QQQ", "KO",
    "PLTR", "COIN", "MSTR", "SMCI",
]
CRYPTO = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "ADA-USD"]
DEFAULT_SYMBOLS = EQUITIES + CRYPTO


def load_history(symbol: str, period: str) -> pd.DataFrame:
    hist = yf.Ticker(symbol).history(period=period, interval="1d")
    if hist.index.tz is not None:
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


def run(symbols: list[str], period: str) -> None:
    BACKTEST_DB.parent.mkdir(parents=True, exist_ok=True)
    if BACKTEST_DB.exists():
        BACKTEST_DB.unlink()
    storage._conn = None
    storage.init_db()

    print(f"Mid-term backtest: {len(symbols)} symbols ({len(CRYPTO)} crypto), "
          f"{WINDOW}-day window, {THRESHOLD:.0%} threshold, "
          f"risk-based sizing (1% risk/trade, max {settings.max_open_positions} positions), over {period}")
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

    trades_taken = 0
    signals_blocked = 0

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
                result = paper_engine.execute_signal(
                    sym, Action.BUY, price, "backtest-midterm-2wk", signal_id=None, at=date_iso
                )
                if result.executed:
                    trades_taken += 1
                else:
                    signals_blocked += 1
            elif not bull and existing is not None and existing["side"] == "long":
                result = paper_engine.execute_signal(
                    sym, Action.SELL, price, "backtest-midterm-2wk", signal_id=None, at=date_iso
                )
                if result.executed:
                    trades_taken += 1

    last_date_iso = (
        all_dates[-1].tz_localize("UTC").isoformat()
        if all_dates[-1].tzinfo is None else all_dates[-1].isoformat()
    )
    for sym, h in data.items():
        existing = storage.get_open_position(sym)
        if existing:
            last_price = float(h["Close"].iloc[-1])
            paper_engine.execute_signal(
                sym, Action.SELL, last_price, "backtest-midterm-final-close",
                signal_id=None, at=last_date_iso,
            )

    stats = performance.compute_performance()
    print()
    print("=" * 70)
    print("MID-TERM (2-WEEK) BACKTEST RESULTS (isolated DB)")
    print("=" * 70)
    print(f"Signals executed: {trades_taken}   Signals blocked (risk/cap/pyramiding): {signals_blocked}")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", default="1y", help="yfinance period, e.g. 6mo, 1y, 2y")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    args = parser.parse_args()
    run([s.strip().upper() for s in args.symbols.split(",") if s.strip()], args.period)
