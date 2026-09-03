"""Paper-trading performance summary, computed from closed positions."""
from __future__ import annotations

from typing import Optional

from . import market_data, storage


def compute_performance() -> dict:
    reset_at = storage.get_account_reset_marker()
    closed = (
        storage.list_positions_closed_since(reset_at)
        if reset_at is not None
        else storage.list_positions(status="closed")
    )
    account = storage.get_account_summary()

    total_trades = len(closed)
    wins = [p for p in closed if (p["realized_pnl"] or 0) > 0]
    losses = [p for p in closed if (p["realized_pnl"] or 0) < 0]

    gross_profit = sum(p["realized_pnl"] for p in wins)
    gross_loss = abs(sum(p["realized_pnl"] for p in losses))

    win_rate = (len(wins) / total_trades) if total_trades else None
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
    avg_win = (gross_profit / len(wins)) if wins else None
    avg_loss = (gross_loss / len(losses)) if losses else None

    starting_equity = account["starting_equity"]
    equity = account["equity"]
    return_pct = ((equity - starting_equity) / starting_equity * 100) if starting_equity else None

    return {
        "starting_equity": starting_equity,
        "current_equity": equity,
        "cash": account["cash"],
        "return_pct": return_pct,
        "realized_pnl_total": account["realized_pnl_total"],
        "daily_realized_pnl": account["daily_realized_pnl"],
        "open_position_count": account["open_position_count"],
        "total_closed_trades": total_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }


def enrich_positions_with_quotes(positions: list[dict]) -> list[dict]:
    """Attach a live (delayed, informational-only) price and unrealized P&L to
    each open position. Never touches the DB or trading logic — display only."""
    enriched = []
    for p in positions:
        live = market_data.get_live_price(p["symbol"])
        entry = dict(p)
        entry["live_price"] = live
        if live is None:
            entry["unrealized_pnl"] = None
        elif p["side"] == "long":
            entry["unrealized_pnl"] = (live - p["avg_price"]) * p["qty"]
        else:
            entry["unrealized_pnl"] = (p["avg_price"] - live) * p["qty"]
        enriched.append(entry)
    return enriched


def compute_unrealized_summary(enriched_positions: list[dict]) -> dict:
    known = [p["unrealized_pnl"] for p in enriched_positions if p["unrealized_pnl"] is not None]
    return {
        "unrealized_pnl_total": sum(known) if known else 0.0,
        "positions_with_live_price": len(known),
        "positions_missing_live_price": len(enriched_positions) - len(known),
    }
