"""API spend guard tests — the non-negotiable cost safety layer."""
from __future__ import annotations

import pytest

from cardinal.llm import spend_guard


def test_cost_computation(cardinal_env):
    cost = spend_guard.compute_cost_usd(1_000_000, 0, 0)
    assert cost == pytest.approx(cardinal_env.price_input_per_mtok)
    cost = spend_guard.compute_cost_usd(0, 1_000_000, 0)
    assert cost == pytest.approx(cardinal_env.price_output_per_mtok)


def test_l3_allowed_when_fresh(cardinal_env, monkeypatch):
    monkeypatch.setattr(cardinal_env, "max_daily_spend_usd", 5.0)
    allowed, _ = spend_guard.l3_allowed()
    assert allowed


def test_80_percent_switches_to_l2_only(cardinal_env, monkeypatch):
    monkeypatch.setattr(cardinal_env, "max_daily_spend_usd", 1.0)
    spend_guard.record_spend(0.85)
    allowed, reason = spend_guard.l3_allowed()
    assert not allowed
    assert "80%" in reason
    assert spend_guard.today_spend()["warned_80"] == 1
    assert spend_guard.today_spend()["locked_out"] == 0


def test_100_percent_locks_out_l3(cardinal_env, monkeypatch):
    monkeypatch.setattr(cardinal_env, "max_daily_spend_usd", 1.0)
    spend_guard.record_spend(0.7)
    spend_guard.record_spend(0.4)  # crosses 1.0
    row = spend_guard.today_spend()
    assert row["locked_out"] == 1
    allowed, reason = spend_guard.l3_allowed()
    assert not allowed
    assert "locked out" in reason


def test_counter_keyed_by_day(cardinal_env):
    """Spend is keyed by date — a new day starts a fresh counter (midnight reset)."""
    spend_guard.record_spend(0.5)
    row = spend_guard.today_spend()
    assert row["day"] == spend_guard._today()
    from cardinal.core import db

    db.execute("UPDATE api_spend SET day='2000-01-01' WHERE day=?", (row["day"],))
    fresh = spend_guard.today_spend()
    assert fresh["spend_usd"] == 0.0 and fresh["locked_out"] == 0


def test_provider_fallback_is_single_attempt(cardinal_env, monkeypatch):
    """At most ONE L3 attempt per event: a failing Anthropic call falls back
    to L2 immediately, never retrying L3."""
    from cardinal.llm import provider as provider_mod

    attempts = {"n": 0}

    class ExplodingProvider(provider_mod.LLMProvider):
        name = "anthropic"

        def _complete(self, *a, **k):
            attempts["n"] += 1
            raise RuntimeError("api down")

    monkeypatch.setattr(provider_mod, "get_provider", lambda: ExplodingProvider())
    resp = provider_mod.complete_with_fallback(
        "test", "patch", "sys", "user",
        context={"code": "def f():\n    return 1/0\n", "error_type": "ZeroDivisionError",
                 "message": ""})
    assert attempts["n"] == 1, "L3 must not be retried"
    assert resp.provider == "local_rules"


def test_mock_provider_selected_and_deterministic(cardinal_env):
    from cardinal.llm.provider import get_provider

    provider = get_provider()
    assert provider.name == "mock"
    ctx = {"topic_title": "Ragnarok", "topic_text": "norse end battle", "archetype": "dungeon"}
    a = provider.complete("t", "gdd", "s", "u", context=ctx).text
    b = provider.complete("t", "gdd", "s", "u", context=ctx).text
    assert a == b, "MockProvider must be deterministic"
    import json

    gdd = json.loads(a)
    for key in ("title", "narrative", "stages", "npcs", "enemies", "rewards", "map_archetype"):
        assert key in gdd, f"mock GDD missing schema key {key}"
