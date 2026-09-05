#!/usr/bin/env python3
"""Walk-forward backtest of a trend-following regime strategy over real
historical daily prices, reusing the same risk/paper-engine code as live
trading (1% risk per trade, 10-position cap, no pyramiding), against a
fully isolated, throwaway SQLite DB that never touches the live paper
account.

No lookahead: each simulated day's regime signal only uses price data up
to and including that day.

Known limitation: the risk engine's daily-loss circuit breaker filters
"closed in the last 24h" by wall-clock time, not simulated date, so it
isn't temporally accurate during a historical replay (it can't halt
trading on a specific bad day the way it would live). The position cap and
per-trade risk sizing are both still enforced correctly, since neither is
time-windowed.

Usage:
    .venv/bin/python backtest_regime.py [--period 6mo] [--symbols NVDA,MSFT,...]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402

BACKTEST_DB = Path(__file__).resolve().parent / "data" / "backtest.db"
object.__setattr__(settings, "db_path", BACKTEST_DB)

from app import paper_engine, performance, risk, storage  # noqa: E402
from app.schemas import Action  # noqa: E402

WINDOW = 20
THRESHOLD = 0.05

DEFAULT_SYMBOLS = [
    "NVDA", "MSFT", "AMZN", "JPM", "XOM", "DIS", "CRM", "ADBE", "PYPL", "MRK",
    "AAPL", "SPY", "GOOGL", "META", "AMD", "TSLA", "QQQ", "KO",
]


def load_history(symbol: str, period: str) -> pd.DataFrame:
    hist = yf.Ticker(symbol).history(period=period, interval="1d")
    if hist.index.tz is not None:
        hist.index = hist.index.tz_localize(None)
    return hist


def is_clean_bull(close_so_far: pd.Series) -> bool:
    if len(close_so_far) < WINDOW + 1:
        return False
    roll_ret = close_so_far.pct_change(WINDOW).iloc[-1]
    sma20 = close_so_far.rolling(20).mean().iloc[-1]
    sma50 = close_so_far.rolling(50).mean().iloc[-1]
    if pd.isna(roll_ret) or pd.isna(sma20) or pd.isna(sma50):
        return False
    return bool(roll_ret > THRESHOLD and sma20 > sma50)


def run(symbols: list[str], period: str) -> None:
    BACKTEST_DB.parent.mkdir(parents=True, exist_ok=True)
    if BACKTEST_DB.exists():
        BACKTEST_DB.unlink()
    storage._conn = None
    storage.init_db()

    print(f"Backtesting {len(symbols)} symbols over {period}, isolated DB: {BACKTEST_DB}")

    data: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        h = load_history(sym, period)
        if h.empty:
            print(f"  {sym}: no data, skipping")
            continue
        data[sym] = h

    all_dates = sorted(set().union(*(h.index for h in data.values())))
    print(f"Trading days in range: {len(all_dates)} ({all_dates[0].date()} to {all_dates[-1].date()})")

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
                    sym, Action.BUY, price, "backtest-regime", signal_id=None, at=date_iso
                )
                if result.executed:
                    trades_taken += 1
                else:
                    signals_blocked += 1
            elif not bull and existing is not None and existing["side"] == "long":
                result = paper_engine.execute_signal(
                    sym, Action.SELL, price, "backtest-regime", signal_id=None, at=date_iso
                )
                if result.executed:
                    trades_taken += 1

    # Force-close anything still open at the last available price per symbol,
    # so final stats reflect a fully realized outcome, not phantom open risk.
    last_date_iso = all_dates[-1].tz_localize("UTC").isoformat() if all_dates[-1].tzinfo is None else all_dates[-1].isoformat()
    for sym, h in data.items():
        existing = storage.get_open_position(sym)
        if existing:
            last_price = float(h["Close"].iloc[-1])
            paper_engine.execute_signal(
                sym, Action.SELL, last_price, "backtest-regime-final-close",
                signal_id=None, at=last_date_iso,
            )

    stats = performance.compute_performance()
    print()
    print("=" * 70)
    print("BACKTEST RESULTS (isolated account, does not affect live paper trading)")
    print("=" * 70)
    print(f"Signals executed: {trades_taken}   Signals blocked (risk/pyramiding): {signals_blocked}")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", default="6mo", help="yfinance period, e.g. 3mo, 6mo, 1y")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    args = parser.parse_args()
    run([s.strip().upper() for s in args.symbols.split(",") if s.strip()], args.period)
