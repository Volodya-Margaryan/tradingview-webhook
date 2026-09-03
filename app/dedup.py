"""Duplicate-signal protection: ignore repeat alerts for the same symbol+action
within a short configurable window (DEDUP_WINDOW_SECONDS)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import storage
from .config import settings


def is_duplicate(symbol: str, action: str) -> bool:
    last = storage.get_last_accepted_signal_time(symbol, action)
    if last is None:
        return False
    window = timedelta(seconds=settings.dedup_window_seconds)
    return datetime.now(timezone.utc) - last < window
