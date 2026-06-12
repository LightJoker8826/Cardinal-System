"""Cognitive Telemetry Module — biometric ingestion + ghost echoes.

MODE A (hardware): async WebSocket server on CARDINAL_BIOMETRIC_PORT
  (default 8765) receiving JSON messages:
    {"player": "Bot_Alpha", "heart_rate": 142, "x": 5, "y": 3}
MODE B (simulation): synthetic heart-rate streams with random spikes,
  for testing without hardware (--simulate).

SPIKE DETECTION:
  baseline = rolling 10-sample average heart rate (per player)
  spike    = current reading > baseline * 1.4

ON SPIKE:
  1. Query current player state from cardinal.db
  2. Spatial cache dump (the ghost-echo imprint — canon: extreme emotion
     leaves a partial duplicate of the player's state on the coordinates)
  3. Incarnate Mode grant via the sequenced control channel (10 seconds:
     0.5x cooldown, 1.3x damage, +0.15 crit — willpower over system limits)
  4. MHCP distress tracking (the Yui module)
"""
from __future__ import annotations

import asyncio
import json
import random
from collections import deque
from datetime import datetime, timezone

from cardinal.core import db
from cardinal.core.config import SEV_DEBUG, SEV_INFO, SEV_WARNING, get_config, log_event
from cardinal.modules import mhcp

BASELINE_WINDOW = 10
SPIKE_FACTOR = 1.4
INCARNATE_DURATION_S = 10.0

_baselines: dict[str, deque] = {}


def process_reading(player: str, heart_rate: float, x: int, y: int) -> bool:
    """Process one biometric reading. Returns True if a spike fired."""
    hist = _baselines.setdefault(player, deque(maxlen=BASELINE_WINDOW))
    baseline = (sum(hist) / len(hist)) if hist else heart_rate
    hist.append(heart_rate)

    spiked = len(hist) >= 3 and heart_rate > baseline * SPIKE_FACTOR
    mhcp.record_reading(player, heart_rate, baseline, spiked)
    if not spiked:
        return False

    # 1. current player state
    row = db.query_one("SELECT * FROM players WHERE name=?", (player,))
    inventory = json.loads(row["inventory_json"]) if row else []

    # 2. spatial cache dump — the ghost echo imprint
    db.dump_spatial_state({
        "player_name": player,
        "x": x,
        "y": y,
        "action_state": "spike",
        "inventory": inventory,
        "biometric_value": heart_rate,
    })

    # 3. Incarnate Mode via the sequenced control channel
    db.push_control("incarnate", {"player": player, "duration_s": INCARNATE_DURATION_S,
                                  "x": x, "y": y})
    db.execute(
        """INSERT INTO incarnate_log (player_name, x, y, duration_s, source, timestamp)
           VALUES (?,?,?,?,?,?)""",
        (player, x, y, INCARNATE_DURATION_S, "biometric",
         datetime.now(timezone.utc).isoformat()),
    )
    log_event("biometrics",
              f"Incarnate Mode activated for {player} at ({x},{y}) "
              f"(hr {heart_rate:.0f} > {baseline:.0f} x {SPIKE_FACTOR})", SEV_INFO)
    try:
        from cardinal.modules import notifier

        notifier.notify_sync(
            "Incarnate Mode activated",
            f"**{player}** surpassed system constraints at ({x},{y}).",
            notifier.COLOR_PURPLE,
            {"heart_rate": f"{heart_rate:.0f} bpm", "baseline": f"{baseline:.0f} bpm",
             "duration": f"{INCARNATE_DURATION_S:.0f}s"},
        )
    except Exception:
        pass
    return True


def render_ghosts(current_x: int, current_y: int, radius: int = 2) -> list[dict]:
    """Ghost echoes within Manhattan distance radius of (x, y)."""
    echoes = db.get_spatial_echo(current_x, current_y, radius)
    return [{
        "player": e["player_name"],
        "x": e["x"],
        "y": e["y"],
        "inventory_snapshot": e["inventory_snapshot"],
        "age_seconds": e["age_seconds"],
    } for e in echoes]


# ===========================================================================
# MODE A — WebSocket hardware listener
# ===========================================================================

async def websocket_server() -> None:
    import websockets

    cfg = get_config()

    async def handler(websocket) -> None:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
                await asyncio.to_thread(
                    process_reading,
                    msg["player"], float(msg["heart_rate"]),
                    int(msg.get("x", 0)), int(msg.get("y", 0)))
            except (json.JSONDecodeError, KeyError, ValueError) as err:
                log_event("biometrics", f"malformed biometric message: {err}", SEV_WARNING)

    log_event("biometrics", f"WebSocket listener on ws://localhost:{cfg.biometric_port}", SEV_INFO)
    async with websockets.serve(handler, "localhost", cfg.biometric_port):
        await asyncio.Future()  # serve forever


# ===========================================================================
# MODE B — synthetic simulation
# ===========================================================================

async def simulate(players: list[str] | None = None, interval_s: float = 1.0,
                   iterations: int | None = None) -> None:
    players = players or ["Bot_Alpha", "Bot_Beta"]
    rng = random.Random()
    base = {p: rng.uniform(62, 78) for p in players}
    log_event("biometrics", f"simulation mode: {players} every {interval_s}s", SEV_INFO)
    n = 0
    while iterations is None or n < iterations:
        n += 1
        for player in players:
            hr = base[player] + rng.gauss(0, 4)
            if rng.random() < 0.06:  # random stress spike
                hr = base[player] * rng.uniform(1.5, 1.9)
            spiked = await asyncio.to_thread(
                process_reading, player, hr,
                rng.randrange(10), rng.randrange(10))
            if spiked:
                log_event("biometrics", f"[sim] spike for {player}: {hr:.0f} bpm", SEV_DEBUG)
        await asyncio.sleep(interval_s)
