"""Taboo Index (law layer) tests."""
from __future__ import annotations

from cardinal.modules.taboo_index import get_taboo_index


def test_item_law_bounds():
    taboo = get_taboo_index(refresh=True)
    lawful = {"name": "Sword", "damage": 50, "crit_chance": 0.2, "crit_mult": 2.0, "value": 100}
    assert taboo.validate_item(lawful) == []
    unlawful = {"name": "GodKiller", "damage": 99999, "crit_chance": 0.99, "crit_mult": 50.0, "value": 100}
    violations = taboo.validate_item(unlawful)
    assert len(violations) >= 3


def test_enemy_law_bounds():
    taboo = get_taboo_index()
    assert taboo.validate_enemy({"name": "Goblin", "damage": 10, "hp": 50, "reward_gold": 20}) == []
    violations = taboo.validate_enemy(
        {"name": "Cheater", "damage": 9999, "hp": 999999, "reward_gold": 1, "attack_window_s": 0.01})
    assert any("attack_window" in v for v in violations)
    assert any("damage" in v for v in violations)


def test_forbidden_code_patterns():
    taboo = get_taboo_index()
    assert taboo.validate_code("def f():\n    return 1\n") == []
    assert taboo.validate_code("import os\nos.system('x')") != []
    assert taboo.validate_code("eval('1+1')") != []


def test_player_clamps():
    taboo = get_taboo_index()
    hp, gold, level = taboo.clamp_player(999999, 999999999, 9999)
    assert hp == taboo.laws["player_laws"]["max_hp"]
    assert gold == taboo.laws["player_laws"]["max_gold"]
    assert level == taboo.laws["player_laws"]["max_level"]


def test_immortal_objects():
    taboo = get_taboo_index()
    assert taboo.is_immortal_object("Teleport Gate")
    assert not taboo.is_immortal_object("Goblin")


def test_max_delta_is_8_percent():
    assert get_taboo_index().max_delta_pct == 0.08
