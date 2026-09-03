#!/usr/bin/env python3
"""Grid-search the bracket strategy's stop-loss/take-profit levels, then check
whether the winner is a real edge or just curve-fit using the pbo-deflated-sharpe
skill (PBO via CSCV + Deflated Sharpe Ratio) -- before trusting it.

Split: oldest 6 months = TRAIN (grid search happens only here). Most recent
6 months = TEST (the exact window backtest_bracket.py has been using all
along) -- the winning config gets one, single, honest run there, never
peeked at during the search. This is the actual point: if the "improved"
config only won on the eval window, that's overfitting, not improvement.

In-memory simulation only (no DB writes) for speed across many trials; only
the final chosen config gets replayed through the real storage/paper engine
for dashboard viewing, same as every other backtest script here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path.home() / ".claude" / "skills" / "pbo-deflated-sharpe"))

from pbo_deflated_sharpe import cscv_pbo, deflated_sharpe, probabilistic_sharpe, verdict  # noqa: E402

WINDOW = 20
THRESHOLD = 0.05
SMA_SHORT = 20
SMA_LONG = 50

TOTAL_CAP = 3000.0
SLICE = 300.0
FRICTION_BPS = 10.0

SYMBOLS = [
    "NVDA", "MSFT", "AMZN", "JPM", "XOM", "DIS", "CRM", "ADBE", "PYPL", "MRK",
    "AAPL", "SPY", "GOOGL", "META", "AMD", "TSLA", "QQQ", "KO",
]

STOP_GRID = [-0.02, -0.03, -0.04, -0.05]
TARGET_GRID = [0.04, 0.06, 0.08, 0.10, 0.12]


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


def simulate(data: dict[str, pd.DataFrame], dates: list, stop_pct: float, target_pct: float) -> list[dict]:
    """In-memory only -- returns a list of closed trades {exit_date, pnl}."""
    positions: dict[str, dict] = {}
    trades: list[dict] = []

    def open_notional() -> float:
        return sum(p["qty"] * p["entry"] for p in positions.values())

    for date in dates:
        for sym, h in data.items():
            if date not in h.index:
                continue
            idx = h.index.get_loc(date)
            close_so_far = h["Close"].iloc[: idx + 1]
            price = float(h["Close"].iloc[idx])
            bull = is_clean_bull(close_so_far)

            if sym in positions:
                entry = positions[sym]["entry"]
                pct = (price - entry) / entry
                reason = None
                if pct <= stop_pct:
                    reason = "stop"
                elif pct >= target_pct:
                    reason = "target"
                elif not bull:
                    reason = "regime"
                if reason:
                    fill = price * (1 - FRICTION_BPS / 10_000)
                    qty = positions[sym]["qty"]
                    trades.append({"exit_date": date, "pnl": (fill - entry) * qty})
                    del positions[sym]
            elif bull:
                remaining = TOTAL_CAP - open_notional()
                if remaining >= 1.0:
                    fill = price * (1 + FRICTION_BPS / 10_000)
                    qty = min(SLICE, remaining) / fill
                    positions[sym] = {"qty": qty, "entry": fill}

    for sym, pos in positions.items():
        last_price = float(data[sym]["Close"].iloc[-1])
        fill = last_price * (1 - FRICTION_BPS / 10_000)
        trades.append({"exit_date": dates[-1], "pnl": (fill - pos["entry"]) * pos["qty"]})

    return trades


def weekly_returns(trades: list[dict], week_index: pd.DatetimeIndex) -> np.ndarray:
    """Bucket each trade's P&L into the last week_index bin <= its exit date."""
    pnl_by_week = pd.Series(0.0, index=week_index)
    for t in trades:
        exit_date = pd.Timestamp(t["exit_date"])
        pos = week_index.searchsorted(exit_date, side="right") - 1
        pos = max(0, min(pos, len(week_index) - 1))
        pnl_by_week.iloc[pos] += t["pnl"]
    return (pnl_by_week / TOTAL_CAP).to_numpy()


def main() -> None:
    print(f"Loading 1y history for {len(SYMBOLS)} symbols...")
    data: dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS:
        h = load_history(sym, "1y")
        if not h.empty:
            data[sym] = h

    all_dates = sorted(set().union(*(h.index for h in data.values())))
    midpoint = len(all_dates) // 2
    train_dates = all_dates[:midpoint]
    test_dates = all_dates[midpoint:]
    print(f"TRAIN: {train_dates[0].date()} to {train_dates[-1].date()} ({len(train_dates)} days)")
    print(f"TEST (held out, never used for tuning):  {test_dates[0].date()} to {test_dates[-1].date()} ({len(test_dates)} days)")

    train_weeks = pd.date_range(train_dates[0], train_dates[-1], freq="W-MON")

    print(f"\nGrid search: {len(STOP_GRID)} stops x {len(TARGET_GRID)} targets = "
          f"{len(STOP_GRID)*len(TARGET_GRID)} trials, TRAIN window only")
    print("-" * 90)
    print(f"{'stop':>6} {'target':>7} {'trades':>7} {'net $':>9} {'win%':>6} {'PF':>6}")

    trial_returns = []
    trial_labels = []
    trial_summary = []

    for stop in STOP_GRID:
        for target in TARGET_GRID:
            trades = simulate(data, train_dates, stop, target)
            net = sum(t["pnl"] for t in trades)
            wins = [t["pnl"] for t in trades if t["pnl"] > 0]
            losses = [t["pnl"] for t in trades if t["pnl"] < 0]
            win_rate = len(wins) / len(trades) if trades else 0
            pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
            rets = weekly_returns(trades, train_weeks)
            trial_returns.append(rets)
            trial_labels.append((stop, target))
            trial_summary.append((stop, target, len(trades), net, win_rate, pf))
            print(f"{stop:>+6.0%} {target:>+7.0%} {len(trades):>7} {net:>9.2f} {win_rate:>6.1%} {pf:>6.2f}")

    matrix = np.array(trial_returns).T  # T periods x N trials
    trial_sharpes = matrix.mean(axis=0) / np.where(matrix.std(axis=0, ddof=1) > 0, matrix.std(axis=0, ddof=1), 1)

    best_idx = int(np.argmax([s[3] for s in trial_summary]))  # by net $ in-sample
    best_stop, best_target = trial_labels[best_idx]
    chosen_returns = matrix[:, best_idx]

    pbo = cscv_pbo(matrix)
    dsr = deflated_sharpe(chosen_returns, trial_sharpes)
    psr = probabilistic_sharpe(chosen_returns, 0.0)

    print("-" * 90)
    print(f"Best in-sample (TRAIN): stop={best_stop:+.0%} target={best_target:+.0%} "
          f"net=${trial_summary[best_idx][3]:.2f} PF={trial_summary[best_idx][5]:.2f}")
    print(f"\nOVERFITTING CHECK ({len(trial_labels)} trials tested):")
    print(f"  PBO (Probability of Backtest Overfitting): {pbo:.1%}  ({'PASS, <50%' if pbo < 0.5 else 'FAIL, >=50% -- likely curve-fit'})")
    print(f"  DSR (Deflated Sharpe Ratio):                {dsr:.1%}  ({'STRONG, >95%' if dsr > 0.95 else 'weak/uncertain'})")
    print(f"  PSR (Probabilistic Sharpe, no trial penalty):{psr:.1%}  (for contrast -- shows the cost of trying {len(trial_labels)} configs)")
    v = verdict(pbo=pbo, dsr=dsr)
    for line in v["lines"]:
        print(f"  {line}")
    print(f"  OVERALL: {v['verdict'].upper()}")

    print(f"\nNow running the winning config ({best_stop:+.0%}/{best_target:+.0%}) on the HELD-OUT TEST window "
          f"it never saw during tuning...")
    test_trades = simulate(data, test_dates, best_stop, best_target)
    test_net = sum(t["pnl"] for t in test_trades)
    test_wins = [t["pnl"] for t in test_trades if t["pnl"] > 0]
    test_losses = [t["pnl"] for t in test_trades if t["pnl"] < 0]
    test_pf = (sum(test_wins) / abs(sum(test_losses))) if test_losses else float("inf")
    print(f"  TEST (out-of-sample) result: {len(test_trades)} trades, net=${test_net:.2f}, "
          f"win_rate={len(test_wins)/len(test_trades):.1%}, PF={test_pf:.2f}")
    print(f"\n  Compare to the ORIGINAL untuned -3%/+6% guess on this SAME test window: "
          f"net=$168.03, PF=1.37 (already known, from backtest_bracket.py --friction-bps 10)")


if __name__ == "__main__":
    main()
