"""Error Control (self-healing) pipeline tests.

All marked `agent` so the healer's own post-patch verification run
(`pytest -m "not chaos and not agent"`) never recurses into them.
Targets are throwaway modules in tmp_path — never the live game files.
"""
from __future__ import annotations

import pytest

from cardinal.core import db
from cardinal.core.events import GameEvent, parse_traceback
from cardinal.modules.self_healing import find_enclosing_function, repair_from_event

BROKEN_MODULE = '''"""Throwaway module with a known bug."""


def divide_loot(total_gold, party_size):
    bonus = party_size - party_size
    share = total_gold / bonus
    return share
'''

FAKE_TRACEBACK = """ZeroDivisionError in divide_loot
Traceback (most recent call last):
  File "{file}", line 6, in divide_loot
    share = total_gold / bonus
ZeroDivisionError: division by zero
"""


def _make_event(target_file) -> GameEvent:
    return GameEvent(
        timestamp="2026-06-11 12:00:00",
        level="CARDINAL_ERROR",
        module="game.test",
        message=f"ZeroDivisionError in divide_loot",
        continuation=FAKE_TRACEBACK.format(file=target_file).splitlines(),
    )


@pytest.fixture
def broken_module(tmp_path):
    target = tmp_path / "loot_module.py"
    target.write_text(BROKEN_MODULE, encoding="utf-8")
    return target


@pytest.mark.agent
def test_traceback_parses(broken_module):
    info = parse_traceback(_make_event(broken_module).full_text)
    assert info["error_type"] == "ZeroDivisionError"
    assert info["line"] == 6
    assert info["file"] == str(broken_module)


@pytest.mark.agent
def test_find_enclosing_function(broken_module):
    name, source = find_enclosing_function(broken_module.read_text(), 6)
    assert name == "divide_loot"
    assert "total_gold / bonus" in source


@pytest.mark.agent
def test_backup_created(broken_module, cardinal_env, fake_test_pass):
    """Healing a file must write a timestamped backup before any change."""
    before = list(cardinal_env.backups_dir.glob("*.py"))
    assert repair_from_event(_make_event(broken_module))
    after = list(cardinal_env.backups_dir.glob("*.py"))
    assert len(after) == len(before) + 1
    assert after[0].read_text(encoding="utf-8").find("total_gold / bonus") != -1 or \
        BROKEN_MODULE in after[0].read_text(encoding="utf-8")


@pytest.mark.agent
def test_patch_applies(broken_module, cardinal_env, fake_test_pass):
    """End-to-end: inject a known bug, run the healer (MockProvider),
    confirm the patched function no longer raises."""
    assert repair_from_event(_make_event(broken_module))

    namespace: dict = {}
    exec(broken_module.read_text(encoding="utf-8"), namespace)
    # the L2/Mock patch guards ZeroDivisionError with a safe default
    assert namespace["divide_loot"](100, 4) == 0

    bugs = db.query("SELECT * FROM bugs ORDER BY id DESC LIMIT 1")
    assert bugs[0]["status"] == "patched"
    replays = db.query("SELECT * FROM replay_log WHERE event_type='code_patch'")
    assert replays and replays[-1]["gate_decision"] == "approved"
    versions = db.query("SELECT * FROM versions")
    assert versions, "approved patch must bump the Cardinal version"


@pytest.mark.agent
def test_rollback_on_fail(broken_module, cardinal_env, fake_test_fail):
    """If post-patch tests fail, the original file is restored from backup
    and the bug is marked patch_failed."""
    original = broken_module.read_text(encoding="utf-8")
    assert not repair_from_event(_make_event(broken_module))
    assert broken_module.read_text(encoding="utf-8") == original

    bugs = db.query("SELECT * FROM bugs ORDER BY id DESC LIMIT 1")
    assert bugs[0]["status"] == "patch_failed"
    replays = db.query("SELECT * FROM replay_log WHERE event_type='code_patch'")
    assert replays[-1]["gate_decision"] == "rejected"


@pytest.mark.agent
def test_gate_rejects_forbidden_code(broken_module, cardinal_env):
    """The Taboo Index blocks patches containing forbidden patterns."""
    from cardinal import sub_process

    evil = 'def divide_loot(total_gold, party_size):\n    import os\n    os.system("rm -rf /")\n    return 0\n'
    result = sub_process.approve_mutation(
        "code_patch", "test",
        {"file": str(broken_module), "function": "divide_loot", "new_code": evil})
    assert not result.approved
    assert any("forbidden" in r for r in result.reasons)
    assert "os.system" not in broken_module.read_text(), "rejected patch must never touch disk"
