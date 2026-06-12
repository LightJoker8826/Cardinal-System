"""Error Control entry point — the self-healing daemon.

Set ANTHROPIC_API_KEY in your .env file or shell environment before running
this module if you want L3 (Fable 5) repairs. Without it, the deterministic
L2 LocalRuleProvider repairs the known bug classes locally.

  python self_healing.py                 run the daemon (poll every 2s)
  python self_healing.py --once          single pass over new log entries
  python self_healing.py --from-start    process the whole existing log
"""
from __future__ import annotations

import argparse
import asyncio

from cardinal.modules.self_healing import daemon


def main() -> None:
    parser = argparse.ArgumentParser(description="Cardinal Error Control daemon")
    parser.add_argument("--once", action="store_true", help="process pending entries and exit")
    parser.add_argument("--from-start", action="store_true",
                        help="process the entire existing server.log, not just new entries")
    args = parser.parse_args()
    asyncio.run(daemon(once=args.once, from_start=args.from_start))


if __name__ == "__main__":
    main()
