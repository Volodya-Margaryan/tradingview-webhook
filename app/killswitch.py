"""Kill switch: stops all signal processing immediately.

Two layers, either one is enough to halt processing:
1. KILL_SWITCH=true in .env — requires a server restart to change.
2. A live DB flag toggled at any time via `python cli.py killswitch on|off`
   (no restart needed; checked on every incoming webhook request).
"""
from __future__ import annotations

from . import storage
from .config import settings


def is_active() -> bool:
    return settings.kill_switch_env or storage.is_kill_switch_on()


def set_active(on: bool) -> None:
    storage.set_kill_switch(on)
