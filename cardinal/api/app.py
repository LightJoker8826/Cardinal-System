"""Cardinal REST API — the engine-agnostic HTTP front door.

Any game that can make an HTTP request can connect to Cardinal: no Python
bindings required. Internally this is just a second adapter onto the same
seams the Python sandbox uses (event schema + control channel + DB) — the
Cardinal modules cannot tell the difference.

Auth: every endpoint requires `Authorization: Bearer <CARDINAL_API_TOKEN>`.
Requests without a valid token get 401. A missing CARDINAL_API_TOKEN does
NOT silently open the API — the server refuses to serve until one is
configured (fail-silent here would be a security hole, not graceful
degradation).
"""
from __future__ import annotations

import secrets
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from cardinal.core import db
from cardinal.core.config import SEV_INFO, get_config, log_event
from cardinal.core.events import parse_stream, parse_traceback

app = FastAPI(title="Cardinal System API", version="0.1.0",
              description="Universal game-management middleware — HTTP adapter")

_bearer = HTTPBearer(auto_error=False)


def require_token(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    cfg = get_config()
    if not cfg.api_token:
        raise HTTPException(status_code=503,
                            detail="CARDINAL_API_TOKEN is not configured — API refuses to serve")
    if credentials is None or not secrets.compare_digest(credentials.credentials, cfg.api_token):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class BugReport(BaseModel):
    log_payload: str = Field(..., description="raw log lines in the Cardinal event schema, "
                                              "including the [CARDINAL_ERROR] entry + traceback")


class PlayerEvent(BaseModel):
    kind: str = Field(..., description="'combat' or 'biometric'")
    player_name: str
    # combat fields
    enemy: str | None = None
    outcome: str | None = None
    damage_dealt: float = 0
    gold_earned: int = 0
    weapon_used: str | None = None
    floor: int = 1
    duration_ticks: int = 1
    # biometric fields
    heart_rate: float | None = None
    x: int = 0
    y: int = 0


class QuestRequest(BaseModel):
    topic: str | None = None
    source: str = "wikipedia"
    random_topic: bool = False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/cardinal/report-bug", dependencies=[Depends(require_token)])
def report_bug(report: BugReport) -> dict[str, Any]:
    """External engines push crash logs here; Cardinal triggers the healer."""
    events = parse_stream(report.log_payload.splitlines())
    errors = [e for e in events if e.is_error]
    if not errors:
        raise HTTPException(status_code=422, detail="payload contains no [CARDINAL_ERROR] entry")
    results = []
    from cardinal.modules.self_healing import repair_from_event

    for event in errors:
        info = parse_traceback(event.full_text)
        patched = repair_from_event(event)
        results.append({"error_type": info.get("error_type"), "file": info.get("file"),
                        "line": info.get("line"), "patched": patched})
    log_event("api", f"report-bug: {len(errors)} error(s) processed", SEV_INFO)
    return {"processed": len(errors), "results": results}


@app.get("/cardinal/balance", dependencies=[Depends(require_token)])
def balance() -> dict[str, Any]:
    from cardinal.modules.balancer import build_report

    report = build_report()
    return {
        "gini": report["gini"],
        "players": report["players"],
        "win_rates": report["win_rates"],
        "flagged": report["flagged"],
        "anomalies": report["anomalies"],
        "soft_flags": report["soft_flags"],
        "churn": report["churn"],
    }


@app.post("/cardinal/generate-quest", dependencies=[Depends(require_token)])
def generate_quest_endpoint(req: QuestRequest) -> dict[str, Any]:
    from cardinal.modules.quest_generator import generate_quest

    if not req.topic and not req.random_topic:
        raise HTTPException(status_code=422, detail="provide topic or random_topic=true")
    return generate_quest(req.topic, source_name=req.source, random_topic=req.random_topic)


@app.get("/cardinal/world-state", dependencies=[Depends(require_token)])
def world_state() -> dict[str, Any]:
    return {"world_state": db.get_world_state()}


@app.post("/cardinal/player-event", dependencies=[Depends(require_token)])
def player_event(event: PlayerEvent) -> dict[str, Any]:
    """Combat or biometric telemetry from an external game."""
    if event.kind == "combat":
        if not event.enemy or not event.outcome:
            raise HTTPException(status_code=422, detail="combat events need enemy and outcome")
        row_id = db.log_combat({
            "player_name": event.player_name,
            "enemy": event.enemy,
            "outcome": event.outcome,
            "damage_dealt": event.damage_dealt,
            "gold_earned": event.gold_earned,
            "weapon_used": event.weapon_used,
            "floor": event.floor,
            "duration_ticks": event.duration_ticks,
        })
        return {"logged": "combat", "id": row_id}
    if event.kind == "biometric":
        if event.heart_rate is None:
            raise HTTPException(status_code=422, detail="biometric events need heart_rate")
        from cardinal.modules.biometrics import process_reading

        spiked = process_reading(event.player_name, event.heart_rate, event.x, event.y)
        return {"logged": "biometric", "spike": spiked}
    raise HTTPException(status_code=422, detail="kind must be 'combat' or 'biometric'")
