#!/usr/bin/env python3
"""Short-term variant of backtest_regime.py, per an explicit ask to change the
strategy: weekly (5-trading-day) lookback instead of 20-day, no cap on
concurrent position COUNT, an expanded universe including crypto (which
trades 7 days/week, unlike equities), and a hard $3,000 TOTAL dollar cap on
capital deployed at once (split into ~$150 slices so ~20 positions can fit
under that cap instead of a handful of large ones).

Because sizing is now a fixed-dollar-slice-under-a-total-cap scheme rather
than the live system's %-of-equity risk sizing, this bypasses risk.py's
size_position() entirely and implements its own sizing -- still using
storage.py for persistence (so results show up in the dashboard the same
way), just not paper_engine.py's position-opening logic. Runs against its
own isolated DB, separate from both the live paper account and the earlier
6-month regime backtest.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402

BACKTEST_DB = Path(__file__).resolve().parent / "data" / "backtest_shortterm.db"
object.__setattr__(settings, "db_path", BACKTEST_DB)

from app import storage  # noqa: E402

WINDOW = 5          # ~1 trading week
THRESHOLD = 0.02    # 2%, scaled down from the 20d strategy's 5% for a 1wk window
SMA_SHORT = 5
SMA_LONG = 10

TOTAL_BUY_LIMIT = 3000.0
SLICE_SIZE = 150.0  # -> up to ~20 concurrent positions under the total cap

EQUITIES = [
    "NVDA", "MSFT", "AMZN", "JPM", "XOM", "DIS", "CRM", "ADBE", "PYPL", "MRK",
    "AAPL", "SPY", "GOOGL", "META", "AMD", "TSLA", "QQQ", "KO",
    "PLTR", "COIN", "MSTR", "SMCI",  # higher-beta, more short-term-swing-prone
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


def current_open_notional() -> float:
    open_positions = storage.list_positions(status="open")
    return sum(p["qty"] * p["avg_price"] for p in open_positions)


def try_open(symbol: str, price: float, at_iso: str) -> bool:
    deployed = current_open_notional()
    remaining = TOTAL_BUY_LIMIT - deployed
    if remaining < 1.0:
        return False  # total cap reached, signal blocked
    slice_dollars = min(SLICE_SIZE, remaining)
    qty = round(slice_dollars / price, 6)
    if qty <= 0:
        return False
    position_id = storage.open_position(symbol, "long", qty, price, "shortterm-weekly", at=at_iso)
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

    print(f"Short-term backtest: {len(symbols)} symbols ({len(CRYPTO)} crypto), "
          f"{WINDOW}-day window, {THRESHOLD:.0%} threshold, "
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
    print("SHORT-TERM BACKTEST RESULTS (isolated DB, separate from live + earlier 6mo backtest)")
    print("=" * 70)
    print(f"Opens: {opens}   Closes: {closes}   Blocked by $3,000 total cap: {blocked}")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", default="1y", help="yfinance period, e.g. 6mo, 1y, 2y")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    args = parser.parse_args()
    run([s.strip().upper() for s in args.symbols.split(",") if s.strip()], args.period)
