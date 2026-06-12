"""cardinal_core.py — the orchestrator's CLI (tbc-db).

Lets humans (and Fable 5, without bloating its context window with raw SQL)
inspect and operate the Cardinal state database.

  python cardinal_core.py status              DB summary stats
  python cardinal_core.py bugs                list unpatched bugs
  python cardinal_core.py balance             current Gini + top win rates
  python cardinal_core.py quests              list active quests
  python cardinal_core.py version             list all system versions
  python cardinal_core.py rollback <version>  restore a previous state (gated, append-only)
  python cardinal_core.py replay <id>         replay a recorded Cardinal event
"""
from __future__ import annotations

import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from cardinal.core import db
from cardinal.core.config import get_config

console = Console()


def cmd_status() -> None:
    counts = db.counts()
    spend = db.query_one("SELECT * FROM api_spend ORDER BY day DESC LIMIT 1")
    table = Table(title="Cardinal System Status", header_style="bold magenta")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("players", str(counts["players"]))
    table.add_row("combat records", str(counts["combat_log"]))
    unpatched = db.query_one("SELECT COUNT(*) AS c FROM bugs WHERE status != 'patched'")
    table.add_row("bugs (total / unpatched)", f"{counts['bugs']} / {unpatched['c'] if unpatched else 0}")
    active_quests = db.query_one("SELECT COUNT(*) AS c FROM quest_registry WHERE status='active'")
    table.add_row("quests (active)", str(active_quests["c"] if active_quests else 0))
    table.add_row("balance changes", str(counts["balance_log"]))
    table.add_row("agent calls", str(counts["agent_log"]))
    table.add_row("system versions", str(counts["versions"]))
    table.add_row("replay events", str(counts["replay_log"]))
    table.add_row("sentiment readings", str(counts["sentiment_log"]))
    table.add_row("admin overrides", str(counts["admin_override_log"]))
    if spend:
        cfg = get_config()
        table.add_row("API spend today",
                      f"${spend['spend_usd']:.2f} / ${cfg.max_daily_spend_usd:.2f}"
                      + ("  [red]LOCKED[/red]" if spend["locked_out"] else ""))
    console.print(table)
    console.print("[green]DB initialized.[/green]")


def cmd_bugs() -> None:
    rows = db.query("SELECT * FROM bugs WHERE status != 'patched' ORDER BY id DESC LIMIT 50")
    if not rows:
        console.print("[green]No unpatched bugs.[/green]")
        return
    table = Table(title="Unpatched Bugs", header_style="bold red")
    for col in ("id", "error_type", "file", "line", "status", "timestamp"):
        table.add_column(col)
    for r in rows:
        table.add_row(str(r["id"]), r["error_type"], r["file"], str(r["line"]), r["status"], r["timestamp"])
    console.print(table)


def cmd_balance() -> None:
    from cardinal.modules.balancer import build_report, print_report

    print_report(build_report())


def cmd_quests() -> None:
    rows = db.query(
        "SELECT id, title, source_name, source_url, status, created_at FROM quest_registry ORDER BY id DESC")
    if not rows:
        console.print("[dim]No quests in the registry yet.[/dim]")
        return
    table = Table(title="Quest Registry", header_style="bold magenta")
    for col in ("id", "title", "source", "status", "created"):
        table.add_column(col)
    for r in rows:
        style = "green" if r["status"] == "active" else "dim"
        table.add_row(str(r["id"]), f"[{style}]{r['title']}[/{style}]",
                      f"{r['source_name'] or '?'}", r["status"], r["created_at"][:19])
    console.print(table)


def cmd_version() -> None:
    rows = db.query(
        """SELECT id, triggered_by, mutation_type, mutation_summary, created_at
           FROM versions ORDER BY id DESC LIMIT 100""")
    if not rows:
        console.print("[dim]No versions yet — no mutations have been approved.[/dim]")
        return
    table = Table(title="Cardinal Version History (append-only)", header_style="bold magenta")
    for col in ("version", "module", "mutation", "summary", "created"):
        table.add_column(col)
    for r in rows:
        table.add_row(f"v{r['id']}", r["triggered_by"], r["mutation_type"],
                      (r["mutation_summary"] or "")[:70], r["created_at"][:19])
    console.print(table)


def cmd_rollback(version_id: int) -> None:
    from cardinal import sub_process

    result = sub_process.approve_mutation("rollback", "cli", {"version_id": version_id})
    if result.approved:
        console.print(Panel(
            f"[bold green]State restored from v{version_id}.[/bold green]\n"
            f"A new version (v{result.version_id}) was appended — history is never rewritten.",
            border_style="green"))
    else:
        console.print(Panel(f"[red]Rollback rejected: {result.reasons}[/red]", border_style="red"))


def cmd_replay(replay_id: int | None) -> None:
    from cardinal.modules.replay import list_replays, render_replay

    if replay_id is None:
        rows = list_replays()
        table = Table(title="Replay Log (most recent)", header_style="bold magenta")
        for col in ("id", "event", "module", "decision", "time"):
            table.add_column(col)
        for r in rows:
            style = "green" if r["gate_decision"] == "approved" else "red"
            table.add_row(str(r["id"]), r["event_type"], r["module"],
                          f"[{style}]{r['gate_decision']}[/{style}]", r["timestamp"][:19])
        console.print(table)
    else:
        render_replay(replay_id)


def main() -> None:
    db.init_db()  # migrations run first, always
    args = sys.argv[1:]
    command = args[0] if args else "status"
    if command == "status":
        cmd_status()
    elif command == "bugs":
        cmd_bugs()
    elif command == "balance":
        cmd_balance()
    elif command == "quests":
        cmd_quests()
    elif command == "version":
        cmd_version()
    elif command == "rollback":
        if len(args) < 2 or not args[1].isdigit():
            console.print("[red]usage: python cardinal_core.py rollback <version_id>[/red]")
            sys.exit(2)
        cmd_rollback(int(args[1]))
    elif command == "replay":
        cmd_replay(int(args[1]) if len(args) > 1 and args[1].isdigit() else None)
    else:
        console.print(f"[red]unknown command '{command}'[/red] — "
                      "try status | bugs | balance | quests | version | rollback | replay")
        sys.exit(2)


if __name__ == "__main__":
    main()
