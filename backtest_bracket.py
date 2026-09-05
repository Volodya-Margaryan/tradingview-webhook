#!/usr/bin/env python3
"""Bracket-exit strategy: trend entry (20-day rolling return above +5%,
price above both its 20- and 50-day moving average) combined with a fixed
stop-loss and take-profit, checked once per day against the closing price.

Exit rules, whichever triggers first:
  - -3% stop-loss
  - +6% take-profit
  - trend reversal (price falls back below its moving averages)

$3,000 total capital cap, ten $300 slices, 18-symbol liquid large-cap
universe. Runs against an isolated SQLite DB, separate from the live paper
account.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402

BACKTEST_DB = Path(__file__).resolve().parent / "data" / "backtest_bracket.db"
object.__setattr__(settings, "db_path", BACKTEST_DB)

from app import storage  # noqa: E402

WINDOW = 20
THRESHOLD = 0.05
SMA_SHORT = 20
SMA_LONG = 50

STOP_LOSS_PCT = -0.03    # cut losers fast
TAKE_PROFIT_PCT = 0.06   # lock in winners fast, 2:1 reward:risk

TOTAL_BUY_LIMIT = 3000.0
SLICE_SIZE = 300.0

# Round-trip transaction cost model: each side (buy and sell) fills at a
# worse price than the quoted close by FRICTION_BPS basis points, modeling
# bid-ask spread + slippage. 0 = the original frictionless assumption.
# Set at runtime via --friction-bps; module-level default kept for direct use.
FRICTION_BPS = 0.0


def buy_fill_price(quoted_price: float) -> float:
    return quoted_price * (1 + FRICTION_BPS / 10_000)


def sell_fill_price(quoted_price: float) -> float:
    return quoted_price * (1 - FRICTION_BPS / 10_000)

SYMBOLS = [
    "NVDA", "MSFT", "AMZN", "JPM", "XOM", "DIS", "CRM", "ADBE", "PYPL", "MRK",
    "AAPL", "SPY", "GOOGL", "META", "AMD", "TSLA", "QQQ", "KO",
]


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
    fill = buy_fill_price(price)
    slice_dollars = min(SLICE_SIZE, remaining)
    qty = round(slice_dollars / fill, 6)
    if qty <= 0:
        return False
    position_id = storage.open_position(symbol, "long", qty, fill, "bracket-3k-cap", at=at_iso)
    storage.record_trade(None, position_id, symbol, "BUY", qty, fill, notes="open", at=at_iso)
    storage.adjust_cash(-fill * qty)
    return True


def close(position: dict, price: float, at_iso: str, reason: str) -> None:
    qty = position["qty"]
    entry = position["avg_price"]
    fill = sell_fill_price(price)
    realized_pnl = (fill - entry) * qty
    storage.close_position(position["id"], fill, realized_pnl, at=at_iso)
    storage.add_realized_pnl(realized_pnl)
    storage.record_trade(None, position["id"], position["symbol"], "SELL", qty, fill,
                          notes=reason, at=at_iso)
    storage.adjust_cash(fill * qty)


def run(symbols: list[str], period: str) -> None:
    BACKTEST_DB.parent.mkdir(parents=True, exist_ok=True)
    if BACKTEST_DB.exists():
        BACKTEST_DB.unlink()
    storage._conn = None
    storage.init_db()

    print(f"Bracket-exit backtest: {len(symbols)} symbols, {WINDOW}-day entry window, "
          f"stop={STOP_LOSS_PCT:.0%} target={TAKE_PROFIT_PCT:.0%}, "
          f"${TOTAL_BUY_LIMIT:.0f} cap in ${SLICE_SIZE:.0f} slices, over {period}")
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

    opens = closes = blocked = stopped_out = took_profit = regime_exits = 0
    hold_days: list[int] = []
    open_since: dict[int, pd.Timestamp] = {}

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
            if existing is not None:
                entry = existing["avg_price"]
                pct = (price - entry) / entry
                if pct <= STOP_LOSS_PCT:
                    close(existing, price, date_iso, "stop-loss")
                    closes += 1
                    stopped_out += 1
                    hold_days.append((date - open_since.pop(existing["id"], date)).days)
                elif pct >= TAKE_PROFIT_PCT:
                    close(existing, price, date_iso, "take-profit")
                    closes += 1
                    took_profit += 1
                    hold_days.append((date - open_since.pop(existing["id"], date)).days)
                elif not bull:
                    close(existing, price, date_iso, "regime-flip")
                    closes += 1
                    regime_exits += 1
                    hold_days.append((date - open_since.pop(existing["id"], date)).days)
                continue

            if bull:
                open_notional_before = current_open_notional()
                if try_open(sym, price, date_iso):
                    opens += 1
                    new_pos = storage.get_open_position(sym)
                    if new_pos:
                        open_since[new_pos["id"]] = date
                else:
                    blocked += 1

    last_date_iso = (
        all_dates[-1].tz_localize("UTC").isoformat()
        if all_dates[-1].tzinfo is None else all_dates[-1].isoformat()
    )
    for sym, h in data.items():
        existing = storage.get_open_position(sym)
        if existing:
            last_price = float(h["Close"].iloc[-1])
            close(existing, last_price, last_date_iso, "final-close")
            closes += 1
            if existing["id"] in open_since:
                hold_days.append((all_dates[-1] - open_since.pop(existing["id"])).days)

    from app import performance
    stats = performance.compute_performance()
    avg_hold = sum(hold_days) / len(hold_days) if hold_days else None
    print()
    print("=" * 70)
    print("BRACKET-EXIT BACKTEST RESULTS")
    print("=" * 70)
    print(f"Opens: {opens}   Closes: {closes}   Blocked by $3,000 cap: {blocked}")
    print(f"Exit reasons -> stop-loss: {stopped_out}   take-profit: {took_profit}   regime-flip: {regime_exits}")
    print(f"Average holding period: {avg_hold:.1f} calendar days" if avg_hold else "no closed trades")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", default="6mo")
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--friction-bps", type=float, default=0.0,
                         help="per-side cost in basis points (bid-ask spread + slippage); "
                              "0 = frictionless, 5-10 = typical liquid large-cap, 20+ = conservative")
    args = parser.parse_args()
    FRICTION_BPS = args.friction_bps
    run([s.strip().upper() for s in args.symbols.split(",") if s.strip()], args.period)
