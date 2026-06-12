"""Order Control Module — economic & combat balance.

Monitors:
  - Gini coefficient of player wealth (exploit/dupe detection)
  - Per-item win rates (overpowered / useless flags)
  - SEC performance (kill-time drops -> policy evolution)
  - Combat activity churn (content drought -> quest generation trigger)
  - Community sentiment soft triggers (alongside the hard thresholds)

Hard thresholds (from the engineering bible):
  G > 0.7           -> suspected exploit; investigate items
  win_rate > 0.80   -> overpowered, flag for nerf
  win_rate < 0.20   -> useless, flag for buff
  kill time -40%    -> SEC policy step
  churn > 20%       -> content drought, trigger quest generator

Exploit-class anomalies (win rate >= 0.95 or stats >4 sigma from the
population) are quarantined deterministically and immediately — the
8%-per-cycle law is for fine tuning, not for catching a 1000x God Sword.

ALL mutations flow through the Sub-Process gate. At most ONE L3 call per
cycle; L2 fallback is immediate on failure.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from rich.console import Console
from rich.table import Table

from cardinal.core import db
from cardinal.core.config import SEV_INFO, SEV_WARNING, get_config, log_event
from cardinal.llm.provider import MAX_TOKENS_PATCH, complete_with_fallback
from cardinal.modules import sec
from cardinal.modules.taboo_index import get_taboo_index
from cardinal import sub_process

console = Console()

GINI_DANGER = 0.7
WIN_RATE_OP = 0.80
WIN_RATE_USELESS = 0.20
CHURN_THRESHOLD = 0.20

BALANCE_SYSTEM_PROMPT = (
    "You are a game balance engine. Output only modified JSON. "
    "Do not add commentary. Adjust numerical values only. "
    "Never change item names, types, or schema structure. "
    "Maximum adjustment per field per cycle: 8%. "
    "When you have enough information, output the JSON immediately."
)


# ===========================================================================
# Calculations
# ===========================================================================

def gini_coefficient(wealth_list: list[float]) -> float:
    """G = 1 - sum_i (X_i - X_{i-1}) * (Y_i + Y_{i-1})
    X = cumulative population share (poorest to richest), Y = cumulative wealth share.
    0 = perfect equality; sample maximum is (n-1)/n."""
    values = sorted(float(v) for v in wealth_list)
    n = len(values)
    if n == 0:
        return 0.0
    total = sum(values)
    if total <= 0:
        return 0.0
    acc = 0.0
    cum_wealth = 0.0
    prev_y = 0.0
    for i, v in enumerate(values, start=1):
        cum_wealth += v
        y = cum_wealth / total
        acc += (1.0 / n) * (y + prev_y)
        prev_y = y
    return 1.0 - acc


def get_win_rates(min_uses: int = 3) -> dict[str, float]:
    return db.get_item_win_rates(min_uses)


def measure_churn(window: int = 200) -> float:
    """Activity drop between the two most recent halves of the combat window."""
    rows = db.query(
        "SELECT timestamp FROM combat_log ORDER BY id DESC LIMIT ?", (window * 2,))
    if len(rows) < window * 2:
        return 0.0
    # equal row counts by construction; churn here means widening time gaps
    from datetime import datetime

    def span(chunk):
        try:
            t0 = datetime.fromisoformat(chunk[-1]["timestamp"])
            t1 = datetime.fromisoformat(chunk[0]["timestamp"])
            return max(1e-6, (t1 - t0).total_seconds())
        except (ValueError, KeyError):
            return None

    recent, older = rows[:window], rows[window:]
    s_recent, s_older = span(recent), span(older)
    if s_recent is None or s_older is None:
        return 0.0
    rate_recent = window / s_recent
    rate_older = window / s_older
    if rate_older <= 0:
        return 0.0
    return max(0.0, (rate_older - rate_recent) / rate_older)


def sentiment_soft_flags(hours: float = 24.0) -> dict[str, float]:
    """Community sentiment as a soft trigger: items with strongly negative
    or positive weighted sentiment get flagged alongside hard thresholds."""
    rows = db.query(
        """SELECT topic, sentiment_score, engagement_weight FROM sentiment_log
           WHERE timestamp >= datetime('now', ?) """,
        (f"-{int(hours)} hours",),
    )
    item_names = {i["name"].lower(): i["name"] for i in _load_items()}
    weighted: dict[str, list[float]] = {}
    for r in rows:
        topic = (r["topic"] or "").lower()
        for lname, name in item_names.items():
            if lname in topic:
                weighted.setdefault(name, []).append(
                    r["sentiment_score"] * (1 + r["engagement_weight"]))
    return {name: sum(vals) / len(vals) for name, vals in weighted.items() if vals}


def _load_items() -> list[dict[str, Any]]:
    path = get_config().data_dir / "items.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ===========================================================================
# Reporting
# ===========================================================================

def build_report() -> dict[str, Any]:
    wealth = db.get_player_wealth_list()
    gini = gini_coefficient(wealth)
    win_rates = get_win_rates()
    items = _load_items()
    flagged = {n: wr for n, wr in win_rates.items()
               if wr > WIN_RATE_OP or wr < WIN_RATE_USELESS}
    anomalies = sub_process.detect_anomalies(win_rates, items)
    soft = sentiment_soft_flags()
    return {
        "gini": gini,
        "players": len(wealth),
        "win_rates": win_rates,
        "flagged": flagged,
        "anomalies": anomalies,
        "soft_flags": soft,
        "churn": measure_churn(),
        "items": items,
    }


def print_report(report: dict[str, Any]) -> None:
    table = Table(title="Cardinal Balance Report", header_style="bold magenta")
    table.add_column("Item")
    table.add_column("Win rate", justify="right")
    table.add_column("Status")
    for name, wr in sorted(report["win_rates"].items(), key=lambda kv: -kv[1]):
        status = "[red]ANOMALY[/red]" if name in report["anomalies"] else (
            "[yellow]overpowered[/yellow]" if wr > WIN_RATE_OP else (
                "[cyan]useless[/cyan]" if wr < WIN_RATE_USELESS else "[green]balanced[/green]"))
        table.add_row(name, f"{wr:.2%}", status)
    console.print(table)
    g = report["gini"]
    style = "red" if g > GINI_DANGER else "green"
    console.print(f"Gini coefficient: [{style}]{g:.3f}[/{style}]  "
                  f"(players: {report['players']}, churn: {report['churn']:.1%})")
    if report["soft_flags"]:
        console.print(f"[dim]Community soft flags: {report['soft_flags']}[/dim]")


# ===========================================================================
# The balance cycle
# ===========================================================================

def run_balance_cycle(allow_questgen: bool = False) -> dict[str, Any]:
    report = build_report()
    gini = report["gini"]
    actions: list[str] = []

    if gini > GINI_DANGER:
        log_event("balancer", f"Gini {gini:.3f} crossed danger threshold {GINI_DANGER}", SEV_WARNING)
        _notify("Gini danger threshold crossed",
                f"G = {gini:.3f} (> {GINI_DANGER}) — suspected exploit/dupe. Investigating items.",
                "danger", {"players": report["players"]})

    # 1. Deterministic anomaly quarantine (no LLM needed for a 1000x sword)
    if report["anomalies"]:
        items = report["items"]
        result = sub_process.approve_mutation(
            "items_json", "balancer",
            {"items": json.loads(json.dumps(items)), "quarantine": report["anomalies"]},
        )
        if result.approved:
            actions.append(f"quarantined: {report['anomalies']}")
            _notify("Weapon quarantined",
                    f"Exploit-class anomaly: {', '.join(report['anomalies'])} disabled pending rebalance.",
                    "danger", {"gini": f"{gini:.3f}"})
            report = build_report()  # refresh post-quarantine

    # 2. Fine-tuning of flagged (non-anomalous) items — ONE LLM call max
    flagged = {n: wr for n, wr in report["flagged"].items() if n not in report["anomalies"]}
    # Soft sentiment flags join the hard flags (never overriding them)
    for name, score in report["soft_flags"].items():
        if name not in flagged and abs(score) >= 0.5:
            wr = report["win_rates"].get(name)
            if wr is not None:
                flagged[name] = wr
                actions.append(f"sentiment soft-flag: {name} ({score:+.2f})")

    changed_fields: dict[str, Any] = {}
    if flagged:
        items = report["items"]
        old_by_name = {i["name"]: dict(i) for i in items}
        user_prompt = (
            f"Current items.json:\n{json.dumps(items, indent=1)}\n\n"
            f"Balance report:\n{json.dumps(report['win_rates'], indent=1)}\n\n"
            f"Gini coefficient: {gini:.3f}\n"
            f"Flagged items: {json.dumps(flagged)}\n\n"
            "Output the corrected items.json with numerical values adjusted "
            "to move win rates closer to 0.50 and Gini below 0.5."
        )
        resp = complete_with_fallback(
            "balancer", "balance", BALANCE_SYSTEM_PROMPT, user_prompt,
            max_tokens=MAX_TOKENS_PATCH,
            context={"items": items, "flagged": flagged, "gini": gini},
        )
        try:
            proposed = json.loads(_strip_fences(resp.text))
        except json.JSONDecodeError:
            proposed = None
            log_event("balancer", "model output was not valid JSON — cycle skipped", SEV_WARNING)
        if proposed is not None:
            result = sub_process.approve_mutation(
                "items_json", "balancer", {"items": proposed},
                llm_input=user_prompt[:4000], llm_output=resp.text[:4000],
            )
            if result.approved:
                new_by_name = {i["name"]: i for i in _load_items()}
                for name in flagged:
                    old, new = old_by_name.get(name, {}), new_by_name.get(name, {})
                    for fieldname in ("damage", "crit_chance", "crit_mult"):
                        if old.get(fieldname) != new.get(fieldname):
                            changed_fields[f"{name}.{fieldname}"] = {
                                "old": old.get(fieldname), "new": new.get(fieldname)}
                actions.append(f"rebalanced {len(flagged)} item(s) via {resp.provider}")
                _print_patch_table(flagged, old_by_name, new_by_name, report["win_rates"])
                _notify("Items rebalanced",
                        f"{len(flagged)} flagged item(s) adjusted (provider: {resp.provider}).",
                        "success", {k: f"{v['old']} -> {v['new']}" for k, v in list(changed_fields.items())[:8]})
            else:
                actions.append(f"rebalance REJECTED by Sub-Process: {result.reasons}")

    gini_after = gini_coefficient(db.get_player_wealth_list())
    db.log_balance_change({
        "gini_before": gini,
        "gini_after": gini_after,
        "changed_fields": changed_fields,
        "trigger_reason": "; ".join(actions) or "routine cycle (no action)",
    })

    # 3. SEC evolution from performance data
    evolved = sec.evolve_all()
    if evolved:
        actions.append(f"SEC evolved: {evolved}")

    # 4. Content drought -> quest generation
    if allow_questgen and report["churn"] > CHURN_THRESHOLD:
        actions.append(f"churn {report['churn']:.1%} > {CHURN_THRESHOLD:.0%} — triggering quest generator")
        try:
            from cardinal.modules.quest_generator import generate_quest

            generate_quest(topic=None, random_topic=True)
        except Exception as err:  # quest gen offline must not break balance
            log_event("balancer", f"quest generation unavailable: {err}", SEV_WARNING)

    log_event("balancer", f"cycle complete: {actions or 'no action needed'}", SEV_INFO)
    return {"report": report, "actions": actions, "gini_after": gini_after}


def _print_patch_table(flagged, old_by_name, new_by_name, win_rates) -> None:
    table = Table(title="Balance Patch", header_style="bold magenta")
    for col in ("Item", "Old dmg", "New dmg", "Win rate"):
        table.add_column(col)
    for name in flagged:
        old, new = old_by_name.get(name, {}), new_by_name.get(name, {})
        table.add_row(name, str(old.get("damage")), str(new.get("damage")),
                      f"{win_rates.get(name, 0):.2%}")
    console.print(table)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def _notify(title: str, message: str, kind: str, fields: dict | None = None) -> None:
    try:
        from cardinal.modules import notifier

        color = {"danger": notifier.COLOR_DANGER, "success": notifier.COLOR_SUCCESS,
                 "warning": notifier.COLOR_WARNING}.get(kind, notifier.COLOR_INFO)
        notifier.notify_sync(title, message, color, fields)
    except Exception:
        pass


# ===========================================================================
# Daemon (async, non-blocking)
# ===========================================================================

async def watch(interval_s: float = 60.0, allow_questgen: bool = True) -> None:
    log_event("balancer", f"Order Control daemon online (every {interval_s:.0f}s)", SEV_INFO)
    while True:
        await asyncio.to_thread(run_balance_cycle, allow_questgen)
        await asyncio.sleep(interval_s)
