#!/usr/bin/env python3
"""Scale-out variant: tests whether win rate can be raised without gutting
total profit, using a different mechanism than a flat take-profit cap
(which we already know cuts total profit ~40-45%, from backtest_notp.py's
own comparison against the capped versions).

Each entry is split into two equal tranches instead of one:
  - Tranche A takes a modest +3% partial profit if touched (intraday-aware),
    locking in a realized win on half the position -- this is what should
    raise the win rate.
  - Tranche B has NO take-profit, exits only on the -3% stop or a regime
    flip, exactly like backtest_notp.py -- preserving participation in the
    rare large trend moves that drove most of that version's profit.

Both tranches share the same entry price/date and the same -3% stop.
Same $2,000 total cap (now split $100/$100 per entry instead of $200),
same 18-symbol universe, same 14 periods, 10bps friction, intraday-aware
High/Low checking throughout.
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
PARTIAL_TARGET_PCT = 0.03  # tranche A only
TOTAL_BUY_LIMIT = 2000.0
SLICE_SIZE = 200.0          # split into two $100 tranches per entry
FRICTION_BPS = 10.0

SYMBOLS = [
    "NVDA", "MSFT", "AMZN", "JPM", "XOM", "DIS", "CRM", "ADBE", "PYPL", "MRK",
    "AAPL", "SPY", "GOOGL", "META", "AMD", "TSLA", "QQQ", "KO",
]

N_PERIODS = 14
HISTORY_PERIOD = "8y"
DB_PREFIX = "backtest_scaleout"


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
    from app import storage

    if db_path.exists():
        db_path.unlink()
    storage._conn = None
    storage.init_db()

    def open_notional() -> float:
        return sum(p["qty"] * p["avg_price"] for p in storage.list_positions(status="open"))

    open_tranches: dict[str, list[dict]] = {}
    open_since: dict[int, pd.Timestamp] = {}

    def try_open(symbol: str, price: float, at_iso: str, today: pd.Timestamp) -> bool:
        remaining = TOTAL_BUY_LIMIT - open_notional()
        if remaining < 1.0:
            return False
        tranche_dollars = min(SLICE_SIZE, remaining) / 2
        if tranche_dollars <= 0:
            return False
        fill = buy_fill_price(price)
        tranches = []
        for tag in ("A", "B"):
            qty = round(tranche_dollars / fill, 6)
            if qty <= 0:
                continue
            pid = storage.open_position(symbol, "long", qty, fill, f"scaleout-{tag}-{label}", at=at_iso)
            storage.record_trade(None, pid, symbol, "BUY", qty, fill, notes=f"open-{tag}", at=at_iso)
            storage.adjust_cash(-fill * qty)
            tranches.append({"id": pid, "tranche": tag, "entry": fill, "qty": qty})
            open_since[pid] = today
        if not tranches:
            return False
        open_tranches[symbol] = tranches
        return True

    def close_tranche(symbol: str, t: dict, price: float, at_iso: str, reason: str) -> None:
        fill = sell_fill_price(price)
        pnl = (fill - t["entry"]) * t["qty"]
        storage.close_position(t["id"], fill, pnl, at=at_iso)
        storage.add_realized_pnl(pnl)
        storage.record_trade(None, t["id"], symbol, "SELL", t["qty"], fill, notes=reason, at=at_iso)
        storage.adjust_cash(fill * t["qty"])

    opens = closes = blocked = 0
    hold_days: list[int] = []

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
            bull = is_clean_bull(close_so_far)

            if sym in open_tranches:
                remaining = []
                for t in open_tranches[sym]:
                    entry = t["entry"]
                    stop_price = entry * (1 + STOP_LOSS_PCT)
                    reason = fill_price = None

                    if day_low <= stop_price:
                        reason = "stop-loss"
                        fill_price = day_open if day_open <= stop_price else stop_price
                    elif t["tranche"] == "A" and day_high >= entry * (1 + PARTIAL_TARGET_PCT):
                        target_price = entry * (1 + PARTIAL_TARGET_PCT)
                        reason = "partial-target"
                        fill_price = day_open if day_open >= target_price else target_price
                    elif not bull:
                        reason = "regime-flip"
                        fill_price = day_close

                    if reason:
                        close_tranche(sym, t, fill_price, date_iso, reason)
                        closes += 1
                        hold_days.append((date - open_since.pop(t["id"], date)).days)
                    else:
                        remaining.append(t)

                if remaining:
                    open_tranches[sym] = remaining
                else:
                    del open_tranches[sym]
                continue

            if bull:
                if try_open(sym, day_close, date_iso, date):
                    opens += 1
                else:
                    blocked += 1

    last_iso = dates[-1].tz_localize("UTC").isoformat() if dates[-1].tzinfo is None else dates[-1].isoformat()
    for sym, tranches in list(open_tranches.items()):
        last_price = float(data[sym]["Close"].iloc[-1])
        for t in tranches:
            close_tranche(sym, t, last_price, last_iso, "final-close")
            closes += 1
            hold_days.append((dates[-1] - open_since.pop(t["id"], dates[-1])).days)

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
    print("SCALE-OUT (half at +3% partial, half uncapped) -- same 14 periods, same $2,000 cap")
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
    print("(Compare to no-take-profit baseline: 9/14 profitable, win rate 35.9%, +$1,022.09.)")
    print("(Compare to full +6% take-profit: 9/14 profitable, win rate ~48%, +$685.83.)")


if __name__ == "__main__":
    main()
