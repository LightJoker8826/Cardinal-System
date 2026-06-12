"""Fluctlight memory — encounter biography, pain_memory, survivor identity.

Every encounter_memory row is an experience. entity_registry rows are
identities with continuity. The world remembers; NPCs are not anonymous mobs.
"""
from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timezone
from typing import Any

from cardinal.core import db
from cardinal.core.config import SEV_INFO, log_event
from cardinal.modules import sec

EPITHETS = ("The Unbroken", "Grudge-Bearer", "Pain-Keeper", "The Remembering")
_TYPE_PREFIX = {"Goblin": "gob", "Orc": "orc", "Dark Knight": "dkn", "Shadow Boss": "sb"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_entity_id(enemy_type: str) -> str:
    prefix = _TYPE_PREFIX.get(enemy_type, enemy_type[:3].lower())
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def register_entity(
    enemy_type: str,
    genome: dict[str, Any],
    *,
    lineage_parent_id: str | None = None,
    entity_id: str | None = None,
) -> str:
    eid = entity_id or _new_entity_id(enemy_type)
    now = _now()
    db.execute(
        """INSERT INTO entity_registry
           (entity_id, enemy_type, genome_json, status, survival_count, experience_count,
            lineage_parent_id, epithet, biography_json, created_at, updated_at)
           VALUES (?,?,?,?,0,0,?,NULL,'[]',?,?)
           ON CONFLICT(entity_id) DO UPDATE SET
             genome_json=excluded.genome_json,
             updated_at=excluded.updated_at""",
        (eid, enemy_type, json.dumps(genome), "alive", lineage_parent_id, now, now),
    )
    return eid


def get_entity(entity_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM entity_registry WHERE entity_id=?", (entity_id,))
    if not row:
        return None
    row["genome"] = json.loads(row["genome_json"])
    row["biography"] = json.loads(row.get("biography_json") or "[]")
    return row


def append_biography(entity_id: str, experience: dict[str, Any]) -> None:
    row = db.query_one("SELECT biography_json FROM entity_registry WHERE entity_id=?", (entity_id,))
    if not row:
        return
    bio = json.loads(row["biography_json"] or "[]")
    bio.append(experience)
    db.execute(
        "UPDATE entity_registry SET biography_json=?, updated_at=? WHERE entity_id=?",
        (json.dumps(bio), _now(), entity_id),
    )


def compute_pain_memory(player_name: str, enemy_type: str) -> dict[str, Any]:
    row = db.query_one(
        "SELECT * FROM player_grudges WHERE player_name=? AND enemy_type=?",
        (player_name, enemy_type),
    )
    if not row:
        return {
            "pain_memory": 0.0,
            "kills_by_player": 0,
            "losses_to_player": 0,
            "flees": 0,
            "dominant_weapon": None,
            "last_entity_id": None,
        }
    return dict(row)


def _recompute_pain_memory(kills: int, losses: int, flees: int) -> float:
    total = max(1, kills + losses + flees)
    raw = (kills * 0.6 - losses * 0.3 + flees * 0.1) / total
    return max(0.0, min(1.0, raw))


def _update_grudges(
    player_name: str,
    enemy_type: str,
    outcome: str,
    weapon: str | None,
    floor: int,
    entity_id: str,
) -> float:
    row = db.query_one(
        "SELECT * FROM player_grudges WHERE player_name=? AND enemy_type=?",
        (player_name, enemy_type),
    )
    kills = int(row["kills_by_player"]) if row else 0
    losses = int(row["losses_to_player"]) if row else 0
    flees = int(row["flees"]) if row else 0
    weapons: dict[str, int] = {}
    if row and row.get("dominant_weapon") and outcome == "win":
        weapons[str(row["dominant_weapon"])] = kills

    if outcome == "win":
        kills += 1
        if weapon:
            weapons[weapon] = weapons.get(weapon, 0) + 1
    elif outcome == "loss":
        losses += 1
    elif outcome == "flee":
        flees += 1

    dominant = max(weapons, key=weapons.get) if weapons else (weapon if outcome == "win" else None)
    if row and row.get("dominant_weapon") and not dominant:
        dominant = row["dominant_weapon"]
    pain = _recompute_pain_memory(kills, losses, flees)
    now = _now()

    db.execute(
        """INSERT INTO player_grudges
           (player_name, enemy_type, kills_by_player, losses_to_player, flees,
            pain_memory, dominant_weapon, last_floor, last_entity_id, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(player_name, enemy_type) DO UPDATE SET
             kills_by_player=excluded.kills_by_player,
             losses_to_player=excluded.losses_to_player,
             flees=excluded.flees,
             pain_memory=excluded.pain_memory,
             dominant_weapon=excluded.dominant_weapon,
             last_floor=excluded.last_floor,
             last_entity_id=excluded.last_entity_id,
             updated_at=excluded.updated_at""",
        (player_name, enemy_type, kills, losses, flees, pain, dominant, floor, entity_id, now),
    )
    return pain


def maybe_epithet(entity_id: str, survival_count: int, rng: random.Random) -> str | None:
    if survival_count < 3:
        return None
    row = db.query_one("SELECT epithet FROM entity_registry WHERE entity_id=?", (entity_id,))
    if row and row.get("epithet"):
        return row["epithet"]
    epithet = rng.choice(EPITHETS)
    db.execute(
        "UPDATE entity_registry SET epithet=?, updated_at=? WHERE entity_id=?",
        (epithet, _now(), entity_id),
    )
    return epithet


def select_lineage_genome(enemy_type: str, rng: random.Random) -> tuple[dict[str, Any], str | None]:
    """New birth: optionally inherit genome from a surviving parent."""
    survivors = db.query(
        "SELECT entity_id, genome_json FROM entity_registry WHERE enemy_type=? AND status='alive' "
        "ORDER BY survival_count DESC LIMIT 5",
        (enemy_type,),
    )
    entropy = sec.compute_entropy()
    if survivors and rng.random() < 0.3 * (1 + entropy):
        parent = rng.choice(survivors)
        parent_genome = json.loads(parent["genome_json"])
        fresh = sec.make_genome(enemy_type, rng=rng)
        child = {}
        for key in ("timing_jitter", "damage_jitter", "combo_seed", "combo_shuffle_depth"):
            if key in parent_genome and key in fresh:
                child[key] = parent_genome[key] * 0.6 + fresh[key] * 0.4
            else:
                child[key] = fresh.get(key, parent_genome.get(key))
        pvec = parent_genome.get("resistance_vector", {})
        fvec = fresh.get("resistance_vector", {})
        child["resistance_vector"] = {
            ch: round(pvec.get(ch, 0) * 0.6 + fvec.get(ch, 0) * 0.4, 4)
            for ch in ("physical", "magic", "ranged")
        }
        child["entropy_at_birth"] = fresh.get("entropy_at_birth", entropy)
        return child, parent["entity_id"]
    return sec.make_genome(enemy_type, rng=rng), None


def resolve_spawn_identity(
    enemy_type: str,
    player_name: str,
    rng: random.Random,
) -> dict[str, Any]:
    """Return spawn package: entity_id, genome, reencounter, epithet."""
    pain_row = compute_pain_memory(player_name, enemy_type)
    pain = float(pain_row["pain_memory"])
    entropy = sec.compute_entropy()

    if pain >= 0.3:
        candidates = db.query(
            """SELECT e.* FROM entity_registry e
               WHERE e.enemy_type=? AND e.status='alive' AND e.survival_count >= 1
               ORDER BY e.survival_count DESC LIMIT 5""",
            (enemy_type,),
        )
        if candidates and rng.random() < 0.5 * (1 + entropy * 0.5):
            pick = rng.choice(candidates)
            genome = json.loads(pick["genome_json"])
            epithet = pick.get("epithet")
            return {
                "entity_id": pick["entity_id"],
                "genome": genome,
                "reencounter": True,
                "epithet": epithet,
                "lineage_parent_id": pick.get("lineage_parent_id"),
            }

    genome, parent_id = select_lineage_genome(enemy_type, rng)
    entity_id = register_entity(enemy_type, genome, lineage_parent_id=parent_id)
    return {
        "entity_id": entity_id,
        "genome": genome,
        "reencounter": False,
        "epithet": None,
        "lineage_parent_id": parent_id,
    }


def record_experience(
    entity_id: str,
    enemy_type: str,
    player_name: str,
    outcome: str,
    weapon: str | None,
    floor: int,
    genome: dict[str, Any],
    rng: random.Random | None = None,
) -> float:
    """Write biographical truth after one encounter. Returns updated pain_memory."""
    rng = rng or random.Random()
    pain_delta = 1.0 if outcome == "win" else (-0.5 if outcome == "loss" else 0.2)
    now = _now()

    db.execute(
        """INSERT INTO encounter_memory
           (entity_id, player_name, enemy_type, outcome, weapon_used, floor,
            pain_delta, genome_snapshot_json, timestamp)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (entity_id, player_name, enemy_type, outcome, weapon, floor,
         pain_delta, json.dumps(genome), now),
    )

    experience = {
        "player": player_name,
        "outcome": outcome,
        "floor": floor,
        "weapon": weapon,
        "at": now,
    }
    append_biography(entity_id, experience)

    if outcome == "win":
        db.execute(
            """UPDATE entity_registry SET status='slain', experience_count=experience_count+1,
               updated_at=? WHERE entity_id=?""",
            (now, entity_id),
        )
    else:
        row = db.query_one(
            "SELECT survival_count FROM entity_registry WHERE entity_id=?", (entity_id,),
        )
        sc = int(row["survival_count"]) + 1 if row else 1
        db.execute(
            """UPDATE entity_registry SET status='alive', survival_count=?, experience_count=experience_count+1,
               updated_at=? WHERE entity_id=?""",
            (sc, now, entity_id),
        )
        maybe_epithet(entity_id, sc, rng)

    pain = _update_grudges(player_name, enemy_type, outcome, weapon, floor, entity_id)
    maybe_semantic_consequence(player_name, enemy_type, pain)
    return pain


def maybe_semantic_consequence(player: str, enemy_type: str, pain_memory: float) -> None:
    """Phase 3: system decides lawful realignment when pain_memory saturates."""
    if pain_memory < 0.85:
        return
    log_event(
        "memory",
        f"semantic consequence threshold for {player} vs {enemy_type} (pain={pain_memory:.2f})",
        SEV_INFO,
    )
