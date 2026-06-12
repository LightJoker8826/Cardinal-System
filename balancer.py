"""Order Control entry point.

  python balancer.py --check    print stats, change nothing
  python balancer.py            run one balance cycle (mutations via the gate)
  python balancer.py --watch    daemon: run a cycle every 60 seconds
"""
from __future__ import annotations

import argparse
import asyncio

from cardinal.core import db
from cardinal.modules import balancer


def main() -> None:
    parser = argparse.ArgumentParser(description="Cardinal Order Control")
    parser.add_argument("--check", action="store_true", help="print stats only, no changes")
    parser.add_argument("--watch", action="store_true", help="run as daemon (every --interval s)")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--no-questgen", action="store_true",
                        help="disable churn-triggered quest generation")
    args = parser.parse_args()

    db.init_db()
    if args.check:
        balancer.print_report(balancer.build_report())
    elif args.watch:
        asyncio.run(balancer.watch(args.interval, allow_questgen=not args.no_questgen))
    else:
        result = balancer.run_balance_cycle(allow_questgen=not args.no_questgen)
        balancer.print_report(result["report"])


if __name__ == "__main__":
    main()
