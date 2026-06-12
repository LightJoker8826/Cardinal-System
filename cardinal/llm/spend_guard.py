"""Hard daily API spend limit — non-negotiable cost safety.

Lives INSIDE the provider layer so no module can bypass it.

  - Cumulative daily cost tracked in cardinal.db api_spend (keyed by date,
    so the counter automatically resets at midnight).
  - At >= 80% of MAX_DAILY_SPEND_USD: one Discord warning fires and
    Cardinal switches to L2 LocalRuleProvider only.
  - At >= 100%: all L3 calls are locked out for the rest of the day and
    the lockout is logged.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from cardinal.core import db
from cardinal.core.config import SEV_CRITICAL, SEV_WARNING, get_config, log_event


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_cost_usd(input_tokens: int, output_tokens: int, cache_read_tokens: int = 0) -> float:
    cfg = get_config()
    return (
        input_tokens / 1_000_000 * cfg.price_input_per_mtok
        + output_tokens / 1_000_000 * cfg.price_output_per_mtok
        + cache_read_tokens / 1_000_000 * cfg.price_cache_read_per_mtok
    )


def today_spend() -> dict:
    row = db.query_one("SELECT * FROM api_spend WHERE day=?", (_today(),))
    return row or {"day": _today(), "spend_usd": 0.0, "calls": 0, "locked_out": 0, "warned_80": 0}


def l3_allowed() -> tuple[bool, str]:
    """May an L3 (cloud) call be made right now?"""
    cfg = get_config()
    row = today_spend()
    if row["locked_out"]:
        return False, "daily spend limit reached — L3 locked out until midnight"
    if cfg.max_daily_spend_usd <= 0:
        return False, "MAX_DAILY_SPEND_USD is 0 — L3 disabled"
    if row["spend_usd"] >= cfg.max_daily_spend_usd:
        _lockout(row)
        return False, "daily spend limit reached — L3 locked out until midnight"
    if row["spend_usd"] >= 0.8 * cfg.max_daily_spend_usd:
        _warn_80(row)
        return False, "80% of daily spend reached — running L2 LocalRuleProvider only"
    return True, "ok"


def record_spend(cost_usd: float) -> None:
    """Accumulate cost and trip thresholds. Called by the provider after every L3 call."""
    cfg = get_config()
    db.execute(
        """INSERT INTO api_spend (day, spend_usd, calls, updated_at)
           VALUES (?,?,1,?)
           ON CONFLICT(day) DO UPDATE SET
             spend_usd = spend_usd + excluded.spend_usd,
             calls = calls + 1,
             updated_at = excluded.updated_at""",
        (_today(), cost_usd, _now()),
    )
    row = today_spend()
    if row["spend_usd"] >= cfg.max_daily_spend_usd and not row["locked_out"]:
        _lockout(row)
    elif row["spend_usd"] >= 0.8 * cfg.max_daily_spend_usd and not row["warned_80"]:
        _warn_80(row)


def _warn_80(row: dict) -> None:
    if row.get("warned_80"):
        return
    db.execute("UPDATE api_spend SET warned_80=1, updated_at=? WHERE day=?", (_now(), row["day"]))
    cfg = get_config()
    msg = (
        f"API spend at ${row['spend_usd']:.2f} — 80% of daily limit "
        f"(${cfg.max_daily_spend_usd:.2f}). Switching to L2 LocalRuleProvider only."
    )
    log_event("spend_guard", msg, SEV_CRITICAL)
    _notify(":warning: Spend guard — 80% threshold", msg, 0xFFA500)


def _lockout(row: dict) -> None:
    if row.get("locked_out"):
        return
    db.execute("UPDATE api_spend SET locked_out=1, updated_at=? WHERE day=?", (_now(), row["day"]))
    cfg = get_config()
    msg = (
        f"API spend hit ${row['spend_usd']:.2f} / ${cfg.max_daily_spend_usd:.2f}. "
        "ALL L3 calls locked out until midnight."
    )
    log_event("spend_guard", msg, SEV_CRITICAL)
    db.log_agent_action(
        {"module": "spend_guard", "action": "l3_lockout", "provider": "guard",
         "input_summary": msg, "output_summary": "locked"}
    )
    _notify(":no_entry: Spend guard — DAILY LIMIT REACHED", msg, 0xFF0000)


def _notify(title: str, message: str, color: int) -> None:
    try:
        from cardinal.modules.notifier import notify_sync

        notify_sync(title, message, color=color)
    except Exception:
        pass  # notification failure must never break cost accounting
