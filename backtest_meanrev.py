#!/usr/bin/env python3
"""Short-term mean-reversion strategy: buys a sharp pullback within an
established uptrend and forces an exit within a fixed number of days,
rather than following a trend for as long as it lasts.

Entry: price is above its 50-day moving average (a real uptrend, not a
falling knife) and has dropped at least 4% over the last 3 trading days.

Exit, whichever comes first:
  - +4% bounce (take-profit)
  - -3% stop-loss
  - 5 trading days elapsed (a forced time exit, guaranteeing a short hold
    regardless of price)

$2,000 total cap ($200 slices, 10 slots), 18-symbol universe, 14
non-overlapping ~6-month periods (2018-2026), intraday-aware (High/Low)
stop/target checking, 10bps friction each way.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402

PULLBACK_WINDOW = 3
PULLBACK_THRESHOLD = -0.04   # entry: dropped >= 4% over last 3 days
TREND_SMA = 50               # must still be above this (real uptrend)
STOP_LOSS_PCT = -0.03
TAKE_PROFIT_PCT = 0.04
MAX_HOLD_DAYS = 5            # forced time exit -- guarantees short holds

TOTAL_BUY_LIMIT = 2000.0
SLICE_SIZE = 200.0
FRICTION_BPS = 10.0

SYMBOLS = [
    "NVDA", "MSFT", "AMZN", "JPM", "XOM", "DIS", "CRM", "ADBE", "PYPL", "MRK",
    "AAPL", "SPY", "GOOGL", "META", "AMD", "TSLA", "QQQ", "KO",
]

N_PERIODS = 14
HISTORY_PERIOD = "8y"
DB_PREFIX = "backtest_meanrev"


def buy_fill_price(p: float) -> float:
    return p * (1 + FRICTION_BPS / 10_000)


def sell_fill_price(p: float) -> float:
    return p * (1 - FRICTION_BPS / 10_000)


def is_pullback_buy(close_so_far: pd.Series) -> bool:
    if len(close_so_far) < TREND_SMA + 1:
        return False
    sma = close_so_far.rolling(TREND_SMA).mean().iloc[-1]
    last = close_so_far.iloc[-1]
    if pd.isna(sma) or last <= sma:
        return False
    pullback = close_so_far.pct_change(PULLBACK_WINDOW).iloc[-1]
    if pd.isna(pullback):
        return False
    return bool(pullback <= PULLBACK_THRESHOLD)


def run_period(data: dict[str, pd.DataFrame], dates: list, db_path: Path, label: str) -> dict:
    object.__setattr__(settings, "db_path", db_path)
    from app import storage

    if db_path.exists():
        db_path.unlink()
    storage._conn = None
    storage.init_db()

    def open_notional() -> float:
        return sum(p["qty"] * p["avg_price"] for p in storage.list_positions(status="open"))

    def try_open(symbol: str, price: float, at_iso: str, day_index: int) -> tuple[bool, int]:
        remaining = TOTAL_BUY_LIMIT - open_notional()
        if remaining < 1.0:
            return False, -1
        fill = buy_fill_price(price)
        qty = round(min(SLICE_SIZE, remaining) / fill, 6)
        if qty <= 0:
            return False, -1
        pid = storage.open_position(symbol, "long", qty, fill, f"meanrev-{label}", at=at_iso)
        storage.record_trade(None, pid, symbol, "BUY", qty, fill, notes="open", at=at_iso)
        storage.adjust_cash(-fill * qty)
        return True, pid

    def close_pos(position: dict, fill_price: float, at_iso: str, reason: str) -> None:
        qty = position["qty"]
        entry = position["avg_price"]
        fill = sell_fill_price(fill_price)
        pnl = (fill - entry) * qty
        storage.close_position(position["id"], fill, pnl, at=at_iso)
        storage.add_realized_pnl(pnl)
        storage.record_trade(None, position["id"], position["symbol"], "SELL", qty, fill,
                              notes=reason, at=at_iso)
        storage.adjust_cash(fill * qty)

    opens = closes = blocked = 0
    hold_days: list[int] = []
    open_since: dict[int, pd.Timestamp] = {}
    entry_day_index: dict[int, int] = {}

    for date in dates:
        date_iso = date.tz_localize("UTC").isoformat() if date.tzinfo is None else date.isoformat()
        for sym, h in data.items():
            if date not in h.index:
                continue
            idx = h.index.get_loc(date)
            close_so_far = h["Close"].iloc[: idx + 1]
            day_open = float(h["Open"].iloc[idx])
            day_high = float(h["High"].iloc[idx])
            day_low = float(h["Low"].iloc[idx])
            day_close = float(h["Close"].iloc[idx])

            existing = storage.get_open_position(sym)
            if existing is not None:
                entry = existing["avg_price"]
                stop_price = entry * (1 + STOP_LOSS_PCT)
                target_price = entry * (1 + TAKE_PROFIT_PCT)
                days_in = idx - entry_day_index.get(existing["id"], idx)

                reason = None
                fill_price = None
                if day_low <= stop_price:
                    reason, fill_price = "stop-loss", (day_open if day_open <= stop_price else stop_price)
                elif day_high >= target_price:
                    reason, fill_price = "take-profit", (day_open if day_open >= target_price else target_price)
                elif days_in >= MAX_HOLD_DAYS:
                    reason, fill_price = "time-exit", day_close

                if reason:
                    close_pos(existing, fill_price, date_iso, reason)
                    closes += 1
                    hold_days.append((date - open_since.pop(existing["id"], date)).days)
                    entry_day_index.pop(existing["id"], None)
                continue

            if is_pullback_buy(close_so_far):
                ok, pid = try_open(sym, day_close, date_iso, idx)
                if ok:
                    opens += 1
                    open_since[pid] = date
                    entry_day_index[pid] = idx
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
    windows = []
    for i in range(N_PERIODS):
        end_idx = n - i * chunk
        start_idx = n - (i + 1) * chunk if i < N_PERIODS - 1 else 0
        windows.append(all_dates[start_idx:end_idx])

    results = []
    for i, dates in enumerate(windows, start=1):
        if len(dates) < 60:
            continue
        data_slice = {sym: h[(h.index >= dates[0]) & (h.index <= dates[-1])] for sym, h in full.items()}
        db_path = Path(__file__).resolve().parent / "data" / f"{DB_PREFIX}_p{i}.db"
        r = run_period(data_slice, dates, db_path, f"p{i}")
        results.append(r)
        print(f"P{i}: {dates[0].date()} to {dates[-1].date()}  opens={r['opens']} closes={r['closes']} "
              f"avg_hold={r['avg_hold']:.1f}d  net=${r['realized_pnl_total']:.2f} "
              f"win_rate={r['win_rate']*100 if r['win_rate'] else 0:.1f}% "
              f"PF={r['profit_factor'] if r['profit_factor'] else float('nan'):.2f}")

    print("\n" + "=" * 100)
    print("MEAN-REVERSION PULLBACK STRATEGY (max 5-day hold) -- 14 periods, $2,000 cap")
    print("=" * 100)
    total_net = sum(r["realized_pnl_total"] for r in results)
    n_profitable = sum(1 for r in results if r["realized_pnl_total"] > 0)
    for r in results:
        pf = r["profit_factor"] if r["profit_factor"] else float("nan")
        wr = r["win_rate"] * 100 if r["win_rate"] else 0
        print(f"{r['label']:>4} {str(r['start'])+' to '+str(r['end']):>24} "
              f"trades={r['total_closed_trades']:>4} avg_hold={r['avg_hold']:>5.1f}d "
              f"win%={wr:>5.1f} PF={pf:>5.2f} net=${r['realized_pnl_total']:>8.2f}")
    print(f"\nProfitable in {n_profitable}/{len(results)} periods. Combined net: ${total_net:.2f}")


if __name__ == "__main__":
    main()
