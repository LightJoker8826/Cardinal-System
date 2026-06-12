"""Quest quality filter — deterministic L2 scoring, zero API spend.

Before any generated GDD reaches the Sub-Process gate it is scored on four
dimensions. Below-threshold on ANY dimension fails the GDD (the generator
regenerates up to 3 times, then discards and logs generation_failed).

  1. narrative coherence    — structure, length, topic-term overlap
  2. stat validity          — enemy/reward numbers inside Taboo bands
  3. archetype consistency  — GDD archetype matches the classifier output
  4. NPC dialogue quality   — non-empty, substantive, no placeholders
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from cardinal.modules.taboo_index import get_taboo_index

PASS_THRESHOLD = 0.6
PLACEHOLDERS = ("lorem", "todo", "tbd", "placeholder", "<string>", "string here",
                "insert ", "xxx", "fixme")
STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "in", "on", "to", "is", "was", "were",
    "for", "with", "by", "as", "at", "from", "that", "this", "it", "its", "be",
}


@dataclass
class QualityReport:
    scores: dict[str, float] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(v >= PASS_THRESHOLD for v in self.scores.values())

    @property
    def summary(self) -> str:
        parts = ", ".join(f"{k}={v:.2f}" for k, v in self.scores.items())
        return f"{'PASS' if self.passed else 'FAIL'} ({parts})"


def _topic_terms(topic_text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z]{4,}", topic_text.lower())
    return {w for w in words if w not in STOPWORDS}


def score_gdd(gdd: dict[str, Any], topic_text: str, expected_archetype: str) -> QualityReport:
    report = QualityReport()

    # 1. Narrative coherence
    narrative = str(gdd.get("narrative", ""))
    stages = gdd.get("stages", [])
    sentences = [s for s in re.split(r"[.!?]+", narrative) if s.strip()]
    score = 0.0
    if len(sentences) >= 2:
        score += 0.3
    if len(narrative) >= 80:
        score += 0.2
    if isinstance(stages, list) and len(stages) >= 3 and all(
            (s.get("description") or "").strip() and (s.get("objective") or "").strip()
            for s in stages):
        score += 0.3
    terms = _topic_terms(topic_text)
    gdd_text = (narrative + " " + " ".join(str(s.get("description", "")) for s in stages)).lower()
    overlap = sum(1 for t in list(terms)[:200] if t in gdd_text)
    if overlap >= 2 or (terms and str(gdd.get("title", "")).lower() in topic_text.lower()):
        score += 0.2
    elif overlap >= 1:
        score += 0.1
    report.scores["narrative_coherence"] = min(1.0, score)
    if report.scores["narrative_coherence"] < PASS_THRESHOLD:
        report.failures.append("narrative lacks structure or topic grounding")

    # 2. Stat validity (Taboo bands)
    taboo = get_taboo_index()
    enemies = gdd.get("enemies", [])
    violations: list[str] = []
    for enemy in enemies:
        violations.extend(taboo.validate_enemy(enemy))
    rewards_ok = all(
        isinstance(r.get("quantity"), int) and r["quantity"] >= 1
        for r in gdd.get("rewards", []))
    stat_score = 1.0
    if not enemies:
        stat_score -= 0.5
    if violations:
        stat_score -= min(0.8, 0.3 * len(violations))
    if not rewards_ok:
        stat_score -= 0.3
    report.scores["stat_validity"] = max(0.0, stat_score)
    if violations:
        report.failures.extend(violations)

    # 3. Map archetype consistency
    report.scores["archetype_consistency"] = (
        1.0 if gdd.get("map_archetype") == expected_archetype else 0.0)
    if report.scores["archetype_consistency"] < PASS_THRESHOLD:
        report.failures.append(
            f"archetype mismatch: GDD says '{gdd.get('map_archetype')}', "
            f"classifier says '{expected_archetype}'")

    # 4. NPC dialogue quality
    npcs = gdd.get("npcs", [])
    if not npcs:
        report.scores["dialogue_quality"] = 0.0
        report.failures.append("no NPCs in GDD")
    else:
        good = 0
        for npc in npcs:
            line = str(npc.get("dialogue", "")).strip()
            if len(line) >= 20 and not any(p in line.lower() for p in PLACEHOLDERS):
                good += 1
        report.scores["dialogue_quality"] = good / len(npcs)
        if report.scores["dialogue_quality"] < PASS_THRESHOLD:
            report.failures.append("NPC dialogue is empty, too short, or placeholder text")

    return report
