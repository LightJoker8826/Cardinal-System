"""Social Control entry point — autonomous quest generation.

  python quest_generator.py --topic "Ragnarok"
  python quest_generator.py --topic "Aztec Empire" --source gutenberg
  python quest_generator.py --random
  python quest_generator.py --list-sources
"""
from __future__ import annotations

import argparse

from rich.console import Console
from rich.panel import Panel

from cardinal.modules.quest_generator import generate_quest
from cardinal.sources.base import available_sources

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Cardinal Social Control — quest generator")
    parser.add_argument("--topic", type=str, default=None)
    parser.add_argument("--random", action="store_true",
                        help="pick a random Wikipedia featured article")
    parser.add_argument("--source", type=str, default="wikipedia",
                        help="topic source adapter (wikipedia, osm, gutenberg)")
    parser.add_argument("--list-sources", action="store_true")
    args = parser.parse_args()

    if args.list_sources:
        for name, enabled in available_sources().items():
            console.print(f"  {name:<12} {'[green]enabled[/green]' if enabled else '[dim]disabled (stub)[/dim]'}")
        return
    if not args.topic and not args.random:
        parser.error("provide --topic KEYWORD or --random")

    result = generate_quest(args.topic, source_name=args.source, random_topic=args.random)
    if result.get("ok"):
        console.print(Panel(
            f"[bold green]{result['title']}[/bold green]\n"
            f"archetype: {result['archetype']}\n"
            f"enemies:   {', '.join(result['enemies'])}\n"
            f"assets:    {result['asset_file']}\n"
            f"quality:   {result['quality']}\n"
            f"provider:  {result['provider']}  |  version: v{result['version']}",
            title="[CARDINAL] Quest installed", border_style="green"))
    else:
        console.print(Panel(f"[red]{result.get('reason')}[/red]",
                            title="[CARDINAL] Quest generation failed", border_style="red"))


if __name__ == "__main__":
    main()
