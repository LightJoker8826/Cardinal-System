"""The Taboo Index — Cardinal's law system.

NOT a config file. The final authority of the simulation. Even L3/Fable 5
agents cannot override these laws — axiom breaches trigger system-wide rollback.

Canon: the absolute rule layer of the world (Anti-Criminal Code Effect
Areas, Immortal Objects, hard system constraints). Loaded from
data/taboo_index.json with two consumers:

  1. The game, at runtime: safe-zone damage nullification ("Immortal
     Object" effect) and stat ceilings.
  2. The Sub-Process, at mutation time: every AI-generated change (code
     patch, item rebalance, generated quest, world mutation) is checked
     against these laws before it may touch disk.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cardinal.core.config import get_config


class TabooViolation(Exception):
    """A mutation or action violates the Taboo Index."""


class TabooIndex:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else get_config().data_dir / "taboo_index.json"
        with open(self.path, encoding="utf-8") as fh:
            self.laws = json.load(fh)

    # ------------------------------------------------------------------ #
    # Runtime laws (queried by the game each tick)
    # ------------------------------------------------------------------ #
    def in_safe_zone(self, x: int, y: int) -> str | None:
        """Return the safe zone name covering (x, y), else None.
        Inside a safe zone, the Anti-Criminal Code nullifies all damage."""
        for zone in self.laws.get("safe_zones", []):
            if zone["x_min"] <= x <= zone["x_max"] and zone["y_min"] <= y <= zone["y_max"]:
                return zone["name"]
        return None

    def is_immortal_object(self, name: str) -> bool:
        return name in self.laws.get("immortal_objects", [])

    def clamp_player(self, hp: int, gold: int, level: int) -> tuple[int, int, int]:
        p = self.laws["player_laws"]
        return (
            min(hp, p["max_hp"]),
            min(gold, p["max_gold"]),
            min(level, p["max_level"]),
        )

    # ------------------------------------------------------------------ #
    # Mutation laws (enforced by the Sub-Process)
    # ------------------------------------------------------------------ #
    def validate_item(self, item: dict[str, Any]) -> list[str]:
        """Return list of violations (empty == lawful)."""
        violations: list[str] = []
        laws = self.laws["item_laws"]
        for fieldname, bounds in laws.items():
            if fieldname in item and isinstance(item[fieldname], (int, float)):
                v = item[fieldname]
                if not (bounds["min"] <= v <= bounds["max"]):
                    violations.append(
                        f"item '{item.get('name', '?')}' field '{fieldname}'={v} "
                        f"outside lawful range [{bounds['min']}, {bounds['max']}]"
                    )
        return violations

    def validate_enemy(self, enemy: dict[str, Any]) -> list[str]:
        violations: list[str] = []
        laws = self.laws["enemy_laws"]
        for fieldname in ("damage", "hp", "reward_gold"):
            bounds = laws.get(fieldname)
            if bounds and fieldname in enemy and isinstance(enemy[fieldname], (int, float)):
                v = enemy[fieldname]
                if not (bounds["min"] <= v <= bounds["max"]):
                    violations.append(
                        f"enemy '{enemy.get('name', '?')}' field '{fieldname}'={v} "
                        f"outside lawful range [{bounds['min']}, {bounds['max']}]"
                    )
        window = enemy.get("attack_window_s")
        if window is not None and window < laws["attack_window_min_s"]:
            violations.append(
                f"enemy '{enemy.get('name', '?')}' attack_window_s={window} below "
                f"lawful minimum {laws['attack_window_min_s']} (compression ceiling)"
            )
        return violations

    def validate_code(self, code: str) -> list[str]:
        violations = []
        for pattern in self.laws["patch_laws"]["forbidden_code_patterns"]:
            if pattern in code:
                violations.append(f"patch contains forbidden pattern: {pattern!r}")
        return violations

    @property
    def max_delta_pct(self) -> float:
        return float(self.laws["patch_laws"]["max_delta_pct_per_cycle"])

    @property
    def anomaly_win_rate(self) -> float:
        return float(self.laws["patch_laws"]["anomaly_win_rate"])

    @property
    def anomaly_stat_sigma(self) -> float:
        return float(self.laws["patch_laws"]["anomaly_stat_sigma"])

    @property
    def protected_fields(self) -> list[str]:
        return list(self.laws["patch_laws"]["protected_fields"])

    @property
    def enemy_attack_window_min_s(self) -> float:
        return float(self.laws["enemy_laws"]["attack_window_min_s"])


AXIOM_MARKERS = (
    "Taboo violation",
    "outside lawful range",
    "forbidden pattern",
    "below lawful minimum",
    "protected field",
    "compression ceiling",
)


def is_axiom_violation(reason: str) -> bool:
    """True when a rejection reason is a logic failure against immutable law."""
    lower = reason.lower()
    return any(marker.lower() in lower for marker in AXIOM_MARKERS)


def admin_gold_lawful(gold: int) -> bool:
    """Observer injections must not exceed player gold law."""
    return 0 <= gold <= TabooIndex().laws["player_laws"]["max_gold"]


_taboo: TabooIndex | None = None


def get_taboo_index(refresh: bool = False) -> TabooIndex:
    global _taboo
    if _taboo is None or refresh:
        _taboo = TabooIndex()
    return _taboo
