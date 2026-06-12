"""REST API layer tests — bearer auth + engine-agnostic endpoints."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cardinal.api.app import app
from cardinal.core import db

TOKEN = "test-token-cardinal"


@pytest.fixture
def client(cardinal_env, monkeypatch):
    monkeypatch.setattr(cardinal_env, "api_token", TOKEN)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_missing_token_rejected(client):
    assert client.get("/cardinal/balance").status_code in (401, 403)


def test_wrong_token_rejected(client):
    resp = client.get("/cardinal/balance", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_unconfigured_token_refuses_service(cardinal_env, monkeypatch):
    """No CARDINAL_API_TOKEN -> the API refuses to serve (503), it does NOT
    fail open."""
    monkeypatch.setattr(cardinal_env, "api_token", None)
    with TestClient(app) as c:
        resp = c.get("/cardinal/balance", headers={"Authorization": "Bearer anything"})
        assert resp.status_code == 503


def test_balance_endpoint(client, auth):
    resp = client.get("/cardinal/balance", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    for key in ("gini", "win_rates", "flagged", "anomalies", "churn"):
        assert key in body


def test_player_event_combat(client, auth):
    resp = client.post("/cardinal/player-event", headers=auth, json={
        "kind": "combat", "player_name": "UnrealHero", "enemy": "Dragon",
        "outcome": "win", "damage_dealt": 55, "gold_earned": 120,
        "weapon_used": "Iron Sword", "floor": 3, "duration_ticks": 9})
    assert resp.status_code == 200
    rows = db.query("SELECT * FROM combat_log WHERE player_name='UnrealHero'")
    assert rows and rows[0]["enemy"] == "Dragon"


def test_player_event_biometric_spike(client, auth):
    """An external engine can drive Incarnate Mode over plain HTTP."""
    for _ in range(5):
        client.post("/cardinal/player-event", headers=auth, json={
            "kind": "biometric", "player_name": "UnrealHero",
            "heart_rate": 70, "x": 4, "y": 4})
    resp = client.post("/cardinal/player-event", headers=auth, json={
        "kind": "biometric", "player_name": "UnrealHero",
        "heart_rate": 160, "x": 4, "y": 4})
    assert resp.status_code == 200
    assert resp.json()["spike"] is True
    pending = db.pending_control(0, "game")
    assert any(p["command"] == "incarnate" for p in pending)


def test_world_state_endpoint(client, auth):
    db.set_world_state("weather", {"type": "storm"})
    resp = client.get("/cardinal/world-state", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["world_state"]["weather"]["type"] == "storm"


@pytest.mark.agent
def test_report_bug_endpoint(client, auth, tmp_path, cardinal_env, fake_test_pass):
    target = tmp_path / "external_module.py"
    target.write_text(
        "def hp_calc(base, modifier):\n"
        "    factor = modifier - modifier\n"
        "    return base / factor\n", encoding="utf-8")
    payload = (
        "[2026-06-11 12:00:00] [CARDINAL_ERROR] [engine.combat] ZeroDivisionError in hp_calc\n"
        "Traceback (most recent call last):\n"
        f'  File "{target}", line 3, in hp_calc\n'
        "    return base / factor\n"
        "ZeroDivisionError: division by zero\n")
    resp = client.post("/cardinal/report-bug", headers=auth, json={"log_payload": payload})
    assert resp.status_code == 200
    body = resp.json()
    assert body["processed"] == 1
    assert body["results"][0]["patched"] is True
