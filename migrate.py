"""Cardinal System — schema migration runner.

THE single source of truth for the entire cardinal.db schema.
Every table used by any Cardinal module is defined here and ONLY here.
No module is permitted to create tables independently; cardinal.core.db
delegates to run_migrations() on initialization.

Properties:
  - Idempotent: safe to run any number of times.
  - Additive: new columns can be appended to existing tables without
    destroying data (versioned migration steps below).
  - Tracked: applied steps are recorded in schema_migrations.

Usage:
    python migrate.py            # apply pending migrations
    python migrate.py --status   # show applied/pending steps
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "cardinal.db"

# ---------------------------------------------------------------------------
# Migration steps. NEVER edit an applied step — append a new one instead.
# Each step is (id, description, [sql statements]).
# ---------------------------------------------------------------------------
MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "core tables from the master prompt",
        [
            """CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                hp INTEGER NOT NULL,
                max_hp INTEGER NOT NULL,
                gold INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 1,
                inventory_json TEXT NOT NULL DEFAULT '[]',
                timestamp TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS combat_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER,
                player_name TEXT,
                enemy TEXT NOT NULL,
                outcome TEXT NOT NULL,
                damage_dealt REAL NOT NULL DEFAULT 0,
                gold_earned INTEGER NOT NULL DEFAULT 0,
                weapon_used TEXT,
                floor INTEGER NOT NULL DEFAULT 1,
                duration_ticks INTEGER NOT NULL DEFAULT 1,
                timestamp TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS item_stats (
                item_name TEXT PRIMARY KEY,
                times_used INTEGER NOT NULL DEFAULT 0,
                total_wins INTEGER NOT NULL DEFAULT 0,
                total_losses INTEGER NOT NULL DEFAULT 0,
                avg_damage REAL NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS bugs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                error_type TEXT NOT NULL,
                file TEXT NOT NULL,
                line INTEGER,
                traceback TEXT,
                status TEXT NOT NULL DEFAULT 'detected',
                patch_applied TEXT,
                timestamp TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS balance_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gini_before REAL,
                gini_after REAL,
                changed_fields_json TEXT NOT NULL DEFAULT '{}',
                trigger_reason TEXT,
                timestamp TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS quest_registry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                source_url TEXT,
                source_name TEXT,
                gdd_json TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS spatial_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_name TEXT NOT NULL,
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                action_state TEXT NOT NULL,
                inventory_json TEXT NOT NULL DEFAULT '[]',
                biometric_value REAL,
                timestamp TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS agent_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                module TEXT NOT NULL,
                action TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'unknown',
                input_summary TEXT,
                output_summary TEXT,
                tokens_used INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL
            )""",
        ],
    ),
    (
        2,
        "canon-faithful extensions: world state, MHCP, control channel",
        [
            """CREATE TABLE IF NOT EXISTS world_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                value_json TEXT NOT NULL,
                changed_by TEXT,
                timestamp TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS mhcp_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_name TEXT NOT NULL,
                distress_index REAL NOT NULL,
                assessment TEXT,
                intervention TEXT,
                permitted INTEGER NOT NULL DEFAULT 1,
                critical INTEGER NOT NULL DEFAULT 0,
                timestamp TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS control_channel (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence_id INTEGER NOT NULL,
                target TEXT NOT NULL DEFAULT 'game',
                command TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                expires_at TEXT,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_control_seq ON control_channel(target, sequence_id)",
        ],
    ),
    (
        3,
        "meta-systems: sentiment, replay, spend, SEC, versions, admin overrides",
        [
            """CREATE TABLE IF NOT EXISTS sentiment_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                topic TEXT NOT NULL,
                sentiment_score REAL NOT NULL,
                engagement_weight REAL NOT NULL DEFAULT 0,
                post_url TEXT,
                excerpt TEXT,
                routed_to TEXT,
                timestamp TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS replay_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                module TEXT NOT NULL,
                state_before_json TEXT,
                state_after_json TEXT,
                llm_input TEXT,
                llm_output TEXT,
                gate_decision TEXT,
                gate_reasons TEXT,
                outcome TEXT,
                timestamp TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS api_spend (
                day TEXT PRIMARY KEY,
                spend_usd REAL NOT NULL DEFAULT 0,
                calls INTEGER NOT NULL DEFAULT 0,
                locked_out INTEGER NOT NULL DEFAULT 0,
                warned_80 INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS sec_state (
                enemy_type TEXT PRIMARY KEY,
                policy_low TEXT NOT NULL,
                policy_high TEXT NOT NULL,
                blend_ratio REAL NOT NULL DEFAULT 0.0,
                entropy REAL NOT NULL DEFAULT 0.0,
                adaptive_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS enemy_genomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                enemy_name TEXT NOT NULL,
                quest_title TEXT,
                genome_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                replay_id INTEGER,
                triggered_by TEXT NOT NULL,
                mutation_type TEXT NOT NULL,
                mutation_summary TEXT,
                items_json TEXT,
                world_state_json TEXT,
                sec_state_json TEXT,
                quests_json TEXT,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS admin_override_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                reason TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )""",
        ],
    ),
    (
        4,
        "incarnate/bot runtime status for dashboard",
        [
            """CREATE TABLE IF NOT EXISTS bot_status (
                bot_name TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                hp INTEGER NOT NULL DEFAULT 0,
                gold INTEGER NOT NULL DEFAULT 0,
                floor INTEGER NOT NULL DEFAULT 1,
                weapon TEXT,
                games_played INTEGER NOT NULL DEFAULT 0,
                games_won INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS incarnate_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_name TEXT NOT NULL,
                x INTEGER,
                y INTEGER,
                duration_s REAL NOT NULL DEFAULT 10,
                source TEXT NOT NULL DEFAULT 'biometric',
                timestamp TEXT NOT NULL
            )""",
        ],
    ),
    (
        5,
        "Fluctlight memory, player pain_memory, axiom violation log",
        [
            """CREATE TABLE IF NOT EXISTS entity_registry (
                entity_id TEXT PRIMARY KEY,
                enemy_type TEXT NOT NULL,
                genome_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'alive',
                survival_count INTEGER NOT NULL DEFAULT 0,
                experience_count INTEGER NOT NULL DEFAULT 0,
                lineage_parent_id TEXT,
                epithet TEXT,
                biography_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS encounter_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                enemy_type TEXT NOT NULL,
                outcome TEXT NOT NULL,
                weapon_used TEXT,
                floor INTEGER NOT NULL DEFAULT 1,
                pain_delta REAL NOT NULL DEFAULT 0,
                genome_snapshot_json TEXT,
                timestamp TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_encounter_player_enemy ON encounter_memory(player_name, enemy_type)",
            "CREATE INDEX IF NOT EXISTS idx_encounter_entity ON encounter_memory(entity_id)",
            """CREATE TABLE IF NOT EXISTS player_grudges (
                player_name TEXT NOT NULL,
                enemy_type TEXT NOT NULL,
                kills_by_player INTEGER NOT NULL DEFAULT 0,
                losses_to_player INTEGER NOT NULL DEFAULT 0,
                flees INTEGER NOT NULL DEFAULT 0,
                pain_memory REAL NOT NULL DEFAULT 0,
                dominant_weapon TEXT,
                last_floor INTEGER NOT NULL DEFAULT 1,
                last_entity_id TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (player_name, enemy_type)
            )""",
            """CREATE TABLE IF NOT EXISTS axiom_violation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mutation_type TEXT NOT NULL,
                module TEXT NOT NULL,
                violation TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                rollback_version_id INTEGER,
                timestamp TEXT NOT NULL
            )""",
        ],
    ),
]


def _connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def run_migrations(db_path: Path | str = DB_PATH, quiet: bool = False) -> list[int]:
    """Apply all pending migration steps. Returns the list of step ids applied."""
    conn = _connect(db_path)
    applied: list[int] = []
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                id INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )"""
        )
        done = {row[0] for row in conn.execute("SELECT id FROM schema_migrations")}
        for step_id, description, statements in MIGRATIONS:
            if step_id in done:
                continue
            with conn:
                for sql in statements:
                    conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (id, description, applied_at) VALUES (?,?,?)",
                    (step_id, description, datetime.now(timezone.utc).isoformat()),
                )
            applied.append(step_id)
            if not quiet:
                print(f"[migrate] applied step {step_id}: {description}")
        if not quiet and not applied:
            print("[migrate] schema up to date (no pending steps)")
    finally:
        conn.close()
    return applied


def status(db_path: Path | str = DB_PATH) -> None:
    conn = _connect(db_path)
    try:
        try:
            done = {row[0] for row in conn.execute("SELECT id FROM schema_migrations")}
        except sqlite3.OperationalError:
            done = set()
        for step_id, description, _ in MIGRATIONS:
            mark = "applied" if step_id in done else "PENDING"
            print(f"  step {step_id:>2}  [{mark}]  {description}")
    finally:
        conn.close()


if __name__ == "__main__":
    if "--status" in sys.argv:
        status()
    else:
        run_migrations()
