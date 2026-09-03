"""Pydantic models for the incoming TradingView webhook payload.

Validation rules:
- secret: required string, checked against WEBHOOK_SECRET (see security.py)
- symbol: known ticker format, optionally "EXCHANGE:TICKER" (e.g. "NASDAQ:AAPL", "BTCUSDT")
- price: positive number
- timeframe: short alnum token (e.g. "5", "15", "1D", "240")
- action: BUY or SELL only
- strategy: 1-100 char non-empty string
"""
from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

SYMBOL_RE = re.compile(r"^([A-Z0-9]{2,15}:)?[A-Z0-9]{1,15}$")
TIMEFRAME_RE = re.compile(r"^[0-9A-Za-z]{1,6}$")


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class WebhookAlert(BaseModel):
    """Exact shape of the JSON body TradingView must POST."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    secret: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1, max_length=32)
    price: float
    timeframe: str = Field(..., min_length=1, max_length=6)
    action: Action
    strategy: str = Field(..., min_length=1, max_length=100)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not SYMBOL_RE.match(v):
            raise ValueError(
                f"invalid symbol format: {v!r} (expected e.g. AAPL or NASDAQ:AAPL)"
            )
        return v

    @field_validator("timeframe")
    @classmethod
    def validate_timeframe(cls, v: str) -> str:
        v = v.strip()
        if not TIMEFRAME_RE.match(v):
            raise ValueError(f"invalid timeframe format: {v!r}")
        return v

    @field_validator("price")
    @classmethod
    def validate_price(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("price must be a positive number")
        return v

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("strategy must not be empty")
        return v
