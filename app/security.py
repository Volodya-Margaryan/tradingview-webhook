"""Shared-secret verification. Constant-time comparison to avoid timing leaks."""
from __future__ import annotations

import hmac

from .config import settings


def is_valid_secret(provided: str | None) -> bool:
    if not provided or not settings.webhook_secret:
        return False
    return hmac.compare_digest(provided, settings.webhook_secret)
