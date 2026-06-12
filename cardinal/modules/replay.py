"""Replay system — total auditability of Cardinal's decisions.

Every significant event is captured by the Sub-Process gate into replay_log:
full before/after state snapshots, the exact LLM input/output (if a call
was made), the gate decision with reasons, and the final outcome. Nothing
can mutate state unrecorded because the capture hooks live inside the gate.

This module renders those records for the CLI (`cardinal_core.py replay <id>`)
and provides query helpers for the dashboard's replay browser.
"""
from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from cardinal.core import db

console = Console()


def list_replays(limit: int = 50) -> list[dict[str, Any]]:
    return db.query(
        """SELECT id, event_type, module, gate_decision, outcome, timestamp
           FROM replay_log ORDER BY id DESC LIMIT ?""", (limit,))


def get_replay(replay_id: int) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM replay_log WHERE id=?", (replay_id,))


def diff_states(before_json: str | None, after_json: str | None) -> list[str]:
    """Human-readable diff of the items/world/sec snapshots."""
    if not before_json or not after_json:
        return ["(no after-state captured — mutation was rejected)"] if before_json else []
    before, after = json.loads(before_json), json.loads(after_json)
    changes: list[str] = []

    b_items = {i["name"]: i for i in before.get("items", [])}
    a_items = {i["name"]: i for i in after.get("items", [])}
    for name, a in a_items.items():
        b = b_items.get(name, {})
        for key, value in a.items():
            if b.get(key) != value:
                changes.append(f"items[{name}].{key}: {b.get(key)} -> {value}")

    for key, value in after.get("world_state", {}).items():
        if before.get("world_state", {}).get(key) != value:
            changes.append(f"world_state[{key}] changed")

    b_quests = {q.get("title") for q in before.get("quests", [])}
    for q in after.get("quests", []):
        if q.get("title") not in b_quests:
            changes.append(f"quest installed: {q.get('title')}")

    b_sec = {s["enemy_type"]: s for s in before.get("sec_state", [])}
    for s in after.get("sec_state", []):
        b = b_sec.get(s["enemy_type"], {})
        if b.get("blend_ratio") != s.get("blend_ratio"):
            changes.append(
                f"sec[{s['enemy_type']}].blend_ratio: {b.get('blend_ratio')} -> {s.get('blend_ratio')}")
    return changes or ["(no observable state delta)"]


def render_replay(replay_id: int) -> bool:
    row = get_replay(replay_id)
    if row is None:
        console.print(f"[red]replay {replay_id} not found[/red]")
        return False

    decision_style = "green" if row["gate_decision"] == "approved" else "red"
    console.print(Panel(
        f"[bold]{row['event_type']}[/bold] from [cyan]{row['module']}[/cyan]\n"
        f"decision: [{decision_style}]{row['gate_decision'].upper()}[/{decision_style}]\n"
        f"reasons:  {row['gate_reasons'] or '—'}\n"
        f"outcome:  {row['outcome']}\n"
        f"time:     {row['timestamp']}",
        title=f"Cardinal Replay #{replay_id}", border_style="magenta"))

    changes = diff_states(row["state_before_json"], row["state_after_json"])
    table = Table(title="State delta", header_style="bold magenta")
    table.add_column("Change")
    for change in changes[:40]:
        table.add_row(change)
    console.print(table)

    if row["llm_input"]:
        console.print(Panel(row["llm_input"][:2000], title="LLM input (exact)", border_style="blue"))
    if row["llm_output"]:
        console.print(Panel(row["llm_output"][:2000], title="LLM output (exact)", border_style="blue"))

    version = db.query_one("SELECT id FROM versions WHERE replay_id=?", (replay_id,))
    if version:
        console.print(f"[dim]linked system version: v{version['id']} "
                      f"(restore with: python cardinal_core.py rollback {version['id']})[/dim]")
    return True
