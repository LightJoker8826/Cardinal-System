"""MHCP — Mental Health Counseling Program (the Yui module).

Canon: Yui-MHCP001 was created by Cardinal to monitor players' emotional
state via NerveGear brainwave data and remedy distress. (At SAO's launch,
Kayaba revoked her permission to interact — our sandbox defaults the
MHCP_INTERACTION_PERMITTED flag to true so the counseling layer may act.)

Consumes biometric history, computes a rolling distress index per player,
and logs assessments + interventions to mhcp_log. Critical distress fires
the Sanctuary intervention: the player is granted safe passage (a control-
channel sanctuary command) and a CRITICAL alert fires even in Ghost mode.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from cardinal.core import db
from cardinal.core.config import SEV_CRITICAL, SEV_INFO, get_config, log_event

WINDOW = 30           # readings kept per player
DISTRESS_MILD = 0.5   # counseling note
DISTRESS_CRITICAL = 0.8  # Sanctuary intervention

_history: dict[str, deque] = {}


def record_reading(player: str, heart_rate: float, baseline: float, spiked: bool) -> float:
    """Feed one biometric reading; returns the current distress index (0..1)."""
    hist = _history.setdefault(player, deque(maxlen=WINDOW))
    elevation = max(0.0, (heart_rate - baseline) / max(baseline, 1.0))
    hist.append((elevation, 1.0 if spiked else 0.0))

    if not hist:
        return 0.0
    avg_elevation = sum(e for e, _ in hist) / len(hist)
    spike_rate = sum(s for _, s in hist) / len(hist)
    distress = min(1.0, 0.6 * min(1.0, avg_elevation / 0.5) + 0.4 * spike_rate)

    if distress >= DISTRESS_CRITICAL:
        _intervene(player, distress, critical=True)
    elif distress >= DISTRESS_MILD and len(hist) >= 5:
        _intervene(player, distress, critical=False)
    return distress


def _intervene(player: str, distress: float, critical: bool) -> None:
    cfg = get_config()
    permitted = cfg.mhcp_interaction_permitted
    if critical:
        assessment = f"critical distress (index {distress:.2f}) — sustained physiological stress"
        intervention = "sanctuary" if permitted else "monitoring only (interaction not permitted)"
    else:
        assessment = f"elevated distress (index {distress:.2f})"
        intervention = "rest recommendation" if permitted else "monitoring only (interaction not permitted)"

    db.execute(
        """INSERT INTO mhcp_log (player_name, distress_index, assessment, intervention, permitted, critical, timestamp)
           VALUES (?,?,?,?,?,?,?)""",
        (player, round(distress, 4), assessment, intervention,
         1 if permitted else 0, 1 if critical else 0,
         datetime.now(timezone.utc).isoformat()),
    )
    if critical and permitted:
        trigger_sanctuary(player, reason=assessment, source="mhcp")
    elif not critical:
        log_event("mhcp", f"{player}: {assessment} -> {intervention}", SEV_INFO)


def trigger_sanctuary(player: str, reason: str, source: str = "mhcp") -> None:
    """Sanctuary intervention: grant the player protected passage. This is a
    critical alert — it fires even under Ghost Protocol level 0."""
    db.push_control("sanctuary", {"player": player, "reason": reason, "source": source})
    log_event("mhcp", f"SANCTUARY triggered for {player} ({source}): {reason}", SEV_CRITICAL)
    try:
        from cardinal.modules import notifier

        notifier.notify_sync(
            "MHCP Sanctuary intervention",
            f"Player **{player}** placed under sanctuary protection.\nReason: {reason}",
            notifier.COLOR_PURPLE, {"source": source},
        )
    except Exception:
        pass
