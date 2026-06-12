"""Cardinal live dashboard — watch the system operate in real time.

FastAPI + SSE. Reads exclusively from cardinal.db (WAL allows concurrent
reads), so it is fully decoupled: kill it and nothing else notices; run it
and every Cardinal module's activity appears live.

Ghost Mode: hides all telemetry panels unless an anomaly is detected,
letting Cardinal run as a truly silent headless engine. Defaults to ON
when CARDINAL_VERBOSITY=0.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse
from starlette.requests import Request

from cardinal.core import db
from cardinal.core.config import GHOST, get_config

app = FastAPI(title="Cardinal Dashboard")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SNAPSHOT_INTERVAL_S = 2.0


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# ===========================================================================
# Snapshot assembly
# ===========================================================================

def build_snapshot() -> dict[str, Any]:
    from cardinal.modules.balancer import GINI_DANGER, gini_coefficient

    cfg = get_config()
    wealth = db.get_player_wealth_list()
    gini = gini_coefficient(wealth)

    gini_history = db.query(
        "SELECT gini_before, gini_after, timestamp FROM balance_log ORDER BY id DESC LIMIT 40")
    bugs = db.query("SELECT * FROM bugs ORDER BY id DESC LIMIT 25")
    balance_changes = db.query("SELECT * FROM balance_log ORDER BY id DESC LIMIT 20")
    quests = db.query(
        "SELECT id, title, source_name, source_url, status, created_at FROM quest_registry "
        "ORDER BY id DESC LIMIT 20")
    sentiment = db.query("SELECT * FROM sentiment_log ORDER BY id DESC LIMIT 20")
    bots = db.query("SELECT * FROM bot_status ORDER BY bot_name")
    incarnates = db.query("SELECT * FROM incarnate_log ORDER BY id DESC LIMIT 10")
    ghosts = db.query("SELECT * FROM spatial_cache ORDER BY id DESC LIMIT 30")
    gate = db.query(
        "SELECT id, event_type, module, gate_decision, gate_reasons, outcome, timestamp "
        "FROM replay_log ORDER BY id DESC LIMIT 25")
    agent = db.query("SELECT * FROM agent_log ORDER BY id DESC LIMIT 25")
    admin = db.query("SELECT * FROM admin_override_log ORDER BY id DESC LIMIT 15")
    versions = db.query(
        "SELECT id, triggered_by, mutation_type, mutation_summary, created_at "
        "FROM versions ORDER BY id DESC LIMIT 15")
    spend = db.query_one("SELECT * FROM api_spend ORDER BY day DESC LIMIT 1")
    mhcp_rows = db.query("SELECT * FROM mhcp_log ORDER BY id DESC LIMIT 10")

    world = db.get_world_state()
    axiom_emergency = world.get("emergency_axiom_breach") or {}
    axiom_active = bool(axiom_emergency.get("active"))

    fluctlight = db.query(
        "SELECT player_name, enemy_type, pain_memory, kills_by_player, losses_to_player, "
        "last_entity_id, dominant_weapon FROM player_grudges "
        "ORDER BY pain_memory DESC LIMIT 15")
    survivors_raw = db.query(
        "SELECT entity_id, enemy_type, epithet, survival_count, experience_count, "
        "biography_json, status FROM entity_registry "
        "WHERE status='alive' ORDER BY survival_count DESC, experience_count DESC LIMIT 10")
    survivors = []
    for row in survivors_raw:
        bio = json.loads(row.get("biography_json") or "[]")
        excerpt = ""
        if bio:
            last = bio[-1]
            excerpt = f"{last.get('player','?')} {last.get('outcome','?')} f{last.get('floor',1)}"
        survivors.append({**row, "biography_excerpt": excerpt})

    unpatched = sum(1 for b in bugs if b["status"] not in ("patched",))
    recent_rejections = sum(1 for g in gate[:10] if g["gate_decision"] == "rejected")
    critical_mhcp = any(m["critical"] for m in mhcp_rows[:5])
    locked = bool(spend and spend["locked_out"])
    anomaly = bool(
        unpatched or gini > GINI_DANGER or locked or recent_rejections
        or critical_mhcp or axiom_active)

    return {
        "gini": round(gini, 4),
        "gini_danger": GINI_DANGER,
        "gini_history": list(reversed(gini_history)),
        "players": len(wealth),
        "bugs": bugs,
        "balance_changes": [
            {**b, "changed_fields": json.loads(b.get("changed_fields_json") or "{}")}
            for b in balance_changes],
        "quests": quests,
        "sentiment": sentiment,
        "bots": bots,
        "incarnates": incarnates,
        "ghosts": ghosts,
        "gate": gate,
        "agent": agent,
        "admin": admin,
        "versions": versions,
        "mhcp": mhcp_rows,
        "spend": spend,
        "max_spend": cfg.max_daily_spend_usd,
        "anomaly": anomaly,
        "ghost_default": cfg.verbosity_mask <= GHOST,
        "axiom_emergency": axiom_emergency,
        "axiom_active": axiom_active,
        "fluctlight": fluctlight,
        "survivors": survivors,
    }


# ===========================================================================
# Routes
# ===========================================================================

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/snapshot")
def snapshot() -> dict[str, Any]:
    return build_snapshot()


@app.get("/api/replay/{replay_id}")
def replay_detail(replay_id: int) -> dict[str, Any]:
    from cardinal.modules.replay import diff_states, get_replay

    row = get_replay(replay_id)
    if row is None:
        raise HTTPException(status_code=404, detail="replay not found")
    return {
        "id": row["id"],
        "event_type": row["event_type"],
        "module": row["module"],
        "gate_decision": row["gate_decision"],
        "gate_reasons": row["gate_reasons"],
        "outcome": row["outcome"],
        "timestamp": row["timestamp"],
        "llm_input": row["llm_input"],
        "llm_output": row["llm_output"],
        "state_delta": diff_states(row["state_before_json"], row["state_after_json"]),
    }


@app.get("/events")
async def events(request: Request):
    async def stream():
        while True:
            if await request.is_disconnected():
                break
            snap = await asyncio.to_thread(build_snapshot)
            yield {"event": "snapshot", "data": json.dumps(snap, default=str)}
            await asyncio.sleep(SNAPSHOT_INTERVAL_S)

    return EventSourceResponse(stream())
