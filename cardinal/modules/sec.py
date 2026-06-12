"""SEC — Skilled Experience Catalogue.

Enemy behavioral intelligence with POLICY BLENDING, not hard swaps:

  - Each enemy type runs a weighted mix of two ADJACENT policies on the
    aggression spectrum (passive -> standard -> aggressive -> adaptive).
    Per-action parameters are interpolated by the blend ratio.
  - Blend ratios shift continuously from player performance data in
    cardinal.db (combat_log): players winning too easily pushes the ratio
    toward the harder neighbor; a >= 40% kill-time drop jumps a policy step.
  - Every enemy instance gets a behavior GENOME: attack-timing jitter,
    combo-pattern permutation, and a resistance vector — seeded per spawn,
    so no two enemies share identical attack timing or combo patterns.
  - The ADAPTIVE tier reads combat_log aggregates for:
      * attack window compression — sequences that beat players get
        tighter telegraph windows, weighted by observed success
      * counter-weapon logic — resistances weighted against the weapons
        players are currently winning with the most
  - ENTROPY DRIFT: a monotonically increasing system-age parameter widens
    mutation variance over time, so the enemy population as a whole grows
    progressively more irregular and unpredictable the longer Cardinal runs.

All SEC state changes are mutations and flow through the Sub-Process gate.
"""
from __future__ import annotations

import json
import math
import random
from typing import Any

from cardinal.core import db
from cardinal.core.config import SEV_DEBUG, SEV_INFO, get_config, log_event
from cardinal.modules.taboo_index import get_taboo_index

_policies_cache: dict[str, Any] | None = None


def load_policies(refresh: bool = False) -> dict[str, Any]:
    global _policies_cache
    if _policies_cache is None or refresh:
        path = get_config().data_dir / "sec_policies.json"
        with open(path, encoding="utf-8") as fh:
            _policies_cache = json.load(fh)
    return _policies_cache


# ---------------------------------------------------------------------------
# Entropy drift — irregularity grows with system age
# ---------------------------------------------------------------------------

def compute_entropy() -> float:
    """0.0 (fresh system) -> 1.0 (long-running). Log-scaled on total combat
    events so early growth is felt but late growth keeps creeping."""
    row = db.query_one("SELECT COUNT(*) AS c FROM combat_log")
    ticks = int(row["c"]) if row else 0
    return min(1.0, math.log10(ticks + 1) / 4.0)  # ~10k events -> 1.0


# ---------------------------------------------------------------------------
# Behavior genomes
# ---------------------------------------------------------------------------

def make_genome(enemy_name: str, entropy: float | None = None,
                rng: random.Random | None = None) -> dict[str, Any]:
    """Unique per-spawn behavior genome. Mutation variance scales with
    entropy: an old Cardinal produces far stranger enemies than a young one."""
    rng = rng or random.Random()
    entropy = compute_entropy() if entropy is None else entropy
    spread = 0.08 + 0.30 * entropy  # +-8% young system, up to +-38% aged
    return {
        "timing_jitter": round(rng.uniform(-spread, spread), 4),
        "damage_jitter": round(rng.uniform(-spread, spread), 4),
        "combo_seed": rng.randrange(2**31),
        "combo_shuffle_depth": 1 + int(entropy * 3 * rng.random()),
        "resistance_vector": {
            "physical": round(rng.uniform(0, 0.15 + 0.25 * entropy), 4),
            "magic": round(rng.uniform(0, 0.15 + 0.25 * entropy), 4),
            "ranged": round(rng.uniform(0, 0.15 + 0.25 * entropy), 4),
        },
        "entropy_at_birth": round(entropy, 4),
    }


def persist_genome(enemy_name: str, genome: dict[str, Any], quest_title: str | None = None) -> int:
    from datetime import datetime, timezone

    return db.execute(
        "INSERT INTO enemy_genomes (enemy_name, quest_title, genome_json, created_at) VALUES (?,?,?,?)",
        (enemy_name, quest_title, json.dumps(genome), datetime.now(timezone.utc).isoformat()),
    )


def mutate_enemy_stats(enemy: dict[str, Any], entropy: float | None = None,
                       rng: random.Random | None = None) -> dict[str, Any]:
    """Random stat mutation applied to every newly generated enemy (quest
    generator path). Returns a mutated copy with its genome attached."""
    rng = rng or random.Random()
    entropy = compute_entropy() if entropy is None else entropy
    genome = make_genome(enemy.get("name", "?"), entropy, rng)
    mutated = dict(enemy)
    for fieldname in ("damage", "hp", "reward_gold"):
        if isinstance(mutated.get(fieldname), (int, float)):
            factor = 1.0 + rng.uniform(-0.10, 0.10) * (1 + entropy)
            mutated[fieldname] = max(1, int(round(mutated[fieldname] * factor)))
    mutated["genome"] = genome
    return mutated


# ---------------------------------------------------------------------------
# Blend state + effective parameters
# ---------------------------------------------------------------------------

def get_blend(enemy_type: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM sec_state WHERE enemy_type=?", (enemy_type,))
    if row:
        row["adaptive"] = json.loads(row.get("adaptive_json") or "{}")
        return row
    defaults = load_policies()["defaults"].get(
        enemy_type, {"policy_low": "standard", "policy_high": "aggressive", "blend_ratio": 0.2}
    )
    return {**defaults, "enemy_type": enemy_type, "entropy": compute_entropy(), "adaptive": {}}


def effective_params(enemy_type: str, genome: dict[str, Any] | None = None,
                     rng: random.Random | None = None,
                     player_name: str | None = None,
                     entity_id: str | None = None) -> dict[str, Any]:
    """Resolve the live behavior parameters for one enemy instance:
    adjacent-policy interpolation + adaptive overlays + genome jitter + Fluctlight memory."""
    rng = rng or random.Random()
    policies = load_policies()["policies"]
    blend = get_blend(enemy_type)
    low = policies[blend["policy_low"]]
    high = policies[blend["policy_high"]]
    t = float(blend["blend_ratio"])

    def lerp(a: float, b: float) -> float:
        return a + (b - a) * t

    params: dict[str, Any] = {
        "attack_damage_mult": lerp(low["attack_damage_mult"], high["attack_damage_mult"]),
        "attack_window_s": lerp(low["attack_window_s"], high["attack_window_s"]),
        "defense_chance": lerp(low["defense_chance"], high["defense_chance"]),
        "flee_threshold_hp_pct": lerp(low["flee_threshold_hp_pct"], high["flee_threshold_hp_pct"]),
        "policy_mix": f"{blend['policy_low']}/{blend['policy_high']}@{t:.2f}",
    }

    # Per-attack combo selection: probability t of drawing from the harder pool.
    pool = high["combo_patterns"] if rng.random() < t else low["combo_patterns"]
    combo = list(rng.choice(pool))

    genome = genome or make_genome(enemy_type, rng=rng)
    grng = random.Random(genome["combo_seed"])
    for _ in range(genome.get("combo_shuffle_depth", 1)):
        if len(combo) > 1:
            grng.shuffle(combo)
    params["combo"] = combo
    params["attack_window_s"] = max(
        get_taboo_index().enemy_attack_window_min_s,
        params["attack_window_s"] * (1 + genome["timing_jitter"]),
    )
    params["attack_damage_mult"] *= (1 + genome["damage_jitter"])
    params["resistance_vector"] = genome["resistance_vector"]

    # Adaptive tier overlays (only meaningful when blending into 'adaptive')
    adaptive = blend.get("adaptive") or {}
    if blend["policy_high"] == "adaptive" and adaptive:
        compression = adaptive.get("window_compression", 0.0) * t
        params["attack_window_s"] = max(
            get_taboo_index().enemy_attack_window_min_s,
            params["attack_window_s"] * (1 - compression),
        )
        counter = adaptive.get("counter_weapons", {})
        params["counter_weapons"] = counter

    if player_name:
        from cardinal.modules import memory

        pain_row = memory.compute_pain_memory(player_name, enemy_type)
        pain = float(pain_row["pain_memory"])
        if pain >= 0.3:
            log_event(
                "sec",
                f"{enemy_type} recognizes {player_name} (pain_memory={pain:.2f})",
                SEV_INFO,
            )
        if pain > 0:
            counter = dict(params.get("counter_weapons") or {})
            dw = pain_row.get("dominant_weapon")
            if dw:
                counter[dw] = round(min(0.75, counter.get(dw, 0) + pain * 0.25), 4)
            params["counter_weapons"] = counter
            params["attack_damage_mult"] *= 1 + pain * 0.08
            params["policy_mix"] = f"{params['policy_mix']}+remembers@{pain:.2f}"
        if entity_id:
            ent = memory.get_entity(entity_id)
            if ent and int(ent.get("survival_count") or 0) >= 3:
                params["defense_chance"] = min(0.75, params["defense_chance"] + 0.05)
    return params


# ---------------------------------------------------------------------------
# Performance-driven evolution (called by the balancer)
# ---------------------------------------------------------------------------

def analyze_performance(enemy_type: str, window: int = 200) -> dict[str, Any] | None:
    rows = db.query(
        """SELECT outcome, duration_ticks, weapon_used FROM combat_log
           WHERE enemy=? ORDER BY id DESC LIMIT ?""",
        (enemy_type, window),
    )
    if len(rows) < 10:
        return None
    wins = sum(1 for r in rows if r["outcome"] == "win")
    player_win_rate = wins / len(rows)
    half = len(rows) // 2
    recent_kill = [r["duration_ticks"] for r in rows[:half] if r["outcome"] == "win"]
    older_kill = [r["duration_ticks"] for r in rows[half:] if r["outcome"] == "win"]
    kill_time_drop = 0.0
    if recent_kill and older_kill:
        old_avg = sum(older_kill) / len(older_kill)
        new_avg = sum(recent_kill) / len(recent_kill)
        if old_avg > 0:
            kill_time_drop = max(0.0, (old_avg - new_avg) / old_avg)
    weapon_wins: dict[str, int] = {}
    for r in rows:
        if r["outcome"] == "win" and r["weapon_used"]:
            weapon_wins[r["weapon_used"]] = weapon_wins.get(r["weapon_used"], 0) + 1
    return {
        "player_win_rate": player_win_rate,
        "kill_time_drop": kill_time_drop,
        "weapon_wins": weapon_wins,
        "sample": len(rows),
    }


def evolve(enemy_type: str, force: bool = False) -> dict[str, Any] | None:
    """Compute the next blend state from performance data and submit it
    through the Sub-Process gate. Returns the gate payload (or None)."""
    from cardinal import sub_process

    perf = analyze_performance(enemy_type)
    if perf is None and not force:
        return None
    perf = perf or {"player_win_rate": 0.5, "kill_time_drop": 0.0, "weapon_wins": {}, "sample": 0}

    spectrum: list[str] = load_policies()["spectrum"]
    blend = get_blend(enemy_type)
    low, high, ratio = blend["policy_low"], blend["policy_high"], float(blend["blend_ratio"])

    # Players steamrolling -> shift toward harder neighbor; struggling -> ease off.
    wr = perf["player_win_rate"]
    ratio += (wr - 0.55) * 0.4  # gentle continuous pressure
    if perf["kill_time_drop"] >= 0.40:
        ratio += 0.5  # canon trigger: boss too easy -> jump policy step

    # Walk the spectrum when the ratio saturates.
    while ratio > 1.0 and spectrum.index(high) < len(spectrum) - 1:
        low, high = high, spectrum[spectrum.index(high) + 1]
        ratio -= 1.0
    while ratio < 0.0 and spectrum.index(low) > 0:
        high, low = low, spectrum[spectrum.index(low) - 1]
        ratio += 1.0
    ratio = max(0.0, min(1.0, ratio))

    # Adaptive tier intelligence from combat_log
    adaptive: dict[str, Any] = dict(blend.get("adaptive") or {})
    if high == "adaptive":
        adaptive["window_compression"] = round(min(0.5, max(0.0, (wr - 0.5))), 4)
        total = sum(perf["weapon_wins"].values()) or 1
        adaptive["counter_weapons"] = {
            weapon: round(min(0.4, wins / total * 0.5), 4)
            for weapon, wins in sorted(perf["weapon_wins"].items(), key=lambda kv: -kv[1])[:3]
        }
        floor = get_taboo_index().enemy_attack_window_min_s
        base = load_policies()["policies"]["adaptive"]["attack_window_s"]
        adaptive["attack_window_s"] = max(floor, base * (1 - adaptive["window_compression"]))

    payload = {
        "enemy_type": enemy_type,
        "policy_low": low,
        "policy_high": high,
        "blend_ratio": round(ratio, 4),
        "entropy": round(compute_entropy(), 4),
        "adaptive": adaptive,
    }
    result = sub_process.approve_mutation("sec_update", "sec", payload)
    if result.approved:
        log_event("sec", f"{enemy_type} evolved: {payload['policy_mix'] if 'policy_mix' in payload else payload['blend_ratio']}", SEV_INFO)
    return payload if result.approved else None


def evolve_all() -> list[str]:
    evolved = []
    for enemy_type in load_policies()["defaults"]:
        if evolve(enemy_type):
            evolved.append(enemy_type)
    return evolved
