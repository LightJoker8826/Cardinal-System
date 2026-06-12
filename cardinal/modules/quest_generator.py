"""Social Control Module — autonomous quest & world generation.

Canon: Cardinal's Automatic Quest Generation Function collects folklore and
legends from the open internet and synthesizes endless quests — with the
authority to restructure the game world itself.

Pipeline:
  source adapter (pluggable; Wikipedia default) -> pre-LLM quality filters
  -> physics-first classifier -> GDD generation (ONE L3 call per attempt,
  L2 fallback; up to 3 attempts) -> deterministic quality filter ->
  SEC stat mutation for every generated enemy (unique genomes) ->
  asset ID registration (asset-pack.json, BEFORE approval) ->
  quest_assets_{slug}.py code generation -> Sub-Process gate install.
"""
from __future__ import annotations

import json
import random
import re
from datetime import datetime, timezone
from typing import Any

from cardinal.core import db
from cardinal.core.config import SEV_INFO, SEV_WARNING, get_config, log_event
from cardinal.llm.provider import MAX_TOKENS_GDD, complete_with_fallback
from cardinal.modules import sec
from cardinal.modules.quality_filter import score_gdd
from cardinal.sources.base import SourceQualityError, TopicData, get_source
from cardinal import sub_process

MAX_GENERATION_ATTEMPTS = 3

GDD_SYSTEM_PROMPT = (
    "You are a game designer. Output only valid JSON. No markdown, "
    "no commentary, no code fences. When you have the data, output immediately."
)

ARCHETYPES = {
    "vertical": ("mountain", "climb", "tower", "ascend", "peak", "cliff", "summit"),
    "horizontal": ("sea", "river", "desert", "plains", "travel", "ocean", "voyage", "storm"),
    "dungeon": ("cave", "tomb", "temple", "underground", "ritual", "crypt", "labyrinth"),
}


# ===========================================================================
# Physics-first classifier
# ===========================================================================

def classify_physics(text: str) -> str:
    lowered = text.lower()
    counts = {
        archetype: sum(lowered.count(kw) for kw in keywords)
        for archetype, keywords in ARCHETYPES.items()
    }
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] > 0 else "open_field"


def build_gdd_prompt(topic_text: str, archetype: str) -> str:
    return (
        f"Topic: {topic_text[:6000]}\n"
        f"Map archetype: {archetype}\n\n"
        "Generate a Game Design Document as JSON with this exact schema:\n"
        "{\n"
        '  "title": string,\n'
        '  "narrative": string (3 sentences),\n'
        '  "stages": [\n'
        '    {"stage": 1, "description": string, "objective": string},\n'
        '    {"stage": 2, "description": string, "objective": string},\n'
        '    {"stage": 3, "description": string, "objective": string}\n'
        "  ],\n"
        '  "npcs": [{"name": string, "role": string, "dialogue": string}],\n'
        '  "enemies": [{"name": string, "damage": int, "hp": int, "reward_gold": int}],\n'
        '  "rewards": [{"item": string, "quantity": int}],\n'
        '  "map_archetype": string,\n'
        '  "world_changes": [{"key": string, "description": string}] (optional, may be empty)\n'
        "}\n"
        f'The "map_archetype" field MUST be exactly "{archetype}". '
        "Enemy damage must be 1-300, hp 1-5000, reward_gold 0-2000."
    )


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", title).strip("_").lower()
    if not slug or slug[0].isdigit():
        slug = "quest_" + slug
    return slug[:60]


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    # tolerate prose around the JSON object
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return text


# ===========================================================================
# Asset generation
# ===========================================================================

def register_assets(slug: str, gdd: dict[str, Any]) -> list[str]:
    """Register every asset ID this quest references in asset-pack.json.
    MUST happen before the gate call — unregistered references are rejected."""
    cfg = get_config()
    pack_path = cfg.data_dir / "asset-pack.json"
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    existing = {a["id"] for a in pack["assets"]}
    now = datetime.now(timezone.utc).isoformat()

    ids = [f"quest::{slug}", f"module::quest_assets_{slug}"]
    ids += [f"enemy::{slug}::{_slugify(e['name'])}" for e in gdd.get("enemies", [])]
    ids += [f"npc::{slug}::{_slugify(n['name'])}" for n in gdd.get("npcs", [])]
    ids += [f"item::{slug}::{_slugify(r['item'])}" for r in gdd.get("rewards", [])]

    for asset_id in ids:
        if asset_id not in existing:
            pack["assets"].append({"id": asset_id, "quest": slug, "registered_at": now})
    pack_path.write_text(json.dumps(pack, indent=2), encoding="utf-8")
    return ids


def write_quest_assets(slug: str, gdd: dict[str, Any], source_url: str) -> str:
    """Generate quest_assets_{slug}.py — runnable quest code for the game."""
    cfg = get_config()
    path = cfg.project_root / f"quest_assets_{slug}.py"
    npc_dialogue = {n["name"]: n["dialogue"] for n in gdd.get("npcs", [])}
    enemies = gdd.get("enemies", [])
    stages = gdd.get("stages", [])

    code = f'''"""Auto-generated quest assets — {gdd["title"]}

Generated by the Cardinal System Social Control module.
Source: {source_url}
Do not edit by hand; regenerate through quest_generator.py.
"""
from __future__ import annotations

import random

QUEST_TITLE = {gdd["title"]!r}
MAP_ARCHETYPE = {gdd["map_archetype"]!r}
NARRATIVE = {gdd["narrative"]!r}

STAGES = {json.dumps(stages, indent=4)}

NPC_DIALOGUE = {json.dumps(npc_dialogue, indent=4)}

ENEMIES = {json.dumps(enemies, indent=4)}

REWARDS = {json.dumps(gdd.get("rewards", []), indent=4)}


class Quest:
    """Stage machine for {gdd["title"]!r}."""

    def __init__(self) -> None:
        self.stage = 0
        self.completed = False

    def check_trigger(self, player_level: int = 1, floor: int = 1) -> bool:
        """The quest becomes available once any player reaches floor 2."""
        return not self.completed and self.stage == 0 and floor >= 2

    def advance_stage(self) -> dict | None:
        if self.completed:
            return None
        if self.stage < len(STAGES):
            self.stage += 1
            return STAGES[self.stage - 1]
        self.complete()
        return None

    def complete(self) -> list[dict]:
        self.completed = True
        return REWARDS

    def current_objective(self) -> str:
        if self.completed:
            return "Quest complete."
        if 0 < self.stage <= len(STAGES):
            return STAGES[self.stage - 1]["objective"]
        return "Speak to " + (list(NPC_DIALOGUE) or ["the Chronicler"])[0]


def spawn_enemy(rng: random.Random | None = None) -> dict:
    """Spawn one quest enemy using its GDD stats + persisted genome."""
    rng = rng or random.Random()
    enemy = dict(rng.choice(ENEMIES))
    enemy.setdefault("genome", {{}})
    return enemy


def npc_line(name: str) -> str:
    return NPC_DIALOGUE.get(name, "...")
'''
    path.write_text(code, encoding="utf-8")
    return str(path)


# ===========================================================================
# Generation pipeline
# ===========================================================================

def generate_quest(topic: str | None, source_name: str = "wikipedia",
                   random_topic: bool = False) -> dict[str, Any]:
    """Run the full Social Control pipeline. Returns a result dict."""
    db.init_db()
    source = get_source(source_name)

    try:
        topic_data: TopicData = source.fetch_random() if random_topic else source.fetch(topic or "")
    except SourceQualityError as err:
        log_event("quest_generator", f"source rejected pre-LLM: {err}", SEV_WARNING)
        return {"ok": False, "reason": f"source quality filter: {err}"}

    archetype = classify_physics(topic_data.full_text)
    log_event("quest_generator",
              f"topic '{topic_data.title}' ({topic_data.word_count} words, "
              f"source {topic_data.source_name}) -> archetype '{archetype}'", SEV_INFO)

    user_prompt = build_gdd_prompt(topic_data.full_text, archetype)
    last_failure = ""
    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        resp = complete_with_fallback(
            "quest_generator", "gdd", GDD_SYSTEM_PROMPT, user_prompt,
            max_tokens=MAX_TOKENS_GDD,
            context={"topic_title": topic_data.title,
                     "topic_text": topic_data.summary_text,
                     "archetype": archetype},
        )
        try:
            gdd = json.loads(_strip_fences(resp.text))
        except json.JSONDecodeError as err:
            last_failure = f"attempt {attempt}: invalid JSON ({err})"
            log_event("quest_generator", last_failure, SEV_WARNING)
            continue

        quality = score_gdd(gdd, topic_data.full_text, archetype)
        if not quality.passed:
            last_failure = f"attempt {attempt}: quality filter {quality.summary} — {quality.failures}"
            log_event("quest_generator", last_failure, SEV_WARNING)
            continue

        return _install(gdd, topic_data, resp, user_prompt, quality.summary)

    # All attempts failed — discard and log.
    db.execute(
        """INSERT INTO quest_registry (title, source_url, source_name, gdd_json, status, created_at)
           VALUES (?,?,?,?, 'generation_failed', ?)""",
        (topic_data.title, topic_data.source_url, topic_data.source_name,
         json.dumps({"failure": last_failure}), datetime.now(timezone.utc).isoformat()),
    )
    log_event("quest_generator",
              f"GDD for '{topic_data.title}' discarded after {MAX_GENERATION_ATTEMPTS} attempts: "
              f"{last_failure}", SEV_WARNING)
    return {"ok": False, "reason": last_failure}


def _install(gdd: dict[str, Any], topic_data: TopicData, resp, user_prompt: str,
             quality_summary: str) -> dict[str, Any]:
    # SEC: every newly generated enemy receives a random stat mutation and a
    # unique behavior genome — no two generated enemies are identical.
    rng = random.Random()
    entropy = sec.compute_entropy()
    mutated = []
    for enemy in gdd.get("enemies", []):
        m = sec.mutate_enemy_stats(enemy, entropy, rng)
        sec.persist_genome(m["name"], m["genome"], quest_title=gdd["title"])
        mutated.append(m)
    gdd["enemies"] = mutated

    slug = _slugify(gdd["title"])
    asset_ids = register_assets(slug, gdd)          # register BEFORE approval
    asset_file = write_quest_assets(slug, gdd, topic_data.source_url)

    result = sub_process.approve_mutation(
        "quest_install", "quest_generator",
        {"gdd": gdd, "asset_file": asset_file, "asset_ids": asset_ids,
         "source_url": topic_data.source_url, "source_name": topic_data.source_name},
        llm_input=user_prompt[:4000], llm_output=resp.text[:4000],
    )
    if not result.approved:
        return {"ok": False, "reason": f"Sub-Process rejected install: {result.reasons}"}

    log_event("quest_generator",
              f"quest '{gdd['title']}' installed (v{result.version_id}, {quality_summary})", SEV_INFO)
    _notify(gdd, topic_data, result.version_id)
    return {
        "ok": True,
        "title": gdd["title"],
        "slug": slug,
        "asset_file": asset_file,
        "archetype": gdd["map_archetype"],
        "enemies": [e["name"] for e in gdd["enemies"]],
        "version": result.version_id,
        "provider": resp.provider,
        "quality": quality_summary,
    }


def _notify(gdd: dict, topic_data: TopicData, version_id: int | None) -> None:
    try:
        from cardinal.modules import notifier

        notifier.notify_sync(
            "New quest generated",
            f"**{gdd['title']}** installed from {topic_data.source_name} "
            f"({topic_data.title}).\n{gdd['narrative'][:300]}",
            notifier.COLOR_INFO,
            {"archetype": gdd["map_archetype"],
             "enemies": ", ".join(e["name"] for e in gdd["enemies"])[:200],
             "version": f"v{version_id}"},
        )
    except Exception:
        pass
