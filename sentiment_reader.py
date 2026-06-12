"""Community sentiment reader entry point.

  python sentiment_reader.py          daemon on the configured schedule (default 6h)
  python sentiment_reader.py --once   single cycle, then exit
"""
from __future__ import annotations

import argparse
import asyncio

from cardinal.modules.sentiment import daemon, run_cycle


def main() -> None:
    parser = argparse.ArgumentParser(description="Cardinal community sentiment reader")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        stored = asyncio.run(run_cycle())
        print(f"[CARDINAL] sentiment readings stored: {stored}")
    else:
        asyncio.run(daemon())


if __name__ == "__main__":
    main()
