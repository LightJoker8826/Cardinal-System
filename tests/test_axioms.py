"""Axiomatic emergency — Taboo breaches trigger rollback and world-lock."""
from __future__ import annotations

import json

import pytest

from cardinal import sub_process
from cardinal.core import db
from cardinal.core.config import get_config
from cardinal.modules.taboo_index import admin_gold_lawful, is_axiom_violation


def test_is_axiom_violation_detects_taboo_reasons():
    assert is_axiom_violation("protected field 'name' changed — Taboo violation")
    assert is_axiom_violation("patch contains forbidden pattern: 'eval('")
    assert not is_axiom_violation("item set mismatch — additions/removals are not balance mutations")


def test_taboo_code_patch_triggers_emergency(cardinal_env, tmp_path):
    """Forbidden code pattern is a logic failure → axiom emergency."""
    cfg = get_config()
    items_path = cfg.data_dir / "items.json"
    items = json.loads(items_path.read_text(encoding="utf-8"))
    snap = sub_process.build_state_snapshot()
    db.execute(
        """INSERT INTO versions
           (triggered_by, mutation_type, mutation_summary, items_json,
            world_state_json, sec_state_json, quests_json, created_at)
           VALUES (?,?,?,?,?,?,?,datetime('now'))""",
        ("test", "items_json", "seed",
         items_path.read_text(encoding="utf-8"),
         json.dumps(snap["world_state"]),
         json.dumps(list(db.query("SELECT * FROM sec_state"))),
         json.dumps(json.loads((cfg.data_dir / "quests.json").read_text(encoding="utf-8")))),
    )

    result = sub_process.approve_mutation(
        "code_patch",
        "test",
        {
            "file": str(cfg.project_root / "game.py"),
            "function": "calculate_damage",
            "new_code": "def calculate_damage():\n    eval('1')\n    return 1\n",
            "bug_id": 0,
        },
    )
    assert not result.approved
    world = db.get_world_state()
    assert world.get("emergency_axiom_breach", {}).get("active") is True
    violations = db.query("SELECT * FROM axiom_violation_log ORDER BY id DESC LIMIT 1")
    assert violations


def test_clamp_only_balance_does_not_trigger_emergency(cardinal_env):
    """Normal clamped rebalance approves without axiom emergency."""
    cfg = get_config()
    items = json.loads((cfg.data_dir / "items.json").read_text(encoding="utf-8"))
    proposed = [dict(i) for i in items]
    for item in proposed:
        if item["name"] == "Iron Sword" and item.get("damage"):
            item["damage"] = int(item["damage"] * 1.07)

    db.set_world_state("emergency_axiom_breach", {"active": False}, changed_by="test_reset")
    result = sub_process.approve_mutation("items_json", "balancer", {"items": proposed})
    assert result.approved
    world = db.get_world_state()
    assert not world.get("emergency_axiom_breach", {}).get("active")


def test_admin_unlawful_gold_blocked():
    max_gold = 10_000_000
    assert admin_gold_lawful(max_gold)
    assert not admin_gold_lawful(max_gold + 1)
