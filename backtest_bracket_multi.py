#!/usr/bin/env python3
"""Run the EXACT SAME bracket-exit strategy (20-day entry window, 5%
threshold, SMA20>SMA50, -3% stop / +6% target, $3,000 total cap, 10bps
realistic friction) across multiple independent, non-overlapping 6-month
historical windows -- no re-tuning, just checking whether the one result we
had is stable or was a one-time fluke on a favorable stretch.

Each period gets its own isolated DB and its own dashboard port, so every
buy/sell -- real symbol, real date, real price, real dollar amount spent --
is independently inspectable and verifiable in the browser.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402

WINDOW = 20
THRESHOLD = 0.05
SMA_SHORT = 20
SMA_LONG = 50
STOP_LOSS_PCT = -0.03
TAKE_PROFIT_PCT = 0.06
TOTAL_BUY_LIMIT = 2000.0   # lowered from $3,000 per request
SLICE_SIZE = 200.0         # scaled proportionally, still 10 slots
FRICTION_BPS = 10.0

SYMBOLS = [
    "NVDA", "MSFT", "AMZN", "JPM", "XOM", "DIS", "CRM", "ADBE", "PYPL", "MRK",
    "AAPL", "SPY", "GOOGL", "META", "AMD", "TSLA", "QQQ", "KO",
]

N_PERIODS = 14  # non-overlapping ~6-month windows, most recent first
HISTORY_PERIOD = "8y"
DB_PREFIX = "backtest_bracket2k"


def buy_fill_price(p: float) -> float:
    return p * (1 + FRICTION_BPS / 10_000)


def sell_fill_price(p: float) -> float:
    return p * (1 - FRICTION_BPS / 10_000)


def is_clean_bull(close_so_far: pd.Series) -> bool:
    if len(close_so_far) < WINDOW + 1:
        return False
    roll_ret = close_so_far.pct_change(WINDOW).iloc[-1]
    sma_s = close_so_far.rolling(SMA_SHORT).mean().iloc[-1]
    sma_l = close_so_far.rolling(SMA_LONG).mean().iloc[-1]
    if pd.isna(roll_ret) or pd.isna(sma_s) or pd.isna(sma_l):
        return False
    return bool(roll_ret > THRESHOLD and sma_s > sma_l)


def run_period(data: dict[str, pd.DataFrame], dates: list, db_path: Path, label: str) -> dict:
    object.__setattr__(settings, "db_path", db_path)
    from app import storage  # re-import binds to the current module cache; db_path read at call time

    if db_path.exists():
        db_path.unlink()
    storage._conn = None
    storage.init_db()

    def open_notional() -> float:
        return sum(p["qty"] * p["avg_price"] for p in storage.list_positions(status="open"))

    def try_open(symbol: str, price: float, at_iso: str) -> bool:
        remaining = TOTAL_BUY_LIMIT - open_notional()
        if remaining < 1.0:
            return False
        fill = buy_fill_price(price)
        qty = round(min(SLICE_SIZE, remaining) / fill, 6)
        if qty <= 0:
            return False
        pid = storage.open_position(symbol, "long", qty, fill, f"bracket-multi-{label}", at=at_iso)
        storage.record_trade(None, pid, symbol, "BUY", qty, fill, notes="open", at=at_iso)
        storage.adjust_cash(-fill * qty)
        return True

    def close_pos(position: dict, price: float, at_iso: str, reason: str) -> None:
        qty = position["qty"]
        entry = position["avg_price"]
        fill = sell_fill_price(price)
        pnl = (fill - entry) * qty
        storage.close_position(position["id"], fill, pnl, at=at_iso)
        storage.add_realized_pnl(pnl)
        storage.record_trade(None, position["id"], position["symbol"], "SELL", qty, fill,
                              notes=reason, at=at_iso)
        storage.adjust_cash(fill * qty)

    opens = closes = blocked = 0
    hold_days: list[int] = []
    open_since: dict[int, pd.Timestamp] = {}

    for date in dates:
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
                reason = "stop-loss" if pct <= STOP_LOSS_PCT else (
                    "take-profit" if pct >= TAKE_PROFIT_PCT else (
                        "regime-flip" if not bull else None))
                if reason:
                    close_pos(existing, price, date_iso, reason)
                    closes += 1
                    hold_days.append((date - open_since.pop(existing["id"], date)).days)
                continue

            if bull:
                if try_open(sym, price, date_iso):
                    opens += 1
                    new_pos = storage.get_open_position(sym)
                    if new_pos:
                        open_since[new_pos["id"]] = date
                else:
                    blocked += 1

    last_iso = dates[-1].tz_localize("UTC").isoformat() if dates[-1].tzinfo is None else dates[-1].isoformat()
    for sym, h in data.items():
        existing = storage.get_open_position(sym)
        if existing:
            close_pos(existing, float(h["Close"].iloc[-1]), last_iso, "final-close")
            closes += 1
            if existing["id"] in open_since:
                hold_days.append((dates[-1] - open_since.pop(existing["id"])).days)

    from app import performance
    stats = performance.compute_performance()
    avg_hold = sum(hold_days) / len(hold_days) if hold_days else None
    return {
        "label": label, "start": dates[0].date(), "end": dates[-1].date(),
        "opens": opens, "closes": closes, "blocked": blocked, "avg_hold": avg_hold,
        **stats,
    }


def main() -> None:
    print(f"Loading full history for {len(SYMBOLS)} symbols...")
    full: dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS:
        h = yf.Ticker(sym).history(period=HISTORY_PERIOD, interval="1d")
        if not h.empty and getattr(h.index, "tz", None) is not None:
            h.index = h.index.tz_localize(None)
        if not h.empty:
            full[sym] = h

    all_dates = sorted(set().union(*(h.index for h in full.values())))
    n = len(all_dates)
    chunk = n // N_PERIODS
    # most recent period first, labeled P1 (P1 = the one already tested at :8006)
    windows = []
    for i in range(N_PERIODS):
        end_idx = n - i * chunk
        start_idx = n - (i + 1) * chunk if i < N_PERIODS - 1 else 0
        windows.append(all_dates[start_idx:end_idx])

    results = []
    for i, dates in enumerate(windows, start=1):
        if len(dates) < 60:
            print(f"P{i}: too few trading days ({len(dates)}), skipping")
            continue
        data_slice = {sym: h[(h.index >= dates[0]) & (h.index <= dates[-1])] for sym, h in full.items()}
        db_path = Path(__file__).resolve().parent / "data" / f"{DB_PREFIX}_p{i}.db"
        print(f"\nP{i}: {dates[0].date()} to {dates[-1].date()} ({len(dates)} trading days) -> {db_path.name}")
        r = run_period(data_slice, dates, db_path, f"p{i}")
        results.append(r)
        print(f"  opens={r['opens']} closes={r['closes']} blocked={r['blocked']} "
              f"avg_hold={r['avg_hold']:.1f}d  net=${r['realized_pnl_total']:.2f} "
              f"win_rate={r['win_rate']*100 if r['win_rate'] else 0:.1f}% "
              f"PF={r['profit_factor'] if r['profit_factor'] else float('nan'):.2f}")

    print("\n" + "=" * 100)
    print("STABILITY CHECK -- same strategy, 4 independent non-overlapping ~6mo windows")
    print("=" * 100)
    print(f"{'Period':>8} {'Dates':>24} {'Trades':>7} {'AvgHold':>8} {'WinRate':>8} {'PF':>6} {'Net $':>9}")
    for r in results:
        pf = r["profit_factor"] if r["profit_factor"] else float("nan")
        wr = r["win_rate"] * 100 if r["win_rate"] else 0
        print(f"{r['label']:>8} {str(r['start'])+' to '+str(r['end']):>24} {r['total_closed_trades']:>7} "
              f"{r['avg_hold']:>7.1f}d {wr:>7.1f}% {pf:>6.2f} {r['realized_pnl_total']:>9.2f}")

    n_profitable = sum(1 for r in results if r["realized_pnl_total"] > 0)
    print(f"\nProfitable in {n_profitable}/{len(results)} independent periods.")


if __name__ == "__main__":
    main()
