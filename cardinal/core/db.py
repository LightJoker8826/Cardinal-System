"""Cardinal state database layer.

Schema is owned exclusively by migrate.py (single source of truth) —
this module never creates tables; init_db() delegates to the migration
runner, which is the first thing that runs when any module initializes.

Concurrency: many independent Cardinal processes share cardinal.db.
WAL mode + busy_timeout + short-lived connections + a retry wrapper
keep concurrent writers safe on Windows.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from cardinal.core.config import get_config

_RETRIES = 5
_RETRY_DELAY_S = 0.25


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(db_path: Path | str | None = None) -> Path:
    """Ensure the schema exists. Delegates to migrate.py — always runs first."""
    import sys

    cfg = get_config()
    path = Path(db_path) if db_path else cfg.db_path
    sys.path.insert(0, str(cfg.project_root))
    try:
        from migrate import run_migrations

        run_migrations(path, quiet=True)
    finally:
        sys.path.pop(0)
    return path


@contextmanager
def connect(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Short-lived WAL connection with row factory."""
    path = Path(db_path) if db_path else get_config().db_path
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
    finally:
        conn.close()


def execute(sql: str, params: tuple = (), db_path: Path | str | None = None) -> int:
    """Write with retry. Returns lastrowid."""
    last_err: Exception | None = None
    for _ in range(_RETRIES):
        try:
            with connect(db_path) as conn:
                with conn:
                    cur = conn.execute(sql, params)
                return cur.lastrowid or 0
        except sqlite3.OperationalError as err:  # locked despite busy_timeout
            last_err = err
            time.sleep(_RETRY_DELAY_S)
    raise last_err  # type: ignore[misc]


def query(sql: str, params: tuple = (), db_path: Path | str | None = None) -> list[dict[str, Any]]:
    for attempt in range(_RETRIES):
        try:
            with connect(db_path) as conn:
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            if attempt == _RETRIES - 1:
                raise
            time.sleep(_RETRY_DELAY_S)
    return []


def query_one(sql: str, params: tuple = (), db_path: Path | str | None = None) -> dict[str, Any] | None:
    rows = query(sql, params, db_path)
    return rows[0] if rows else None


# ===========================================================================
# Prompt-mandated functions
# ===========================================================================

def update_bot_live_status(
    bot_name: str,
    state: str,
    *,
    hp: int = 0,
    gold: int = 0,
    floor: int = 1,
    weapon: str | None = None,
) -> None:
    """Refresh dashboard bot row mid-game without bumping game counters."""
    execute(
        """INSERT INTO bot_status
           (bot_name, state, hp, gold, floor, weapon, games_played, games_won, updated_at)
           VALUES (?,?,?,?,?,?,0,0,?)
           ON CONFLICT(bot_name) DO UPDATE SET
             state=excluded.state,
             hp=excluded.hp,
             gold=excluded.gold,
             floor=excluded.floor,
             weapon=excluded.weapon,
             updated_at=excluded.updated_at""",
        (bot_name, state, hp, gold, floor, weapon, _now()),
    )


def upsert_player(name: str, hp: int, max_hp: int, gold: int, level: int, inventory: list[str]) -> None:
    execute(
        """INSERT INTO players (name, hp, max_hp, gold, level, inventory_json, timestamp)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             hp=excluded.hp, max_hp=excluded.max_hp, gold=excluded.gold,
             level=excluded.level, inventory_json=excluded.inventory_json,
             timestamp=excluded.timestamp""",
        (name, hp, max_hp, gold, level, json.dumps(inventory), _now()),
    )


def log_combat(data: dict[str, Any]) -> int:
    rowid = execute(
        """INSERT INTO combat_log
           (player_id, player_name, enemy, outcome, damage_dealt, gold_earned,
            weapon_used, floor, duration_ticks, timestamp)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            data.get("player_id"),
            data.get("player_name"),
            data["enemy"],
            data["outcome"],
            data.get("damage_dealt", 0),
            data.get("gold_earned", 0),
            data.get("weapon_used"),
            data.get("floor", 1),
            data.get("duration_ticks", 1),
            _now(),
        ),
    )
    weapon = data.get("weapon_used")
    if weapon:
        won = 1 if data["outcome"] == "win" else 0
        execute(
            """INSERT INTO item_stats (item_name, times_used, total_wins, total_losses, avg_damage, timestamp)
               VALUES (?, 1, ?, ?, ?, ?)
               ON CONFLICT(item_name) DO UPDATE SET
                 times_used = times_used + 1,
                 total_wins = total_wins + excluded.total_wins,
                 total_losses = total_losses + excluded.total_losses,
                 avg_damage = (avg_damage * times_used + excluded.avg_damage) / (times_used + 1),
                 timestamp = excluded.timestamp""",
            (weapon, won, 1 - won, data.get("damage_dealt", 0), _now()),
        )
    return rowid


def log_bug(error_data: dict[str, Any]) -> int:
    return execute(
        """INSERT INTO bugs (error_type, file, line, traceback, status, timestamp)
           VALUES (?,?,?,?,?,?)""",
        (
            error_data["error_type"],
            error_data["file"],
            error_data.get("line"),
            error_data.get("traceback", ""),
            error_data.get("status", "detected"),
            _now(),
        ),
    )


def mark_bug_patched(bug_id: int, patch_code: str = "", status: str = "patched") -> None:
    execute(
        "UPDATE bugs SET status=?, patch_applied=? WHERE id=?",
        (status, patch_code, bug_id),
    )


def log_balance_change(data: dict[str, Any]) -> int:
    return execute(
        """INSERT INTO balance_log (gini_before, gini_after, changed_fields_json, trigger_reason, timestamp)
           VALUES (?,?,?,?,?)""",
        (
            data.get("gini_before"),
            data.get("gini_after"),
            json.dumps(data.get("changed_fields", {})),
            data.get("trigger_reason"),
            _now(),
        ),
    )


def get_item_win_rates(min_uses: int = 3) -> dict[str, float]:
    rows = query(
        """SELECT item_name, total_wins, total_losses FROM item_stats
           WHERE (total_wins + total_losses) >= ?""",
        (min_uses,),
    )
    out: dict[str, float] = {}
    for r in rows:
        total = r["total_wins"] + r["total_losses"]
        if total:
            out[r["item_name"]] = r["total_wins"] / total
    return out


def get_player_wealth_list() -> list[int]:
    rows = query("SELECT gold FROM players ORDER BY gold ASC")
    return [r["gold"] for r in rows]


def log_agent_action(data: dict[str, Any]) -> int:
    return execute(
        """INSERT INTO agent_log
           (module, action, provider, input_summary, output_summary, tokens_used, cost_usd, timestamp)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            data["module"],
            data["action"],
            data.get("provider", "unknown"),
            (data.get("input_summary") or "")[:2000],
            (data.get("output_summary") or "")[:2000],
            data.get("tokens_used", 0),
            data.get("cost_usd", 0.0),
            _now(),
        ),
    )


def dump_spatial_state(data: dict[str, Any]) -> int:
    return execute(
        """INSERT INTO spatial_cache
           (player_name, x, y, action_state, inventory_json, biometric_value, timestamp)
           VALUES (?,?,?,?,?,?,?)""",
        (
            data["player_name"],
            data["x"],
            data["y"],
            data.get("action_state", "spike"),
            json.dumps(data.get("inventory", [])),
            data.get("biometric_value"),
            _now(),
        ),
    )


def get_spatial_echo(x: int, y: int, r: int = 2) -> list[dict[str, Any]]:
    """Cached states within Manhattan distance r of (x, y)."""
    rows = query(
        "SELECT * FROM spatial_cache WHERE (ABS(x - ?) + ABS(y - ?)) <= ? ORDER BY timestamp DESC",
        (x, y, r),
    )
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            ts = datetime.fromisoformat(row["timestamp"])
            row["age_seconds"] = max(0.0, (now - ts).total_seconds())
        except (ValueError, TypeError):
            row["age_seconds"] = None
        row["inventory_snapshot"] = json.loads(row.get("inventory_json") or "[]")
    return rows


# ===========================================================================
# Control channel — Temporal Drift Compensation
# ===========================================================================
# Every row carries a monotonic sequence_id per target. Consumers (the game
# adapter) must apply updates strictly in order (current_id + 1); anything
# out-of-order is discarded and answered with a State Refresh Request.

def push_control(command: str, payload: dict[str, Any] | None = None,
                 target: str = "game", expires_at: str | None = None) -> int:
    """Append a sequenced control message. Returns the new sequence_id."""
    last_err: Exception | None = None
    for _ in range(_RETRIES):
        try:
            with connect() as conn:
                with conn:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(sequence_id), 0) AS s FROM control_channel WHERE target=?",
                        (target,),
                    ).fetchone()
                    seq = (row["s"] or 0) + 1
                    conn.execute(
                        """INSERT INTO control_channel
                           (sequence_id, target, command, payload_json, expires_at, created_at)
                           VALUES (?,?,?,?,?,?)""",
                        (seq, target, command, json.dumps(payload or {}), expires_at, _now()),
                    )
                return seq
        except sqlite3.OperationalError as err:
            last_err = err
            time.sleep(_RETRY_DELAY_S)
    raise last_err  # type: ignore[misc]


def pending_control(after_sequence: int, target: str = "game") -> list[dict[str, Any]]:
    rows = query(
        """SELECT * FROM control_channel
           WHERE target=? AND sequence_id > ? AND acknowledged=0
           ORDER BY sequence_id ASC""",
        (target, after_sequence),
    )
    for r in rows:
        r["payload"] = json.loads(r.get("payload_json") or "{}")
    return rows


def ack_control(sequence_id: int, target: str = "game") -> None:
    execute(
        "UPDATE control_channel SET acknowledged=1 WHERE target=? AND sequence_id<=?",
        (target, sequence_id),
    )


def latest_sequence(target: str = "game") -> int:
    row = query_one(
        "SELECT COALESCE(MAX(sequence_id), 0) AS s FROM control_channel WHERE target=?",
        (target,),
    )
    return int(row["s"]) if row else 0


# ===========================================================================
# World state
# ===========================================================================

def set_world_state(key: str, value: Any, changed_by: str = "cardinal") -> None:
    execute(
        """INSERT INTO world_state (key, value_json, changed_by, timestamp)
           VALUES (?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET
             value_json=excluded.value_json, changed_by=excluded.changed_by,
             timestamp=excluded.timestamp""",
        (key, json.dumps(value), changed_by, _now()),
    )


def get_world_state() -> dict[str, Any]:
    rows = query("SELECT key, value_json FROM world_state")
    return {r["key"]: json.loads(r["value_json"]) for r in rows}


# ===========================================================================
# Summary helpers (CLI / dashboard)
# ===========================================================================

def counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for table in ("players", "combat_log", "bugs", "quest_registry", "balance_log",
                  "agent_log", "spatial_cache", "versions", "replay_log",
                  "sentiment_log", "admin_override_log"):
        row = query_one(f"SELECT COUNT(*) AS c FROM {table}")  # table names are internal constants
        out[table] = int(row["c"]) if row else 0
    return out
