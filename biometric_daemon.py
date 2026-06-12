"""Cognitive Telemetry entry point.

  python biometric_daemon.py                listen for hardware on ws://localhost:8765
  python biometric_daemon.py --simulate     synthetic heart-rate streams (no hardware)
  python biometric_daemon.py --simulate --iterations 30 --interval 0.2
"""
from __future__ import annotations

import argparse
import asyncio

from cardinal.core import db
from cardinal.modules.biometrics import simulate, websocket_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Cardinal Cognitive Telemetry daemon")
    parser.add_argument("--simulate", action="store_true",
                        help="generate synthetic biometric data instead of listening")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--iterations", type=int, default=None,
                        help="simulation cycles (default: run forever)")
    parser.add_argument("--players", nargs="*", default=None)
    args = parser.parse_args()

    db.init_db()
    if args.simulate:
        asyncio.run(simulate(args.players, args.interval, args.iterations))
    else:
        asyncio.run(websocket_server())


if __name__ == "__main__":
    main()
