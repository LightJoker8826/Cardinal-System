"""Fluctlight memory — pain_memory, biography, survivor identity."""
from __future__ import annotations

import json
import random

import game
from cardinal.core import db
from cardinal.modules import memory, sec


def test_pain_memory_rises_with_slaughter(cardinal_env):
    eid = memory.register_entity("Goblin", sec.make_genome("Goblin", rng=random.Random(1)))
    rng = random.Random(2)
    for _ in range(3):
        memory.record_experience(eid, "Goblin", "Bot_Alpha", "win", "Iron Sword", 1, {}, rng)
        eid = memory.register_entity("Goblin", sec.make_genome("Goblin", rng=rng))
    row = memory.compute_pain_memory("Bot_Alpha", "Goblin")
    assert row["kills_by_player"] == 3
    assert row["pain_memory"] > 0.3


def test_biography_appended_every_experience(cardinal_env):
    eid = memory.register_entity("Orc", sec.make_genome("Orc"))
    memory.record_experience(eid, "Orc", "Bot_Beta", "loss", "Iron Sword", 2, {}, random.Random(3))
    ent = memory.get_entity(eid)
    assert ent is not None
    assert len(ent["biography"]) == 1
    assert ent["biography"][0]["player"] == "Bot_Beta"


def test_survivor_reencounter_same_entity_id(cardinal_env):
    genome = sec.make_genome("Goblin", rng=random.Random(10))
    eid = memory.register_entity("Goblin", genome)
    db.execute(
        "UPDATE entity_registry SET status='alive', survival_count=3, epithet='The Unbroken' WHERE entity_id=?",
        (eid,),
    )
    db.execute(
        """INSERT INTO player_grudges
           (player_name, enemy_type, kills_by_player, losses_to_player, flees,
            pain_memory, dominant_weapon, last_floor, last_entity_id, updated_at)
           VALUES ('Bot_Alpha','Goblin',0,2,0,0.5,'Iron Sword',1,?,datetime('now'))""",
        (eid,),
    )
    rng = random.Random(11)
    rng.random = lambda: 0.01  # force re-encounter roll
    ident = memory.resolve_spawn_identity("Goblin", "Bot_Alpha", rng)
    assert ident["entity_id"] == eid
    assert ident["reencounter"] is True


def test_pain_memory_modifies_sec_params(cardinal_env):
    db.execute(
        """INSERT INTO player_grudges
           (player_name, enemy_type, kills_by_player, losses_to_player, flees,
            pain_memory, dominant_weapon, last_floor, last_entity_id, updated_at)
           VALUES ('Bot_Alpha','Goblin',5,0,0,0.7,'Iron Sword',3,NULL,datetime('now'))""",
    )
    base = sec.effective_params("Goblin", rng=random.Random(4))
    biased = sec.effective_params("Goblin", rng=random.Random(4), player_name="Bot_Alpha")
    assert biased["attack_damage_mult"] > base["attack_damage_mult"]
    assert "remembers@" in biased["policy_mix"]


def test_epithet_at_three_survivals(cardinal_env):
    eid = memory.register_entity("Orc", sec.make_genome("Orc"))
    rng = random.Random(5)
    for _ in range(3):
        memory.record_experience(eid, "Orc", "Bot_Beta", "loss", "Iron Sword", 1, {}, rng)
    ent = memory.get_entity(eid)
    assert ent["epithet"] is not None
    assert int(ent["survival_count"]) >= 3


def test_spawn_assigns_entity_id(cardinal_env):
    enemy = game.spawn_enemy(1, random.Random(6), "Bot_Alpha")
    assert enemy.entity_id
    assert len(enemy.entity_id) > 3


def test_full_run_writes_encounter_memory(cardinal_env):
    game.run_game("MemTest", "Iron Sword", max_floor=2, seed=42)
    rows = db.query("SELECT COUNT(*) AS c FROM encounter_memory")[0]["c"]
    assert rows >= 1
