"""sim_runner.py — headless bot simulator (the L2 telemetry feeder).

Runs N automated playthroughs of game.py with zero human input.

  Bot_Alpha : random weapon from enabled items each game
  Bot_Beta  : always Iron Sword (control)

Each game runs in its own thread until player death or floor 10 cleared.
A thread killed by an unpatched bug (GameCrash) is caught, recorded, and
the simulation continues — exactly the environment the self-healing daemon
operates in. Between games, if the healer signalled a module reload over
the control channel, the supervisor hot-swaps game.py via importlib.

Usage:
  python sim_runner.py --games 100
  python sim_runner.py --games 50 --force-weapon "Cursed Blade"   (chaos testing)
"""
from __future__ import annotations

import argparse
import importlib
import threading
from datetime import datetime, timezone

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from cardinal.adapters.python_rpg import PythonRPGAdapter
from cardinal.core import db
from cardinal.core.config import SEV_CRITICAL, SEV_INFO, log_event
from cardinal.modules.balancer import gini_coefficient

console = Console()


def _update_bot_status(name: str, state: str, summary: dict | None = None) -> None:
    s = summary or {}
    db.execute(
        """INSERT INTO bot_status (bot_name, state, hp, gold, floor, weapon, games_played, games_won, updated_at)
           VALUES (?,?,?,?,?,?,
                   COALESCE((SELECT games_played FROM bot_status WHERE bot_name=?), 0) + ?,
                   COALESCE((SELECT games_won FROM bot_status WHERE bot_name=?), 0) + ?,
                   ?)
           ON CONFLICT(bot_name) DO UPDATE SET
             state=excluded.state, hp=excluded.hp, gold=excluded.gold, floor=excluded.floor,
             weapon=excluded.weapon, games_played=excluded.games_played,
             games_won=excluded.games_won, updated_at=excluded.updated_at""",
        (name, state, 0, s.get("gold", 0), s.get("floor", 1), s.get("weapon"),
         name, 1 if summary else 0, name, 1 if (s.get("cleared")) else 0,
         datetime.now(timezone.utc).isoformat()),
    )


def run_simulation(games: int, max_floor: int = 10,
                   force_weapon: str | None = None, seed: int | None = None) -> dict:
    import game

    db.init_db()
    adapter = PythonRPGAdapter()
    crashes: list[str] = []
    results: list[dict] = []
    bot_gold: dict[str, int] = {"Bot_Alpha": 0, "Bot_Beta": 0}

    with Progress(
        TextColumn("[bold cyan]simulating[/bold cyan]"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("games", total=games)
        for n in range(games):
            # Safe boundary: hot-swap the game module if the healer patched it.
            adapter.poll_control()
            if adapter.reload_module_requested:
                importlib.reload(game)
                adapter.reload_module_requested = False
                log_event("sim", "game module hot-swapped after Cardinal patch", SEV_INFO)
                console.print(Panel("[bold green]\\[CARDINAL] Bug repaired. System restored.[/bold green]",
                                    border_style="green"))

            bot = "Bot_Alpha" if n % 2 == 0 else "Bot_Beta"
            weapon = (force_weapon if bot == "Bot_Alpha" else None) if force_weapon else (
                None if bot == "Bot_Alpha" else "Iron Sword")
            db.update_bot_live_status(
                bot, "running", floor=1,
                weapon=weapon if isinstance(weapon, str) else None,
            )

            outcome: dict = {}
            error: list[BaseException] = []

            def play() -> None:
                try:
                    outcome.update(game.run_game(
                        bot, weapon, max_floor=max_floor,
                        seed=None if seed is None else seed + n, adapter=adapter))
                except BaseException as exc:  # noqa: BLE001 — thread boundary
                    error.append(exc)

            thread = threading.Thread(target=play, name=f"game-{n}-{bot}", daemon=True)
            thread.start()
            thread.join(timeout=120)

            if error:
                exc = error[0]
                crashes.append(f"game {n} ({bot}): {type(exc).__name__}: {exc}")
                log_event("sim", f"game thread crashed: {exc}", SEV_CRITICAL)
                _update_bot_status(bot, "crashed", {"weapon": weapon})
            elif outcome:
                results.append(outcome)
                bot_gold[bot] += outcome["gold"]
                _update_bot_status(bot, "idle", outcome)
            progress.advance(task)

    return {"results": results, "crashes": crashes, "bot_gold": bot_gold}


def print_summary(sim: dict) -> None:
    results, crashes = sim["results"], sim["crashes"]

    weapon_stats: dict[str, dict[str, int]] = {}
    for r in results:
        st = weapon_stats.setdefault(r["weapon"], {"games": 0, "cleared": 0})
        st["games"] += 1
        st["cleared"] += 1 if r["cleared"] else 0

    table = Table(title="Simulation Summary — win rates per weapon", header_style="bold magenta")
    for col in ("Weapon", "Games", "Cleared", "Clear rate"):
        table.add_column(col)
    for weapon, st in sorted(weapon_stats.items(), key=lambda kv: -kv[1]["games"]):
        rate = st["cleared"] / st["games"] if st["games"] else 0
        table.add_row(weapon, str(st["games"]), str(st["cleared"]), f"{rate:.1%}")
    console.print(table)

    wealth = db.get_player_wealth_list()
    gini = gini_coefficient(wealth)
    style = "red" if gini > 0.7 else "green"
    console.print(f"Gini coefficient (player wealth): [{style}]{gini:.3f}[/{style}]")
    console.print(f"Bot gold this run: {sim['bot_gold']}")

    if crashes:
        console.print(Panel("\n".join(crashes), title=f"[red]{len(crashes)} crash event(s) caught[/red]",
                            border_style="red"))
    else:
        console.print("[green]No crash events.[/green]")

    combat_win_rates = db.get_item_win_rates()
    if combat_win_rates:
        t2 = Table(title="Per-encounter win rates (combat_log)", header_style="bold magenta")
        t2.add_column("Weapon")
        t2.add_column("Win rate", justify="right")
        for name, wr in sorted(combat_win_rates.items(), key=lambda kv: -kv[1]):
            t2.add_row(name, f"{wr:.2%}")
        console.print(t2)


def main() -> None:
    import time

    parser = argparse.ArgumentParser(description="Cardinal headless bot simulator")
    parser.add_argument("--games", type=int, default=100)
    parser.add_argument("--max-floor", type=int, default=10)
    parser.add_argument("--force-weapon", type=str, default=None,
                        help="force Bot_Alpha to use this weapon (chaos testing)")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--watch", action="store_true",
                        help="run batches continuously so the dashboard stays live")
    parser.add_argument("--batch", type=int, default=20,
                        help="games per batch when using --watch")
    parser.add_argument("--pause", type=float, default=1.0,
                        help="seconds between batches in --watch mode")
    args = parser.parse_args()

    if args.watch:
        batch = max(1, args.batch)
        console.print(f"[cyan]Watch mode: {batch} games/batch, {args.pause}s pause — Ctrl+C to stop[/cyan]")
        try:
            while True:
                sim = run_simulation(batch, args.max_floor, args.force_weapon, args.seed)
                print_summary(sim)
                time.sleep(max(0.0, args.pause))
        except KeyboardInterrupt:
            console.print("\n[dim]sim_runner watch stopped[/dim]")
    else:
        sim = run_simulation(args.games, args.max_floor, args.force_weapon, args.seed)
        print_summary(sim)


if __name__ == "__main__":
    main()
