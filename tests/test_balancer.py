"""Order Control tests."""
from __future__ import annotations

import json

import pytest

from cardinal.core import db
from cardinal.modules.balancer import gini_coefficient, run_balance_cycle


def test_gini_zero():
    assert gini_coefficient([100, 100, 100]) == pytest.approx(0.0, abs=1e-9)


def test_gini_max():
    # For n=4 with all wealth held by one player the sample maximum is
    # exactly (n-1)/n = 0.75 — NOT "close to 1.0" (that's only true as n grows).
    assert gini_coefficient([0, 0, 0, 1000]) == pytest.approx(0.75, abs=1e-9)
    # Asymptotic check: n=1000 -> (n-1)/n = 0.999
    big = [0] * 999 + [1_000_000]
    assert gini_coefficient(big) == pytest.approx(0.999, abs=1e-6)


def test_gini_empty_and_zero_wealth():
    assert gini_coefficient([]) == 0.0
    assert gini_coefficient([0, 0, 0]) == 0.0


def _seed_combat(weapon: str, wins: int, losses: int) -> None:
    for _ in range(wins):
        db.log_combat({"player_name": "P", "enemy": "Goblin", "outcome": "win",
                       "weapon_used": weapon, "damage_dealt": 30})
    for _ in range(losses):
        db.log_combat({"player_name": "P", "enemy": "Goblin", "outcome": "loss",
                       "weapon_used": weapon, "damage_dealt": 5})


@pytest.mark.agent
def test_patch_reduces_damage(isolated_data_dir, fake_test_pass):
    """An overpowered (but non-anomalous) weapon gets nerfed within the 8%
    Taboo limit, through the Sub-Process gate, using the MockProvider."""
    _seed_combat("Iron Sword", wins=9, losses=1)   # 0.90 win rate -> flagged
    _seed_combat("Forest Bow", wins=5, losses=5)   # balanced control
    old_damage = _damage(isolated_data_dir, "Iron Sword")

    result = run_balance_cycle(allow_questgen=False)

    new_damage = _damage(isolated_data_dir, "Iron Sword")
    assert new_damage < old_damage
    assert new_damage >= old_damage * 0.92 - 1e-6, "nerf exceeded the 8% Taboo law"
    assert any("rebalanced" in a for a in result["actions"])


@pytest.mark.agent
def test_anomaly_quarantine(isolated_data_dir, fake_test_pass):
    """A >=95% win-rate exploit-class item is disabled immediately rather
    than waiting for 8%-per-cycle nerfs to catch up."""
    _seed_combat("Cursed Blade", wins=39, losses=1)  # 0.975 -> anomaly
    run_balance_cycle(allow_questgen=False)
    items = json.loads((isolated_data_dir / "items.json").read_text())
    blade = next(i for i in items if i["name"] == "Cursed Blade")
    assert blade["enabled"] is False


@pytest.mark.agent
def test_gate_rejects_protected_field_change(isolated_data_dir):
    """The Sub-Process refuses mutations touching Taboo-protected fields."""
    from cardinal import sub_process

    items = json.loads((isolated_data_dir / "items.json").read_text())
    items[0]["rarity"] = "mythic"  # protected field
    result = sub_process.approve_mutation("items_json", "test", {"items": items})
    assert not result.approved
    assert any("protected" in r for r in result.reasons)


@pytest.mark.agent
def test_versioning_and_rollback(isolated_data_dir, fake_test_pass):
    """Every approved mutation bumps the version; rollback restores state."""
    from cardinal import sub_process

    original = json.loads((isolated_data_dir / "items.json").read_text())
    old_damage = _damage(isolated_data_dir, "Iron Sword")

    items = json.loads(json.dumps(original))
    sword = next(i for i in items if i["name"] == "Iron Sword")
    sword["damage"] = int(round(old_damage * 0.95))  # lawful 5% change
    r1 = sub_process.approve_mutation("items_json", "test", {"items": items})
    assert r1.approved and r1.version_id is not None
    assert _damage(isolated_data_dir, "Iron Sword") < old_damage

    # capture pre-mutation state version? v1 snapshot is taken AFTER its
    # mutation; restoring v1 returns the nerfed state, so mutate again first.
    items2 = json.loads((isolated_data_dir / "items.json").read_text())
    sword2 = next(i for i in items2 if i["name"] == "Iron Sword")
    sword2["damage"] = int(round(sword2["damage"] * 0.95))
    r2 = sub_process.approve_mutation("items_json", "test", {"items": items2})
    assert r2.approved and r2.version_id > r1.version_id

    r3 = sub_process.approve_mutation("rollback", "test", {"version_id": r1.version_id})
    assert r3.approved
    assert _damage(isolated_data_dir, "Iron Sword") == sword["damage"]
    # rollback appended a NEW version — history is never rewritten
    assert r3.version_id > r2.version_id


def _damage(data_dir, name: str) -> float:
    items = json.loads((data_dir / "items.json").read_text())
    return next(i for i in items if i["name"] == name)["damage"]
