"""Live quotes from Yahoo Finance, for display purposes only.

Hard boundary: nothing in here is ever called from the paper-trading engine
or the risk/sizing path. A signal is always executed at the price the
webhook payload actually sent (matching what a real TradingView alert would
have reported at fire time) — this module only adds an independent,
informational cross-check and dashboard context. If Yahoo is slow, rate
limited, or unreachable, every function here degrades to returning None
rather than raising, so it can never block or break signal processing.

Quotes are delayed (~15min for US equities on the free Yahoo endpoint),
not real-time — treat everything from here as approximate.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

import yfinance as yf

_CACHE_TTL_SECONDS = 20
_FETCH_TIMEOUT_SECONDS = 3.0
_cache: dict[str, tuple[float, Optional[float]]] = {}  # symbol -> (fetched_at, price)

_CRYPTO_QUOTE_SUFFIXES = ("USDT", "USD", "USDC")


def normalize_symbol(symbol: str) -> str:
    """Best-effort TradingView-style symbol -> Yahoo Finance ticker.

    'NASDAQ:AAPL' -> 'AAPL'. 'BTCUSD' -> 'BTC-USD'. 'BTCUSDT' -> 'BTC-USD'
    (Yahoo has no USDT pairs; USD is the closest approximation — informational
    only, never used for execution). Already-hyphenated or plain equity
    symbols pass through unchanged.
    """
    sym = symbol.strip().upper()
    if ":" in sym:
        sym = sym.split(":", 1)[1]

    if "-" in sym:
        return sym

    for suffix in _CRYPTO_QUOTE_SUFFIXES:
        if sym.endswith(suffix) and len(sym) > len(suffix):
            base = sym[: -len(suffix)]
            if re.fullmatch(r"[A-Z]{2,10}", base):
                return f"{base}-USD"

    return sym


def get_live_price(symbol: str) -> Optional[float]:
    """Synchronous, cached (20s) live-price lookup. Returns None on any failure."""
    norm = normalize_symbol(symbol)
    now = time.monotonic()

    cached = _cache.get(norm)
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    price: Optional[float] = None
    try:
        info = yf.Ticker(norm).fast_info
        raw = info.get("lastPrice")
        if raw is not None and raw > 0:
            price = float(raw)
    except Exception:
        price = None

    _cache[norm] = (now, price)
    return price


async def get_live_price_async(symbol: str) -> Optional[float]:
    """Same as get_live_price but off the event loop, with a hard timeout so a
    slow/hanging Yahoo request can never stall a webhook response."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(get_live_price, symbol), timeout=_FETCH_TIMEOUT_SECONDS
        )
    except (TimeoutError, asyncio.TimeoutError):
        return None


def price_sanity_note(submitted_price: float, live_price: Optional[float]) -> str:
    """Human-readable, non-blocking note comparing a signal's price to the
    live quote at the time it arrived."""
    if live_price is None:
        return "no live quote available for comparison"
    diff_pct = (submitted_price - live_price) / live_price * 100
    if abs(diff_pct) <= 3:
        return f"OK (live ${live_price:.2f}, {diff_pct:+.2f}%)"
    return f"SUSPICIOUS: submitted ${submitted_price:.2f} vs live ${live_price:.2f} ({diff_pct:+.1f}%)"
