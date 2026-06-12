"""Admin backdoor — God-level scenario injection CLI.

A deliberate development/demonstration tool that injects EVENTS directly,
bypassing the Sub-Process gate. It is not a security hole because:
  - it injects events, never balance/code mutations (those still have no
    path around the gate)
  - every action REQUIRES a --reason and is written to admin_override_log
  - the dashboard renders admin overrides as a clearly labeled separate
    feed, visually distinct from autonomous Cardinal actions

Usage (run from the project root):
  python -m cardinal.admin.cli force-incarnate Bot_Alpha 15 --reason "demo for stream"
  python -m cardinal.admin.cli inject-weather storm --reason "test weather hooks"
  python -m cardinal.admin.cli set-gini 0.85 --reason "trigger balancer danger path"
  python -m cardinal.admin.cli trigger-sanctuary Bot_Beta --reason "test MHCP sanctuary"
  python -m cardinal.admin.cli force-quest "Ragnarok" --reason "content demo"
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel

from cardinal.core import db
from cardinal.core.config import SEV_WARNING, log_event

console = Console()


def _audit(action_type: str, payload: dict, reason: str) -> int:
    row_id = db.execute(
        """INSERT INTO admin_override_log (action_type, payload_json, reason, timestamp)
           VALUES (?,?,?,?)""",
        (action_type, json.dumps(payload), reason, datetime.now(timezone.utc).isoformat()),
    )
    log_event("admin", f"OVERRIDE #{row_id} {action_type} {payload} — reason: {reason}", SEV_WARNING)
    console.print(Panel(
        f"[bold yellow]ADMIN OVERRIDE #{row_id}[/bold yellow]\n"
        f"action:  {action_type}\npayload: {payload}\nreason:  {reason}",
        border_style="yellow"))
    return row_id


def force_incarnate(player: str, duration: float, reason: str) -> None:
    db.push_control("incarnate", {"player": player, "duration_s": duration})
    db.execute(
        """INSERT INTO incarnate_log (player_name, x, y, duration_s, source, timestamp)
           VALUES (?,?,?,?,?,?)""",
        (player, None, None, duration, "admin_override", datetime.now(timezone.utc).isoformat()),
    )
    _audit("force-incarnate", {"player": player, "duration_s": duration}, reason)


def inject_weather(weather_type: str, reason: str) -> None:
    db.set_world_state("weather", {"type": weather_type, "injected": True}, changed_by="admin_override")
    db.push_control("world_changed", {"key": "weather"})
    _audit("inject-weather", {"type": weather_type}, reason)


def set_gini(value: float, reason: str) -> None:
    """Skew the wealth distribution so the next balancer cycle reads ~value.
    Implemented by injecting a synthetic whale/pauper pair of test players."""
    from cardinal.modules.taboo_index import admin_gold_lawful, get_taboo_index

    whale_gold = int(1_000_000 * max(0.0, min(value, 0.99)))
    if not admin_gold_lawful(whale_gold):
        max_g = get_taboo_index().laws["player_laws"]["max_gold"]
        console.print(Panel(
            f"[bold red]BLOCKED[/bold red] whale gold {whale_gold} exceeds Taboo max_gold ({max_g})",
            border_style="red"))
        _audit("set-gini-blocked", {"target": value, "whale_gold": whale_gold}, reason)
        return
    db.upsert_player("_admin_whale", 1, 1, whale_gold, 1, [])
    db.upsert_player("_admin_pauper", 1, 1, 0, 1, [])
    _audit("set-gini", {"target": value}, reason)


def trigger_sanctuary(player: str, reason: str) -> None:
    from cardinal.modules.mhcp import trigger_sanctuary as sanctuary

    sanctuary(player, reason=f"admin override: {reason}", source="admin_override")
    _audit("trigger-sanctuary", {"player": player}, reason)


def force_quest(topic: str, reason: str) -> None:
    _audit("force-quest", {"topic": topic}, reason)
    from cardinal.modules.quest_generator import generate_quest

    result = generate_quest(topic)
    style = "green" if result.get("ok") else "red"
    console.print(f"[{style}]{result}[/{style}]")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cardinal-admin",
        description="Cardinal admin backdoor — gate-bypassing scenario injection (fully audited)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("force-incarnate")
    p.add_argument("player")
    p.add_argument("duration", type=float, nargs="?", default=10.0)

    p = sub.add_parser("inject-weather")
    p.add_argument("type")

    p = sub.add_parser("set-gini")
    p.add_argument("value", type=float)

    p = sub.add_parser("trigger-sanctuary")
    p.add_argument("player")

    p = sub.add_parser("force-quest")
    p.add_argument("topic")

    for sp in sub.choices.values():
        sp.add_argument("--reason", required=True,
                        help="mandatory justification, recorded in admin_override_log")

    args = parser.parse_args()
    db.init_db()

    if args.command == "force-incarnate":
        force_incarnate(args.player, args.duration, args.reason)
    elif args.command == "inject-weather":
        inject_weather(args.type, args.reason)
    elif args.command == "set-gini":
        set_gini(args.value, args.reason)
    elif args.command == "trigger-sanctuary":
        trigger_sanctuary(args.player, args.reason)
    elif args.command == "force-quest":
        force_quest(args.topic, args.reason)


if __name__ == "__main__":
    main()
