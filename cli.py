#!/usr/bin/env python3
"""Terminal inspection + control tool for the TradingView paper-trading webhook receiver.

Usage:
    python cli.py killswitch on|off|status
    python cli.py signals [--limit 20]
    python cli.py positions [--status open|closed]
    python cli.py trades [--limit 20]
    python cli.py performance
    python cli.py status
    python cli.py summary [--limit 20]
    python cli.py session-marker [--label TEXT]
    python cli.py reset-account
"""
from __future__ import annotations

import argparse
import sys

from app import killswitch, performance as perf_module, storage
from app.config import settings
from app.logging_conf import signal_logger


def _table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "(none)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    header = " | ".join(c.ljust(widths[c]) for c in columns)
    sep = "-+-".join("-" * widths[c] for c in columns)
    lines = [header, sep]
    for r in rows:
        lines.append(" | ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))
    return "\n".join(lines)


def cmd_killswitch(args: argparse.Namespace) -> None:
    storage.init_db()
    if args.action == "status":
        active = killswitch.is_active()
        print(f"kill switch: {'ACTIVE (signals blocked)' if active else 'inactive'}")
        print(f"  env flag (KILL_SWITCH in .env): {settings.kill_switch_env}")
        print(f"  runtime flag (DB, toggled by this CLI): {storage.is_kill_switch_on()}")
    elif args.action == "on":
        killswitch.set_active(True)
        print("kill switch ENABLED — the running server will stop processing new signals immediately.")
    elif args.action == "off":
        killswitch.set_active(False)
        print("kill switch disabled — signal processing resumed.")
        if settings.kill_switch_env:
            print("NOTE: KILL_SWITCH=true is still set in .env; that flag also blocks processing "
                  "and requires a server restart to clear.")


def cmd_status(_: argparse.Namespace) -> None:
    storage.init_db()
    acct = storage.get_account_summary()
    print(f"kill switch active: {killswitch.is_active()}")
    print(f"equity: ${acct['equity']:.2f}  cash: ${acct['cash']:.2f}  "
          f"open positions: {acct['open_position_count']}")
    print(f"realized P&L total: ${acct['realized_pnl_total']:.2f}  "
          f"(24h: ${acct['daily_realized_pnl']:.2f})")


def cmd_signals(args: argparse.Namespace) -> None:
    storage.init_db()
    rows = storage.list_signals(limit=args.limit)
    print(_table(rows, ["id", "received_at", "status", "symbol", "action", "price", "strategy", "reason"]))


def cmd_positions(args: argparse.Namespace) -> None:
    storage.init_db()
    rows = storage.list_positions(status=args.status)
    if args.live and rows:
        rows = perf_module.enrich_positions_with_quotes(rows)
        print(_table(rows, ["id", "symbol", "side", "qty", "avg_price", "live_price",
                             "unrealized_pnl", "status", "opened_at"]))
    else:
        print(_table(rows, ["id", "symbol", "side", "qty", "avg_price", "status", "opened_at", "closed_at", "realized_pnl"]))


def cmd_trades(args: argparse.Namespace) -> None:
    storage.init_db()
    rows = storage.list_trades(limit=args.limit)
    print(_table(rows, ["id", "filled_at", "symbol", "side", "qty", "price", "notes"]))


def cmd_performance(_: argparse.Namespace) -> None:
    storage.init_db()
    p = perf_module.compute_performance()
    for k, v in p.items():
        print(f"{k}: {v}")


def cmd_summary(args: argparse.Namespace) -> None:
    """One-shot report: mode, signals (accepted/rejected), trades, positions, P&L.

    By default, scopes signals/fills to everything since the last session
    marker (set via `session-marker`), so "since session start" just works.
    Pass --all to ignore the marker and show the most recent N regardless.
    """
    storage.init_db()
    acct = storage.get_account_summary()
    perf = perf_module.compute_performance()
    positions_open = storage.list_positions(status="open")
    positions_closed = storage.list_positions(status="closed")

    marker = None if args.all else storage.get_session_marker()
    if marker:
        signals = storage.list_signals_since(marker["at"], limit=max(args.limit, 500))
        trades = storage.list_trades_since(marker["at"], limit=max(args.limit, 500))
        scope_desc = f"since session start: {marker['at']} ({marker['label']})"
    else:
        signals = storage.list_signals(limit=args.limit)
        trades = storage.list_trades(limit=args.limit)
        scope_desc = f"last {args.limit} (no session marker set — run `session-marker` to scope this)"

    rejected = [s for s in signals if s["status"] == "rejected"]

    print("=" * 70)
    print("TRADINGVIEW WEBHOOK RECEIVER — SUMMARY")
    print(f"scope: {scope_desc}")
    print("=" * 70)

    kill_active = killswitch.is_active()
    print(f"\nMODE: {'*** KILL SWITCH ACTIVE — signals blocked ***' if kill_active else 'LIVE (paper trading only, no real orders possible)'}")
    print(f"  env flag: {settings.kill_switch_env}   runtime flag: {storage.is_kill_switch_on()}")
    print(f"  duplicate-signal window: {settings.dedup_window_seconds}s")

    return_str = f"{perf['return_pct']:.4f}%" if perf['return_pct'] is not None else "n/a"
    print(f"\nACCOUNT (all-time, not session-scoped): equity ${acct['equity']:.2f}  "
          f"cash ${acct['cash']:.2f}  open positions {acct['open_position_count']}")
    print(f"  realized P&L total: ${acct['realized_pnl_total']:.2f}   "
          f"(last 24h: ${acct['daily_realized_pnl']:.2f})   return: {return_str}")

    win_rate_str = f"{perf['win_rate']*100:.1f}%" if perf['win_rate'] is not None else "n/a"
    print(f"\nPERFORMANCE (all-time): {perf['total_closed_trades']} closed trades, "
          f"{perf['wins']} win / {perf['losses']} loss, win rate {win_rate_str}")

    print(f"\nSIGNALS ({scope_desc}, {len(rejected)} rejected):")
    print(_table(signals, ["id", "received_at", "status", "symbol", "action", "reason"]))

    print(f"\nOPEN POSITIONS ({len(positions_open)}):")
    print(_table(positions_open, ["symbol", "side", "qty", "avg_price", "opened_at", "strategy"]))

    print(f"\nCLOSED POSITIONS ({len(positions_closed)}):")
    print(_table(positions_closed, ["symbol", "side", "qty", "avg_price", "close_price", "realized_pnl", "closed_at"]))

    print(f"\nFILLS ({scope_desc}):")
    print(_table(trades, ["filled_at", "symbol", "side", "qty", "price", "notes"]))
    print("=" * 70)


def cmd_session_marker(args: argparse.Namespace) -> None:
    """Write a clearly-delimited marker into webhook.log AND persist its
    timestamp in the DB, so `summary` can filter to 'since session start'."""
    storage.init_db()
    label = args.label or "session start"
    banner = "#" * 70
    signal_logger.info(banner)
    signal_logger.info("SESSION MARKER: %s", label.upper())
    signal_logger.info(banner)
    at = storage.set_session_marker(label)
    print(f"Wrote SESSION MARKER ('{label}') to {settings.log_path}")
    print(f"Recorded session start at {at} — `cli.py summary` will now scope to this by default.")


def cmd_reset_account(_: argparse.Namespace) -> None:
    """Reset cash/realized P&L back to a clean starting_equity baseline.
    Does not delete signal/position/trade history — just excludes anything
    before the reset from account totals and performance stats going forward."""
    storage.init_db()
    before = storage.get_account_summary()
    banner = "#" * 70
    signal_logger.info(banner)
    signal_logger.info(
        "ACCOUNT RESET: cash/realized P&L cleared back to $%.2f (was equity $%.2f, realized $%.2f)",
        settings.starting_equity, before["equity"], before["realized_pnl_total"],
    )
    signal_logger.info(banner)
    at = storage.reset_account()
    print(f"Account reset at {at}")
    print(f"  before: equity ${before['equity']:.2f}, realized P&L ${before['realized_pnl_total']:.2f}")
    print(f"  after:  equity ${settings.starting_equity:.2f}, realized P&L $0.00")
    print("  (signal/position/trade history preserved — just excluded from totals going forward)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_kill = sub.add_parser("killswitch", help="stop/resume all signal processing immediately")
    p_kill.add_argument("action", choices=["on", "off", "status"])
    p_kill.set_defaults(func=cmd_killswitch)

    p_status = sub.add_parser("status", help="account + kill switch summary")
    p_status.set_defaults(func=cmd_status)

    p_signals = sub.add_parser("signals", help="list recent signals")
    p_signals.add_argument("--limit", type=int, default=20)
    p_signals.set_defaults(func=cmd_signals)

    p_positions = sub.add_parser("positions", help="list paper positions")
    p_positions.add_argument("--status", choices=["open", "closed"], default=None)
    p_positions.add_argument("--live", action="store_true",
                              help="attach live (delayed) price + unrealized P&L, informational only")
    p_positions.set_defaults(func=cmd_positions)

    p_trades = sub.add_parser("trades", help="list simulated fills")
    p_trades.add_argument("--limit", type=int, default=20)
    p_trades.set_defaults(func=cmd_trades)

    p_perf = sub.add_parser("performance", help="paper-trading performance stats")
    p_perf.set_defaults(func=cmd_performance)

    p_summary = sub.add_parser(
        "summary", help="one-shot report: mode, signals, trades, positions, P&L"
    )
    p_summary.add_argument("--limit", type=int, default=20)
    p_summary.add_argument("--all", action="store_true",
                            help="ignore the session marker; show most recent N regardless of session")
    p_summary.set_defaults(func=cmd_summary)

    p_marker = sub.add_parser(
        "session-marker", help="write a SESSION MARKER banner into webhook.log"
    )
    p_marker.add_argument("--label", type=str, default=None)
    p_marker.set_defaults(func=cmd_session_marker)

    p_reset = sub.add_parser(
        "reset-account", help="reset cash/realized P&L to a clean starting_equity baseline"
    )
    p_reset.set_defaults(func=cmd_reset_account)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
