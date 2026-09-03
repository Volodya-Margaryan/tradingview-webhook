#!/usr/bin/env python3
"""Real day trading: every position opens and closes on the SAME calendar
day -- zero overnight exposure, unlike every prior strategy tested (all of
which held positions across multiple days).

Honesty on data: true minute-by-minute intraday backtesting only works for
the last ~60 days from this data source. This design only needs each day's
Open and Close (available for the full 8 years), so it can be tested
properly across all 14 periods without that limitation.

Strategy: gap-down FADE (opposite of backtest_daytrade.py's gap-up-continuation,
which lost -$693 over 8 years -- testing whether buying weakness works where
chasing strength didn't).
  Trend filter: yesterday's close was above its 50-day SMA (real uptrend --
    still only buying dips in names that are fundamentally going up).
  Entry: today's Open is <= yesterday's Close - GAP_THRESHOLD (a real gap down).
  Exit: same day's Close. Always. No exceptions, no overnight hold ever.

Same $2,000 total cap, $200 slices, 10bps friction each way. Universe
expanded well beyond the original 18 blue chips per explicit request to
"trade everything" -- adds higher-beta speculative equities and crypto
(which trades 7 days/week, unlike equities).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import settings  # noqa: E402

TREND_SMA = 50
GAP_THRESHOLD = 0.01   # gap down >= 1% from yesterday's close
FRICTION_BPS = 10.0

TOTAL_BUY_LIMIT = 2000.0
SLICE_SIZE = 200.0

BLUE_CHIPS = [
    "NVDA", "MSFT", "AMZN", "JPM", "XOM", "DIS", "CRM", "ADBE", "PYPL", "MRK",
    "AAPL", "SPY", "GOOGL", "META", "AMD", "TSLA", "QQQ", "KO",
]
SPECULATIVE = [
    "RIVN", "SOFI", "PLUG", "RIOT", "MARA", "UPST", "RKLB", "IONQ", "LCID",
    "NIO", "CHPT", "SOUN", "IREN", "ASTS", "AFRM", "PLTR", "COIN", "MSTR", "SMCI",
]
CRYPTO = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD", "ADA-USD", "AVAX-USD", "LINK-USD", "LTC-USD"]
SYMBOLS = BLUE_CHIPS + SPECULATIVE + CRYPTO

N_PERIODS = 14
HISTORY_PERIOD = "8y"
DB_PREFIX = "backtest_daytrade_fade"


def buy_fill_price(p: float) -> float:
    return p * (1 + FRICTION_BPS / 10_000)


def sell_fill_price(p: float) -> float:
    return p * (1 - FRICTION_BPS / 10_000)


def gaps_down_in_trend(hist_so_far: pd.DataFrame, today_open: float) -> bool:
    if len(hist_so_far) < TREND_SMA + 1:
        return False
    yesterday_close = hist_so_far["Close"].iloc[-1]
    sma = hist_so_far["Close"].rolling(TREND_SMA).mean().iloc[-1]
    if pd.isna(sma) or yesterday_close <= sma:
        return False
    return bool(today_open <= yesterday_close * (1 - GAP_THRESHOLD))


def run_period(data: dict[str, pd.DataFrame], dates: list, db_path: Path, label: str) -> dict:
    object.__setattr__(settings, "db_path", db_path)
    from app import storage

    if db_path.exists():
        db_path.unlink()
    storage._conn = None
    storage.init_db()

    def open_notional() -> float:
        return sum(p["qty"] * p["avg_price"] for p in storage.list_positions(status="open"))

    opens = closes = blocked = 0
    trading_days_with_a_trade = 0

    for date in dates:
        date_iso = date.tz_localize("UTC").isoformat() if date.tzinfo is None else date.isoformat()
        any_trade_today = False
        for sym, h in data.items():
            if date not in h.index:
                continue
            idx = h.index.get_loc(date)
            if idx == 0:
                continue
            hist_so_far = h.iloc[:idx]  # strictly before today -- no lookahead
            today_open = float(h["Open"].iloc[idx])
            today_close = float(h["Close"].iloc[idx])

            if not gaps_down_in_trend(hist_so_far, today_open):
                continue

            remaining = TOTAL_BUY_LIMIT - open_notional()
            if remaining < 1.0:
                blocked += 1
                continue

            buy_fill = buy_fill_price(today_open)
            qty = round(min(SLICE_SIZE, remaining) / buy_fill, 6)
            if qty <= 0:
                continue

            pid = storage.open_position(sym, "long", qty, buy_fill, f"daytrade-fade-{label}", at=date_iso)
            storage.record_trade(None, pid, sym, "BUY", qty, buy_fill, notes="open", at=date_iso)
            storage.adjust_cash(-buy_fill * qty)
            opens += 1
            any_trade_today = True

            sell_fill = sell_fill_price(today_close)
            pnl = (sell_fill - buy_fill) * qty
            storage.close_position(pid, sell_fill, pnl, at=date_iso)
            storage.add_realized_pnl(pnl)
            storage.record_trade(None, pid, sym, "SELL", qty, sell_fill, notes="same-day-close", at=date_iso)
            storage.adjust_cash(sell_fill * qty)
            closes += 1

        if any_trade_today:
            trading_days_with_a_trade += 1

    from app import performance
    stats = performance.compute_performance()
    return {
        "label": label, "start": dates[0].date(), "end": dates[-1].date(),
        "opens": opens, "closes": closes, "blocked": blocked,
        "active_days": trading_days_with_a_trade, "total_days": len(dates),
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
        print(f"P{i}: {dates[0].date()} to {dates[-1].date()}  trades={r['closes']} "
              f"active_days={r['active_days']}/{r['total_days']}  net=${r['realized_pnl_total']:.2f} "
              f"win_rate={r['win_rate']*100 if r['win_rate'] else 0:.1f}% "
              f"PF={r['profit_factor'] if r['profit_factor'] else float('nan'):.2f}")

    print("\n" + "=" * 100)
    print("DAY TRADING (gap-DOWN fade, same-day close only, expanded universe) -- 14 periods, $2,000 cap")
    print("=" * 100)
    total_net = sum(r["realized_pnl_total"] for r in results)
    n_profitable = sum(1 for r in results if r["realized_pnl_total"] > 0)
    for r in results:
        pf = r["profit_factor"] if r["profit_factor"] else float("nan")
        wr = r["win_rate"] * 100 if r["win_rate"] else 0
        print(f"{r['label']:>4} {str(r['start'])+' to '+str(r['end']):>24} "
              f"trades={r['total_closed_trades']:>4} win%={wr:>5.1f} PF={pf:>5.2f} "
              f"net=${r['realized_pnl_total']:>8.2f}")
    print(f"\nProfitable in {n_profitable}/{len(results)} periods. Combined net: ${total_net:.2f}")


if __name__ == "__main__":
    main()
