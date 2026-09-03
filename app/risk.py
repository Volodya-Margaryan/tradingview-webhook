"""Position sizing and hard risk limits for the paper account.

All limits are configurable via .env / config.Settings:
- MAX_RISK_PER_TRADE_PCT: cap the notional risked on a single trade, as a
  percent of current equity
- MAX_POSITION_NOTIONAL_PCT: hard cap on any single position's notional,
  as a percent of current equity
- MAX_OPEN_POSITIONS: cap on concurrent open positions
- MAX_DAILY_LOSS_PCT: circuit breaker — block new entries once the rolling
  24h realized loss exceeds this percent of starting equity
"""
from __future__ import annotations

from dataclasses import dataclass

from . import storage
from .config import settings


@dataclass
class SizingResult:
    approved: bool
    qty: float
    reason: str


def size_position(price: float) -> SizingResult:
    account = storage.get_account_summary()
    equity = account["equity"]

    if equity <= 0:
        return SizingResult(False, 0.0, "account equity is zero or negative")

    daily_pnl = account["daily_realized_pnl"]
    daily_loss_limit = -(settings.starting_equity * settings.max_daily_loss_pct / 100)
    if daily_pnl <= daily_loss_limit:
        return SizingResult(
            False,
            0.0,
            f"daily loss limit hit (realized {daily_pnl:.2f} <= limit {daily_loss_limit:.2f})",
        )

    if account["open_position_count"] >= settings.max_open_positions:
        return SizingResult(
            False, 0.0, f"max open positions reached ({settings.max_open_positions})"
        )

    risk_notional = equity * (settings.max_risk_per_trade_pct / 100)
    cap_notional = equity * (settings.max_position_notional_pct / 100)
    notional = min(risk_notional, cap_notional)

    if notional <= 0:
        return SizingResult(False, 0.0, "computed position notional is <= 0")

    if notional > account["cash"]:
        notional = account["cash"]

    qty = round(notional / price, 6)
    if qty <= 0:
        return SizingResult(False, 0.0, "position size rounds down to 0 units")

    return SizingResult(True, qty, "ok")
