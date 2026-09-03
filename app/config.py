"""Central configuration, loaded from environment / .env. No secrets hardcoded."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val not in (None, "") else default


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    # --- security ---
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")

    # --- storage ---
    # DB_PATH/LOG_PATH overrides let a second instance of this same app serve
    # a different database (e.g. a backtest run) on a different port, reusing
    # the dashboard/API as-is instead of building a separate viewer.
    data_dir: Path = BASE_DIR / "data"
    db_path: Path = Path(os.environ["DB_PATH"]) if os.getenv("DB_PATH") else BASE_DIR / "data" / "trading.db"
    log_path: Path = (
        Path(os.environ["LOG_PATH"]) if os.getenv("LOG_PATH")
        else BASE_DIR / "data" / "logs" / "webhook.log"
    )

    # --- duplicate-signal protection ---
    dedup_window_seconds: int = _int("DEDUP_WINDOW_SECONDS", 30)

    # --- paper account / risk rules ---
    starting_equity: float = _float("STARTING_EQUITY", 100_000.0)
    max_risk_per_trade_pct: float = _float("MAX_RISK_PER_TRADE_PCT", 1.0)
    max_position_notional_pct: float = _float("MAX_POSITION_NOTIONAL_PCT", 20.0)
    max_open_positions: int = _int("MAX_OPEN_POSITIONS", 10)
    max_daily_loss_pct: float = _float("MAX_DAILY_LOSS_PCT", 5.0)
    allow_shorts: bool = _bool("ALLOW_SHORTS", False)

    # --- kill switch (config-flag form; DB flag via CLI is the live-toggle form) ---
    kill_switch_env: bool = _bool("KILL_SWITCH", False)

    # --- server ---
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = _int("PORT", 8000)


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.log_path.parent.mkdir(parents=True, exist_ok=True)
