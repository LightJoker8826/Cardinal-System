"""SEC (Skilled Experience Catalogue) tests."""
from __future__ import annotations

import random

import pytest

from cardinal.core import db
from cardinal.modules import sec
from cardinal.modules.taboo_index import get_taboo_index


def test_blend_interpolates_adjacent_policies():
    policies = sec.load_policies(refresh=True)["policies"]
    rng = random.Random(3)
    params = sec.effective_params("Orc", rng=rng)  # standard/aggressive blend
    low, high = policies["standard"], policies["aggressive"]
    lo = min(low["attack_damage_mult"], high["attack_damage_mult"]) * 0.6
    hi = max(low["attack_damage_mult"], high["attack_damage_mult"]) * 1.4
    assert lo <= params["attack_damage_mult"] <= hi
    assert params["combo"], "blended policy must produce a combo"


def test_genomes_are_unique():
    """No two enemies share identical attack timing / combo patterns."""
    rng = random.Random(99)
    g1 = sec.make_genome("Goblin", rng=rng)
    g2 = sec.make_genome("Goblin", rng=rng)
    assert g1 != g2
    assert g1["combo_seed"] != g2["combo_seed"]


def test_mutation_respects_floor():
    enemy = {"name": "Test Wraith", "damage": 20, "hp": 100, "reward_gold": 50}
    rng = random.Random(5)
    mutated = sec.mutate_enemy_stats(enemy, entropy=0.9, rng=rng)
    assert mutated["damage"] >= 1 and mutated["hp"] >= 1
    assert "genome" in mutated
    assert mutated["genome"]["entropy_at_birth"] == 0.9


def test_attack_window_taboo_floor():
    """Genome jitter + adaptive compression can never push the telegraph
    window below the Taboo minimum."""
    floor = get_taboo_index(refresh=True).enemy_attack_window_min_s
    rng = random.Random(11)
    for _ in range(50):
        params = sec.effective_params("Shadow Boss", rng=rng)
        assert params["attack_window_s"] >= floor - 1e-9


def test_entropy_grows_with_history(cardinal_env):
    e0 = sec.compute_entropy()
    for _ in range(200):
        db.log_combat({"player_name": "P", "enemy": "Goblin", "outcome": "win",
                       "weapon_used": "Iron Sword"})
    assert sec.compute_entropy() > e0


@pytest.mark.agent
def test_evolution_shifts_blend_toward_harder(cardinal_env):
    """Players steamrolling an enemy pushes its blend ratio up (or steps the
    spectrum), via the Sub-Process gate."""
    for i in range(40):
        db.log_combat({"player_name": "P", "enemy": "Orc", "outcome": "win",
                       "weapon_used": "Iron Sword", "duration_ticks": 3})
    before = sec.get_blend("Orc")
    payload = sec.evolve("Orc")
    assert payload is not None
    after = sec.get_blend("Orc")
    spectrum = sec.load_policies()["spectrum"]
    harder = (
        after["blend_ratio"] > before["blend_ratio"]
        or spectrum.index(after["policy_high"]) > spectrum.index(before["policy_high"])
        or spectrum.index(after["policy_low"]) > spectrum.index(before["policy_low"])
    )
    assert harder
    assert 0.0 <= after["blend_ratio"] <= 1.0


@pytest.mark.agent
def test_adaptive_tier_counter_weapons(cardinal_env):
    """The adaptive tier weights resistances against the winning weapons."""
    for _ in range(30):
        db.log_combat({"player_name": "P", "enemy": "Shadow Boss", "outcome": "win",
                       "weapon_used": "Shadow Blade", "duration_ticks": 4})
    sec.evolve("Shadow Boss")
    blend = sec.get_blend("Shadow Boss")
    if blend["policy_high"] == "adaptive":
        assert "Shadow Blade" in blend["adaptive"].get("counter_weapons", {})
        floor = get_taboo_index().enemy_attack_window_min_s
        assert blend["adaptive"].get("attack_window_s", floor) >= floor
