"""Paper-trading execution engine: simulated fills only. No real broker calls,
no network egress to any exchange. Every 'fill' happens at the signal's price
(no live market data lookup), which keeps this fully offline and testable.

Behavior:
- BUY with no open position for the symbol -> sized via risk.size_position,
  opens a simulated long position (or covers an open short).
- BUY with an existing open long -> rejected (avoid pyramiding by default).
- SELL with an open long -> closes it, records realized P&L.
- SELL with no open position -> opens a short if ALLOW_SHORTS is enabled,
  otherwise rejected (flat SELL alerts are logged but not executed).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import risk, storage
from .config import settings
from .schemas import Action


@dataclass
class ExecutionResult:
    executed: bool
    reason: str
    side: Optional[str] = None
    qty: Optional[float] = None
    price: Optional[float] = None
    realized_pnl: Optional[float] = None
    position_id: Optional[int] = None


def _close(position: dict, exit_price: float, signal_id: Optional[int],
           at: Optional[str] = None) -> ExecutionResult:
    qty = position["qty"]
    entry_price = position["avg_price"]
    if position["side"] == "long":
        realized_pnl = (exit_price - entry_price) * qty
        storage.adjust_cash(exit_price * qty)
        fill_side = "SELL"
    else:  # short
        realized_pnl = (entry_price - exit_price) * qty
        storage.adjust_cash(realized_pnl)  # short: only the P&L moves cash
        fill_side = "BUY"

    storage.close_position(position["id"], exit_price, realized_pnl, at=at)
    storage.add_realized_pnl(realized_pnl)
    storage.record_trade(
        signal_id, position["id"], position["symbol"], fill_side, qty, exit_price,
        notes="close", at=at,
    )
    return ExecutionResult(
        executed=True,
        reason=f"closed {position['side']} position",
        side=fill_side,
        qty=qty,
        price=exit_price,
        realized_pnl=realized_pnl,
        position_id=position["id"],
    )


def _open(symbol: str, side: str, price: float, strategy: str, signal_id: Optional[int],
          at: Optional[str] = None) -> ExecutionResult:
    sizing = risk.size_position(price)
    if not sizing.approved:
        return ExecutionResult(executed=False, reason=sizing.reason)

    position_id = storage.open_position(symbol, side, sizing.qty, price, strategy, at=at)
    fill_side = "BUY" if side == "long" else "SELL"
    storage.record_trade(signal_id, position_id, symbol, fill_side, sizing.qty, price, notes="open", at=at)

    if side == "long":
        storage.adjust_cash(-price * sizing.qty)
    # opening a short: cash unaffected until cover (P&L settles at close)

    return ExecutionResult(
        executed=True,
        reason=f"opened {side} position",
        side=fill_side,
        qty=sizing.qty,
        price=price,
        position_id=position_id,
    )


def execute_signal(
    symbol: str, action: Action, price: float, strategy: str, signal_id: Optional[int],
    at: Optional[str] = None,
) -> ExecutionResult:
    """`at` optionally backdates position/trade timestamps to a simulated
    historical date (used by the backtest replay) instead of real wall-clock
    time. Defaults to None, i.e. real-time, for all live webhook traffic."""
    existing = storage.get_open_position(symbol)

    if action == Action.BUY:
        if existing is None:
            return _open(symbol, "long", price, strategy, signal_id, at=at)
        if existing["side"] == "short":
            return _close(existing, price, signal_id, at=at)
        return ExecutionResult(executed=False, reason="long position already open for symbol")

    # action == SELL
    if existing is not None and existing["side"] == "long":
        return _close(existing, price, signal_id, at=at)
    if existing is not None and existing["side"] == "short":
        return ExecutionResult(executed=False, reason="short position already open for symbol")
    if not settings.allow_shorts:
        return ExecutionResult(
            executed=False,
            reason="no open long position to sell and ALLOW_SHORTS is disabled",
        )
    return _open(symbol, "short", price, strategy, signal_id, at=at)
