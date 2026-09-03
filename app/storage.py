"""SQLite persistence: signals, positions, trades, and key/value settings
(account cash/equity, kill switch state). Single file DB, WAL mode for
safe concurrent reads while the server writes.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    symbol TEXT,
    price REAL,
    timeframe TEXT,
    action TEXT,
    strategy TEXT,
    status TEXT NOT NULL,          -- accepted | rejected
    reason TEXT,
    raw_payload TEXT NOT NULL,
    price_check TEXT               -- informational live-price sanity note, never blocks
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,            -- long | short
    qty REAL NOT NULL,
    avg_price REAL NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    close_price REAL,
    status TEXT NOT NULL,          -- open | closed
    realized_pnl REAL,
    strategy TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    position_id INTEGER,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,            -- BUY | SELL
    qty REAL NOT NULL,
    price REAL NOT NULL,
    filled_at TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS settings_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol_action ON signals(symbol, action, received_at);
CREATE INDEX IF NOT EXISTS idx_positions_symbol_status ON positions(symbol, status);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    # WAL still allows exactly one writer at a time; a second process (e.g. the
    # CLI) writing at the same instant as an incoming webhook would otherwise
    # get "database is locked" immediately (default busy_timeout=0). Make it
    # retry for up to 5s instead of failing a real signal outright.
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _migrate_add_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_add_column(conn, "signals", "price_check", "TEXT")
    conn.commit()


# A single sqlite3.Connection shared across threads (even with
# check_same_thread=False) can produce corrupted/interleaved query results
# under concurrent load — this is a documented Python sqlite3 pitfall, not a
# hypothetical one (it's what caused the intermittent 500s seen once the
# dashboard started firing several concurrent requests per refresh). Each
# thread gets its own connection instead; WAL mode is specifically designed
# for many independent connections reading/writing one file safely.
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    cached = getattr(_local, "conn", None)
    cached_path = getattr(_local, "path", None)
    if cached is not None and cached_path == settings.db_path:
        return cached
    if cached is not None:
        try:
            cached.close()
        except Exception:
            pass
    conn = _connect()
    _ensure_schema(conn)
    _local.conn = conn
    _local.path = settings.db_path
    return conn


def init_db() -> None:
    """Ensure schema + default settings exist for the current thread's
    connection. Idempotent — safe to call repeatedly, including after
    settings.db_path changes (e.g. in tests)."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_schema(_get_conn())
    if get_setting("cash") is None:
        set_setting("cash", settings.starting_equity)
    if get_setting("realized_pnl_total") is None:
        set_setting("realized_pnl_total", 0.0)
    if get_setting("kill_switch") is None:
        set_setting("kill_switch", settings.kill_switch_env)


@contextmanager
def _cursor() -> Iterator[sqlite3.Cursor]:
    conn = _get_conn()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- settings_kv

def get_setting(key: str) -> Any:
    with _cursor() as cur:
        row = cur.execute("SELECT value FROM settings_kv WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return json.loads(row["value"])


def set_setting(key: str, value: Any) -> None:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO settings_kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


def is_kill_switch_on() -> bool:
    return bool(get_setting("kill_switch"))


def set_kill_switch(on: bool) -> None:
    set_setting("kill_switch", on)


# -------------------------------------------------------------------- signals

def log_signal(
    *,
    symbol: Optional[str],
    price: Optional[float],
    timeframe: Optional[str],
    action: Optional[str],
    strategy: Optional[str],
    status: str,
    reason: Optional[str],
    raw_payload: dict,
    price_check: Optional[str] = None,
) -> int:
    with _cursor() as cur:
        cur.execute(
            """INSERT INTO signals
               (received_at, symbol, price, timeframe, action, strategy, status, reason, raw_payload, price_check)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _now(),
                symbol,
                price,
                timeframe,
                action,
                strategy,
                status,
                reason,
                json.dumps(raw_payload),
                price_check,
            ),
        )
        return cur.lastrowid


def get_last_accepted_signal_time(symbol: str, action: str) -> Optional[datetime]:
    with _cursor() as cur:
        row = cur.execute(
            """SELECT received_at FROM signals
               WHERE symbol = ? AND action = ? AND status = 'accepted'
               ORDER BY id DESC LIMIT 1""",
            (symbol, action),
        ).fetchone()
    if row is None:
        return None
    return datetime.fromisoformat(row["received_at"])


def list_signals(limit: int = 50) -> list[dict]:
    with _cursor() as cur:
        rows = cur.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def list_signals_since(since_iso: str, limit: int = 500) -> list[dict]:
    with _cursor() as cur:
        rows = cur.execute(
            "SELECT * FROM signals WHERE received_at >= ? ORDER BY id DESC LIMIT ?",
            (since_iso, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------- session marker

def set_session_marker(label: str) -> str:
    """Record 'session start' as a DB timestamp (not just a log line) so
    reports can filter to 'since session start' precisely."""
    now = _now()
    set_setting("session_marker_at", now)
    set_setting("session_marker_label", label)
    return now


def get_session_marker() -> Optional[dict]:
    at = get_setting("session_marker_at")
    if at is None:
        return None
    return {"at": at, "label": get_setting("session_marker_label")}


# ------------------------------------------------------------------ positions

def get_open_position(symbol: str) -> Optional[dict]:
    with _cursor() as cur:
        row = cur.execute(
            "SELECT * FROM positions WHERE symbol = ? AND status = 'open' LIMIT 1",
            (symbol,),
        ).fetchone()
    return dict(row) if row else None


def count_open_positions() -> int:
    with _cursor() as cur:
        row = cur.execute("SELECT COUNT(*) AS n FROM positions WHERE status = 'open'").fetchone()
    return row["n"]


def open_position(symbol: str, side: str, qty: float, price: float, strategy: str,
                   at: Optional[str] = None) -> int:
    with _cursor() as cur:
        cur.execute(
            """INSERT INTO positions (symbol, side, qty, avg_price, opened_at, status, strategy)
               VALUES (?, ?, ?, ?, ?, 'open', ?)""",
            (symbol, side, qty, price, at or _now(), strategy),
        )
        return cur.lastrowid


def close_position(position_id: int, close_price: float, realized_pnl: float,
                    at: Optional[str] = None) -> None:
    with _cursor() as cur:
        cur.execute(
            """UPDATE positions SET status = 'closed', closed_at = ?, close_price = ?,
               realized_pnl = ? WHERE id = ?""",
            (at or _now(), close_price, realized_pnl, position_id),
        )


def list_positions(status: Optional[str] = None) -> list[dict]:
    with _cursor() as cur:
        if status:
            rows = cur.execute(
                "SELECT * FROM positions WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = cur.execute("SELECT * FROM positions ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def list_positions_closed_since(since_iso: str) -> list[dict]:
    with _cursor() as cur:
        rows = cur.execute(
            "SELECT * FROM positions WHERE status = 'closed' AND closed_at >= ? ORDER BY id DESC",
            (since_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------- trades

def record_trade(
    signal_id: Optional[int],
    position_id: Optional[int],
    symbol: str,
    side: str,
    qty: float,
    price: float,
    notes: str = "",
    at: Optional[str] = None,
) -> int:
    with _cursor() as cur:
        cur.execute(
            """INSERT INTO trades (signal_id, position_id, symbol, side, qty, price, filled_at, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (signal_id, position_id, symbol, side, qty, price, at or _now(), notes),
        )
        return cur.lastrowid


def list_trades(limit: int = 100) -> list[dict]:
    with _cursor() as cur:
        rows = cur.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def list_trades_since(since_iso: str, limit: int = 500) -> list[dict]:
    with _cursor() as cur:
        rows = cur.execute(
            "SELECT * FROM trades WHERE filled_at >= ? ORDER BY id DESC LIMIT ?",
            (since_iso, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------- account

def get_cash() -> float:
    return float(get_setting("cash"))


def adjust_cash(delta: float) -> None:
    set_setting("cash", get_cash() + delta)


def get_realized_pnl_total() -> float:
    return float(get_setting("realized_pnl_total"))


def add_realized_pnl(amount: float) -> None:
    set_setting("realized_pnl_total", get_realized_pnl_total() + amount)


def get_daily_realized_pnl() -> float:
    """Sum of realized PnL on positions closed in the last 24h (rolling day),
    floored at the last account reset so pre-reset trades never leak back in."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    reset_at = get_setting("account_reset_at")
    if reset_at is not None and reset_at > cutoff:
        cutoff = reset_at
    with _cursor() as cur:
        row = cur.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS total FROM positions "
            "WHERE status = 'closed' AND closed_at >= ?",
            (cutoff,),
        ).fetchone()
    return float(row["total"])


# ---------------------------------------------------------------- account reset

def reset_account() -> str:
    """Reset the paper account back to a clean starting_equity baseline.

    Does NOT delete signals/positions/trades — the audit trail is preserved
    for reference, it's just excluded from account totals and performance
    stats going forward via the account_reset_at marker.
    """
    now = _now()
    set_setting("cash", settings.starting_equity)
    set_setting("realized_pnl_total", 0.0)
    set_setting("account_reset_at", now)
    return now


def get_account_reset_marker() -> Optional[str]:
    return get_setting("account_reset_at")


def get_account_summary() -> dict:
    cash = get_cash()
    open_positions = list_positions(status="open")
    open_notional = sum(p["qty"] * p["avg_price"] for p in open_positions)
    equity = cash + open_notional
    realized_pnl_total = get_realized_pnl_total()
    daily_realized_pnl = get_daily_realized_pnl()
    return {
        "starting_equity": settings.starting_equity,
        "cash": cash,
        "open_positions_notional": open_notional,
        "equity": equity,
        "realized_pnl_total": realized_pnl_total,
        "daily_realized_pnl": daily_realized_pnl,
        "open_position_count": len(open_positions),
    }
