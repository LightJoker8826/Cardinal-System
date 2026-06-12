"""game.py — the L1 Predictor: a headless text-mode rogue-like RPG engine.

This is the deterministic rules layer (zero AI calls). It is also the first
game plugged into the Cardinal middleware: it writes schema-conformant
events to server.log, mirrors state into cardinal.db, and consumes the
sequenced control channel through its adapter.

INTENTIONAL BUG TRIGGERS (Cardinal repairs these autonomously — DO NOT fix
them by hand):
  BUG A: equipping "Cursed Blade" -> 5% chance per combat round of a
         ZeroDivisionError inside calculate_damage(). Full traceback is
         logged with [CARDINAL_ERROR], then re-raised to crash the thread.
  BUG B: player gold > 9999 -> 3% chance per tick of an unbounded loop in
         loot_distribution(). A 5-second watchdog deadline catches it,
         logs [CARDINAL_ERROR], and breaks the loop.
"""
from __future__ import annotations

import json
import random
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from cardinal.core import db
from cardinal.core.config import get_config
from cardinal.core.events import format_line
from cardinal.modules import memory, sec
from cardinal.modules.taboo_index import get_taboo_index

PROJECT_ROOT = Path(__file__).resolve().parent
SERVER_LOG = PROJECT_ROOT / "server.log"

GRID_SIZE = 10
INCARNATE_DURATION_S = 10.0


class GameCrash(Exception):
    """A game thread died from an unpatched bug."""


class LootLoopWatchdogError(Exception):
    """Watchdog tripped: loot_distribution failed to converge within 5s."""


# ===========================================================================
# Logging (the normalized event stream Cardinal consumes)
# ===========================================================================

def write_log(level: str, module: str, message: str) -> None:
    with open(SERVER_LOG, "a", encoding="utf-8") as fh:
        fh.write(format_line(level, module, message) + "\n")


# ===========================================================================
# Items
# ===========================================================================

_items_cache: list[dict] | None = None


def load_items(refresh: bool = False) -> list[dict]:
    global _items_cache
    if _items_cache is None or refresh:
        with open(PROJECT_ROOT / "data" / "items.json", encoding="utf-8") as fh:
            _items_cache = json.load(fh)
    return _items_cache


def enabled_weapons() -> list[dict]:
    return [i for i in load_items() if i["type"] == "weapon" and i.get("enabled")]


def get_item(name: str) -> dict | None:
    for item in load_items():
        if item["name"] == name:
            return item
    return None


# ===========================================================================
# Entities
# ===========================================================================

@dataclass
class Player:
    name: str
    hp: int = 100
    max_hp: int = 100
    gold: int = 0
    level: int = 1
    inventory: list[str] = field(default_factory=lambda: ["Health Potion", "Health Potion"])
    weapon: dict = field(default_factory=lambda: dict(get_item("Iron Sword") or {}))
    x: int = 0
    y: int = 0
    incarnate_until: float = 0.0

    @property
    def incarnate(self) -> bool:
        return time.time() < self.incarnate_until


ENEMY_TABLE = [
    # name, hp, damage, reward_gold, min_floor, weight
    ("Goblin", 30, 6, 12, 1, 50),
    ("Orc", 60, 12, 35, 2, 30),
    ("Dark Knight", 120, 22, 90, 4, 15),
    ("Shadow Boss", 300, 35, 400, 6, 5),
]


@dataclass
class Enemy:
    name: str
    hp: int
    damage: int
    reward_gold: int
    entity_id: str = ""
    genome: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    epithet: str | None = None


def spawn_enemy(floor: int, rng: random.Random, player_name: str = "Unknown") -> Enemy:
    pool = [(n, hp, dmg, gold, w) for n, hp, dmg, gold, mf, w in ENEMY_TABLE if floor >= mf]
    names, weights = [p[0] for p in pool], [p[4] * (1 + 0.1 * floor) for p in pool]
    pick = rng.choices(range(len(pool)), weights=weights, k=1)[0]
    name, hp, dmg, gold, _ = pool[pick]
    scale = 1 + 0.12 * (floor - 1)

    identity = memory.resolve_spawn_identity(name, player_name, rng)
    genome = identity["genome"]
    entity_id = identity["entity_id"]
    params = sec.effective_params(
        name, genome, rng, player_name=player_name, entity_id=entity_id,
    )

    if identity.get("reencounter"):
        label = identity.get("epithet") or entity_id
        write_log(
            "INFO", "game.recognition",
            f"{label} {entity_id} remembers {player_name}",
        )
    else:
        pain_row = memory.compute_pain_memory(player_name, name)
        if float(pain_row["pain_memory"]) >= 0.3:
            write_log(
                "INFO", "game.recognition",
                f"A {name} bears pain_memory of {player_name} ({pain_row['pain_memory']:.2f})",
            )

    return Enemy(
        name=name,
        hp=int(hp * scale),
        damage=int(dmg * scale * params["attack_damage_mult"]),
        reward_gold=int(gold * scale),
        entity_id=entity_id,
        genome=genome,
        params=params,
        epithet=identity.get("epithet"),
    )


# ===========================================================================
# Combat math
# ===========================================================================

def calculate_damage(weapon: dict, player: Player | None = None,
                     rng: random.Random | None = None) -> float:
    """Resolve one attack's damage. Contains intentional BUG A."""
    rng = rng or random.Random()
    damage = weapon.get("damage", 0) * rng.uniform(0.8, 1.2)
    if weapon.get("name") == "Cursed Blade" and rng.random() < 0.05:
        # BUG A (intentional): the curse destabilizes the resonance factor.
        resonance = int(damage) - int(damage)
        damage = damage / resonance
    crit_chance = weapon.get("crit_chance", 0.0)
    crit_mult = weapon.get("crit_mult", 1.0)
    if player is not None and player.incarnate:
        damage *= 1.3
        crit_chance += 0.15
    if rng.random() < crit_chance:
        damage *= crit_mult
    return damage


def loot_distribution(player: Player, enemy: Enemy,
                      rng: random.Random | None = None) -> list[str]:
    """Distribute drops after a kill. Contains intentional BUG B."""
    rng = rng or random.Random()
    drops: list[str] = []
    if player.gold > 9999 and rng.random() < 0.03:
        # BUG B (intentional): the loot table fails to converge once wealth
        # overflows the rarity weighting, looping forever. A 5-second
        # watchdog deadline trips, logs, and breaks the loop.
        deadline = time.time() + 5.0
        weight = 0.0
        while True:
            weight += 0.0  # never converges
            if time.time() > deadline:
                raise LootLoopWatchdogError(
                    f"loot_distribution stuck for 5s (gold={player.gold})"
                )
    for item in load_items():
        if not item.get("enabled"):
            continue
        rarity_weight = {"common": 0.12, "rare": 0.05, "epic": 0.02, "legendary": 0.005}
        if rng.random() < rarity_weight.get(item.get("rarity", "common"), 0.05):
            drops.append(item["name"])
    return drops


def enemy_resistance(enemy: Enemy, weapon: dict) -> float:
    vec = enemy.params.get("resistance_vector", {})
    channel = {"ranged": "ranged", "magic": "magic"}.get(weapon.get("subtype", ""), "physical")
    resist = vec.get(channel, 0.0)
    counter = enemy.params.get("counter_weapons", {})
    resist += counter.get(weapon.get("name", ""), 0.0)
    return min(0.75, resist)


# ===========================================================================
# One combat encounter (a sequence of ticks)
# ===========================================================================

def run_combat(player: Player, enemy: Enemy, rng: random.Random,
               floor: int) -> dict:
    taboo = get_taboo_index()
    ticks = 0
    while player.hp > 0 and enemy.hp > 0:
        ticks += 1
        action = rng.choices(["attack", "use_item", "flee"], weights=[80, 15, 5], k=1)[0]

        if action == "use_item" and any(get_item(i) and (get_item(i) or {}).get("heal") for i in player.inventory):
            for inv_name in list(player.inventory):
                item = get_item(inv_name)
                if item and item.get("heal"):
                    player.hp = min(player.max_hp, player.hp + item["heal"])
                    player.inventory.remove(inv_name)
                    write_log("INFO", "game.combat", f"{player.name} used {inv_name} (+{item['heal']} hp)")
                    break
        elif action == "flee" and rng.random() < 0.5:
            write_log("INFO", "game.combat", f"{player.name} fled from {enemy.name}")
            return {"outcome": "flee", "ticks": ticks, "damage_dealt": 0}
        else:
            try:
                dmg = calculate_damage(player.weapon, player, rng)
            except ZeroDivisionError:
                tb = traceback.format_exc()
                write_log("CARDINAL_ERROR", "game.combat",
                          f"ZeroDivisionError in calculate_damage (weapon={player.weapon.get('name')})\n{tb}")
                raise GameCrash("BUG A: Cursed Blade resonance divide-by-zero") from None
            if rng.random() < enemy.params.get("defense_chance", 0.2):
                dmg *= 0.4
            dmg *= (1 - enemy_resistance(enemy, player.weapon))
            enemy.hp -= int(dmg)
            write_log("INFO", "game.combat",
                      f"{player.name} dealt {int(dmg)} dmg to {enemy.name} "
                      f"({enemy.params.get('policy_mix', '?')})")

        # Enemy counter-attack: combo length from blended policy + genome.
        if enemy.hp > 0:
            zone = taboo.in_safe_zone(player.x, player.y)
            if zone:
                write_log("INFO", "game.taboo",
                          f"Anti-Criminal Code: damage to {player.name} nullified in '{zone}' (Immortal Object)")
            else:
                for _move in enemy.params.get("combo", ["strike"]):
                    counter = enemy.damage * rng.uniform(0.7, 1.1)
                    if player.incarnate:
                        counter *= 0.5  # Incarnate Mode halves effective enemy pressure
                    player.hp -= int(counter)
                    if player.hp <= 0:
                        break

    if player.hp <= 0:
        write_log("INFO", "game.combat", f"{player.name} was slain by {enemy.name} on floor {floor}")
        return {"outcome": "loss", "ticks": ticks, "damage_dealt": 0}

    gold = enemy.reward_gold
    player.gold += gold
    try:
        drops = loot_distribution(player, enemy, rng)
    except LootLoopWatchdogError:
        tb = traceback.format_exc()
        write_log("CARDINAL_ERROR", "game.loot",
                  f"LootLoopWatchdogError in loot_distribution (gold={player.gold})\n{tb}")
        drops = []
    player.inventory.extend(drops)
    write_log("INFO", "game.combat",
              f"{player.name} defeated {enemy.name} (+{gold} gold, drops: {drops or 'none'})")
    return {"outcome": "win", "ticks": ticks, "damage_dealt": enemy.damage, "gold": gold}


# ===========================================================================
# Ghost echoes
# ===========================================================================

def render_ghosts(current_x: int, current_y: int, radius: int = 2) -> list[dict]:
    """Ghost echoes: emotional imprints cached on nearby coordinates."""
    echoes = db.get_spatial_echo(current_x, current_y, radius)
    out = []
    for e in echoes:
        out.append({
            "player": e["player_name"],
            "x": e["x"],
            "y": e["y"],
            "inventory_snapshot": e["inventory_snapshot"],
            "age_seconds": e["age_seconds"],
        })
    return out


# ===========================================================================
# Full playthrough (one bot game)
# ===========================================================================

def run_game(player_name: str, weapon_name: str | None = None,
             max_floor: int = 10, seed: int | None = None,
             adapter=None) -> dict:
    """Run one game until death or floor `max_floor` cleared.
    Raises GameCrash if an unpatched bug kills the thread."""
    rng = random.Random(seed)
    weapons = enabled_weapons()
    if weapon_name:
        weapon = get_item(weapon_name)
        if weapon is None or not weapon.get("enabled"):
            weapon = rng.choice(weapons)
    else:
        weapon = rng.choice(weapons)

    player = Player(name=player_name, weapon=dict(weapon))
    player.x, player.y = rng.randrange(GRID_SIZE), rng.randrange(GRID_SIZE)
    floor = 1
    encounters = 0
    write_log("INFO", "game.run", f"{player_name} enters floor 1 wielding {weapon['name']}")
    if adapter is not None:
        adapter.report_bot_live(player, floor)

    while player.hp > 0 and floor <= max_floor:
        # Safe tick boundary: consume control channel updates in order.
        if adapter is not None:
            adapter.poll_control()
            if adapter.consume_incarnate(player):
                write_log("INFO", "game.incarnate",
                          f"Incarnate Mode active for {player.name} at ({player.x},{player.y})")

        # wander the grid
        player.x = max(0, min(GRID_SIZE - 1, player.x + rng.choice([-1, 0, 1])))
        player.y = max(0, min(GRID_SIZE - 1, player.y + rng.choice([-1, 0, 1])))

        ghosts = render_ghosts(player.x, player.y)
        for ghost in ghosts[:1]:
            write_log("INFO", "game.ghost", f"(echo of {ghost['player']} lingers here)")

        if get_taboo_index().in_safe_zone(player.x, player.y):
            player.hp = min(player.max_hp, player.hp + 5)  # rest in town
            continue

        enemy = spawn_enemy(floor, rng, player_name)
        encounters += 1
        result = run_combat(player, enemy, rng, floor)
        memory.record_experience(
            enemy.entity_id,
            enemy.name,
            player_name,
            result["outcome"],
            player.weapon.get("name"),
            floor,
            enemy.genome,
            rng,
        )
        db.log_combat({
            "player_name": player.name,
            "enemy": enemy.name,
            "outcome": result["outcome"],
            "damage_dealt": result.get("damage_dealt", 0),
            "gold_earned": result.get("gold", 0),
            "weapon_used": player.weapon["name"],
            "floor": floor,
            "duration_ticks": result["ticks"],
        })
        if result["outcome"] == "win":
            player.level = max(player.level, 1 + encounters // 5)
            if encounters % 3 == 0:
                floor += 1
                write_log("INFO", "game.run", f"{player_name} advances to floor {floor}")
        if adapter is not None:
            adapter.report_bot_live(player, floor)

    hp, gold, level = get_taboo_index().clamp_player(player.hp, player.gold, player.level)
    player.hp, player.gold, player.level = hp, gold, level
    db.upsert_player(player.name, max(0, player.hp), player.max_hp,
                     player.gold, player.level, player.inventory)
    cleared = floor > max_floor
    write_log("INFO", "game.run",
              f"{player_name} {'CLEARED floor ' + str(max_floor) if cleared else 'died on floor ' + str(floor)} "
              f"(gold={player.gold}, encounters={encounters})")
    return {
        "player": player.name,
        "cleared": cleared,
        "floor": floor,
        "gold": player.gold,
        "weapon": player.weapon["name"],
        "encounters": encounters,
    }


if __name__ == "__main__":
    db.init_db()
    summary = run_game("Solo_Player")
    print(json.dumps(summary, indent=2))
