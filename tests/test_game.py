"""L1 game engine tests."""
from __future__ import annotations

import json
import random

import pytest

import game
from cardinal.core import db
from tests.conftest import ForcedRandom

REQUIRED_ITEM_FIELDS = {"name", "damage", "crit_chance", "crit_mult", "type", "value",
                        "rarity", "enabled"}


def test_combat_resolves():
    """One combat round completes without crashing (safe weapon)."""
    rng = random.Random(7)
    player = game.Player(name="TestHero", weapon=dict(game.get_item("Iron Sword")))
    player.x, player.y = 5, 5  # outside safe zones
    enemy = game.Enemy(name="Goblin", hp=10, damage=2, reward_gold=5,
                       genome={"combo_seed": 1, "combo_shuffle_depth": 1,
                               "timing_jitter": 0, "damage_jitter": 0,
                               "resistance_vector": {}},
                       params={"defense_chance": 0.0, "combo": ["strike"],
                               "attack_damage_mult": 1.0})
    result = game.run_combat(player, enemy, rng, floor=1)
    assert result["outcome"] in ("win", "loss", "flee")
    assert result["ticks"] >= 1


@pytest.mark.chaos
def test_cursed_blade_can_crash():
    """BUG A trigger exists: Cursed Blade can raise ZeroDivisionError inside
    the damage calculation. (chaos marker: excluded from the healer's
    post-patch verification so a legitimate repair is never self-defeated.)"""
    cursed = game.get_item("Cursed Blade")
    assert cursed is not None
    # rng scripted: first random() = 0.01 < 0.05 -> bug path fires
    rng = ForcedRandom([0.01], uniform_value=1.0)
    try:
        game.calculate_damage(dict(cursed), player=None, rng=rng)
    except ZeroDivisionError:
        return  # bug trigger present, as shipped
    pytest.skip("BUG A no longer reproduces — Cardinal has healed calculate_damage "
                "(restore the original from backups/ to re-arm the sandbox)")


@pytest.mark.chaos
def test_loot_loop_watchdog_exists():
    """BUG B trigger exists: gold > 9999 can hit the 5s watchdog. We verify
    the unbounded-loop construct is present without waiting out 5 seconds."""
    import inspect

    src = inspect.getsource(game.loot_distribution)
    assert "while True" in src or "LootLoopWatchdogError" in src


def test_items_load():
    items = game.load_items(refresh=True)
    assert len(items) >= 12
    names = {i["name"] for i in items}
    for required in ("Iron Sword", "Silver Dagger", "Cursed Blade", "God Sword",
                     "Wooden Shield", "Health Potion", "Elixir", "Shadow Blade",
                     "Forest Bow", "Flame Staff", "Mithril Armor", "Cursed Ring"):
        assert required in names
    for item in items:
        missing = REQUIRED_ITEM_FIELDS - set(item.keys())
        assert not missing, f"{item['name']} missing {missing}"
    god = game.get_item("God Sword")
    assert god["enabled"] is False, "God Sword must start DISABLED"


def test_db_init(cardinal_env):
    rows = db.query("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r["name"] for r in rows}
    expected = {
        "players", "combat_log", "item_stats", "bugs", "balance_log",
        "quest_registry", "spatial_cache", "agent_log", "world_state",
        "mhcp_log", "control_channel", "sentiment_log", "replay_log",
        "api_spend", "sec_state", "enemy_genomes", "versions",
        "admin_override_log", "bot_status", "incarnate_log", "schema_migrations",
        "entity_registry", "encounter_memory", "player_grudges", "axiom_violation_log",
    }
    assert expected.issubset(tables), f"missing tables: {expected - tables}"


def test_migrations_idempotent(cardinal_env):
    """Running migrations repeatedly must be a no-op, never destructive."""
    import migrate

    db.log_combat({"player_name": "Keep", "enemy": "Goblin", "outcome": "win",
                   "weapon_used": "Iron Sword"})
    assert migrate.run_migrations(cardinal_env.db_path, quiet=True) == []
    rows = db.query("SELECT COUNT(*) AS c FROM combat_log")
    assert rows[0]["c"] == 1


def test_control_channel_sequencing(cardinal_env):
    """Temporal Drift Compensation: strictly ordered application; a gap
    triggers a state refresh and fast-forward."""
    from cardinal.adapters.base import GameAdapter

    applied: list[str] = []

    class FakeAdapter(GameAdapter):
        target = "game"

        def apply_control(self, command, payload):
            applied.append(command)

        def apply_state_snapshot(self, snapshot):
            applied.append("STATE_REFRESH")

    adapter = FakeAdapter()
    db.push_control("cmd_a", {})
    db.push_control("cmd_b", {})
    assert adapter.poll_control() == 2
    assert applied == ["cmd_a", "cmd_b"]

    # Simulate drift: adapter cursor regresses (stale duplicate consumption)
    db.push_control("cmd_c", {})
    adapter.last_applied_seq -= 2  # now out of sync: next pending is not cur+1
    adapter.poll_control()
    assert "STATE_REFRESH" in applied
    assert adapter.last_applied_seq == db.latest_sequence("game")


def test_safe_zone_law():
    from cardinal.modules.taboo_index import get_taboo_index

    taboo = get_taboo_index(refresh=True)
    assert taboo.in_safe_zone(1, 1) == "Town of Beginnings"
    assert taboo.in_safe_zone(5, 5) is None


def test_bot_live_status_preserves_game_counters(cardinal_env):
    db.execute(
        """INSERT INTO bot_status
           (bot_name, state, hp, gold, floor, weapon, games_played, games_won, updated_at)
           VALUES ('Bot_Alpha','idle',0,12,1,'Iron Sword',5,1,'2020-01-01T00:00:00+00:00')""")
    db.update_bot_live_status(
        "Bot_Alpha", "running", hp=77, gold=40, floor=3, weapon="Cursed Blade")
    row = db.query_one("SELECT * FROM bot_status WHERE bot_name='Bot_Alpha'")
    assert row["state"] == "running"
    assert row["hp"] == 77
    assert row["floor"] == 3
    assert row["weapon"] == "Cursed Blade"
    assert row["games_played"] == 5
    assert row["games_won"] == 1


def test_ghost_echo_roundtrip(cardinal_env):
    db.dump_spatial_state({"player_name": "Griselda", "x": 4, "y": 4,
                           "inventory": ["Wedding Ring"], "biometric_value": 150})
    echoes = game.render_ghosts(5, 5, radius=2)
    assert any(e["player"] == "Griselda" for e in echoes)
    assert echoes[0]["inventory_snapshot"] == ["Wedding Ring"]
