"""Quest quality filter + source filter tests."""
from __future__ import annotations

import pytest

from cardinal.modules.quality_filter import score_gdd
from cardinal.sources.base import MIN_WORDS, SourceQualityError, TopicData, TopicSource

TOPIC_TEXT = (
    "Ragnarok is a series of events in Norse mythology including a great battle "
    "foretold to lead to the death of gods such as Odin, Thor, and Loki, natural "
    "disasters, and the submersion of the world in water. " * 10
)

GOOD_GDD = {
    "title": "The Trial of Ragnarok",
    "narrative": ("Whispers of Ragnarok have reached the floating castle. The end-battle "
                  "of the gods reshapes the land into a dungeon trial. Only those who "
                  "unravel its origin may claim the reward."),
    "stages": [
        {"stage": 1, "description": "Investigate the Ragnarok rumors near the great battle site.",
         "objective": "Speak with the Chronicler."},
        {"stage": 2, "description": "The guardians of the gods emerge.",
         "objective": "Defeat 3 Anomaly Guardians."},
        {"stage": 3, "description": "Odin's avatar reveals itself in the deep crypt.",
         "objective": "Defeat the Avatar and seal the anomaly."},
    ],
    "npcs": [{"name": "The Chronicler", "role": "quest_giver",
              "dialogue": "Traveler... the tale of Ragnarok is older than this castle. Listen well."}],
    "enemies": [{"name": "Anomaly Guardian", "damage": 18, "hp": 120, "reward_gold": 60}],
    "rewards": [{"item": "Relic of Ragnarok", "quantity": 1}],
    "map_archetype": "dungeon",
}


def test_good_gdd_passes():
    report = score_gdd(GOOD_GDD, TOPIC_TEXT, "dungeon")
    assert report.passed, report.summary


def test_archetype_mismatch_fails():
    report = score_gdd(GOOD_GDD, TOPIC_TEXT, "vertical")
    assert not report.passed
    assert any("archetype" in f for f in report.failures)


def test_placeholder_dialogue_fails():
    bad = dict(GOOD_GDD)
    bad["npcs"] = [{"name": "X", "role": "quest_giver", "dialogue": "TODO write dialogue here"}]
    report = score_gdd(bad, TOPIC_TEXT, "dungeon")
    assert report.scores["dialogue_quality"] < 0.6


def test_unlawful_enemy_stats_fail():
    bad = dict(GOOD_GDD)
    bad["enemies"] = [{"name": "Godslayer", "damage": 99999, "hp": 999999, "reward_gold": 999999}]
    report = score_gdd(bad, TOPIC_TEXT, "dungeon")
    assert report.scores["stat_validity"] < 0.6


def test_source_filters_reject_short_stub_disambiguation():
    class FakeSource(TopicSource):
        name = "fake"

        def fetch(self, keyword):
            raise NotImplementedError

    short = TopicData(title="Short", summary_text="x", full_text="too short",
                      source_name="fake", source_url="")
    with pytest.raises(SourceQualityError):
        FakeSource.enforce_quality(short)

    long_text = "word " * (MIN_WORDS + 10)
    stub = TopicData(title="Stubby", summary_text="x", full_text=long_text,
                     source_name="fake", source_url="", quality_flags=["stub"])
    with pytest.raises(SourceQualityError):
        FakeSource.enforce_quality(stub)

    disambig = TopicData(title="Mercury", summary_text="x", full_text=long_text,
                         source_name="fake", source_url="",
                         quality_flags=["disambiguation"])
    with pytest.raises(SourceQualityError):
        FakeSource.enforce_quality(disambig)

    clean = TopicData(title="Clean", summary_text="x", full_text=long_text,
                      source_name="fake", source_url="")
    assert FakeSource.enforce_quality(clean) is clean


@pytest.mark.agent
def test_quest_pipeline_end_to_end(isolated_data_dir, cardinal_env, monkeypatch):
    """Full Social Control pipeline on the MockProvider with a stubbed source:
    quality filter -> SEC mutation -> asset registration -> gate install."""
    from cardinal.modules import quest_generator as qg
    from cardinal.sources import base as source_base

    topic = TopicData(title="Ragnarok", summary_text=TOPIC_TEXT[:200],
                      full_text=TOPIC_TEXT, source_name="stub",
                      source_url="https://example.test/ragnarok")

    class StubSource(TopicSource):
        name = "stub"

        def fetch(self, keyword):
            return topic

    monkeypatch.setattr(source_base, "_REGISTRY",
                        {**source_base._REGISTRY, "stub": StubSource})
    # write quest assets into tmp, not the real project root
    monkeypatch.setattr(cardinal_env, "project_root", isolated_data_dir.parent)

    result = qg.generate_quest("Ragnarok", source_name="stub")
    assert result["ok"], result
    import json as _json

    pack = _json.loads((isolated_data_dir / "asset-pack.json").read_text())
    assert any(a["id"].startswith("quest::") for a in pack["assets"])
    quests = _json.loads((isolated_data_dir / "quests.json").read_text())
    assert quests and quests[-1]["status"] == "active"

    from cardinal.core import db

    genomes = db.query("SELECT * FROM enemy_genomes")
    assert genomes, "every generated enemy must have a persisted genome"
    registry = db.query("SELECT * FROM quest_registry WHERE status='active'")
    assert registry


@pytest.mark.agent
def test_unregistered_assets_rejected(isolated_data_dir, cardinal_env, tmp_path):
    """The gate refuses quests referencing asset IDs absent from asset-pack.json."""
    from cardinal import sub_process

    asset_file = tmp_path / "quest_assets_fake.py"
    asset_file.write_text("QUEST_TITLE = 'Fake'\n", encoding="utf-8")
    result = sub_process.approve_mutation(
        "quest_install", "test",
        {"gdd": {**GOOD_GDD}, "asset_file": str(asset_file),
         "asset_ids": ["quest::never_registered"],
         "source_url": "", "source_name": "test"})
    assert not result.approved
    assert any("unregistered" in r for r in result.reasons)
