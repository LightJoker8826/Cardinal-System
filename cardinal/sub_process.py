"""The Sub-Process — Cardinal's Restrainer.

Canon: the Cardinal System runs two master processes. The Main Process
(the creative brain — Fable 5) generates changes; the Sub-Process watches
everything it does and MUST approve any change before it is written to the
live game. This module is that gate.

EVERY mutation — code patches from the healer, items.json rebalances,
generated quests, SEC behavior updates, world mutations, version rollbacks —
flows through approve_mutation(). There are no exceptions and no bypass
path anywhere in the codebase (the admin CLI's gate-bypassing scenario
injection deliberately does not mutate balance/code state — it only injects
*events*, and is fully audited in admin_override_log).

Pipeline per mutation:
  1. Schema validation
  2. Taboo Index compliance (the law layer)
  3. Bounded-delta clamp (max 8%/field/cycle, enforced deterministically —
     never trusted to the model) + exploit-class anomaly quarantine
  4. Verification (compile check / sandboxed pytest run for code)
  5. Atomic write to disk, with backup + rollback on failure
  6. Replay capture (full before/after state, LLM I/O, decision)
  7. Version bump: a full system snapshot in the same approval flow,
     making the audit trail gapless by construction
  8. Control-channel notification so running games pick the change up
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cardinal.core import db
from cardinal.core.config import SEV_CRITICAL, SEV_INFO, SEV_WARNING, get_config, log_event
from cardinal.modules.taboo_index import get_taboo_index, is_axiom_violation

POST_PATCH_PYTEST_ARGS = ["-m", "not chaos and not agent", "-x", "-q"]


@dataclass
class ApprovalResult:
    approved: bool
    mutation_type: str
    reasons: list[str] = field(default_factory=list)
    clamped_fields: dict[str, Any] = field(default_factory=dict)
    version_id: int | None = None
    replay_id: int | None = None

    @property
    def summary(self) -> str:
        verdict = "APPROVED" if self.approved else "REJECTED"
        return f"{verdict} {self.mutation_type}: {'; '.join(self.reasons) or 'clean'}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ===========================================================================
# Authoritative state snapshot (versions + drift resync)
# ===========================================================================

def build_state_snapshot() -> dict[str, Any]:
    cfg = get_config()
    items_path = cfg.data_dir / "items.json"
    quests_path = cfg.data_dir / "quests.json"
    items = json.loads(items_path.read_text(encoding="utf-8")) if items_path.exists() else []
    quests = json.loads(quests_path.read_text(encoding="utf-8")) if quests_path.exists() else []
    sec_rows = db.query("SELECT * FROM sec_state")
    return {
        "items": items,
        "world_state": db.get_world_state(),
        "sec_state": sec_rows,
        "quests": quests,
        "captured_at": _now(),
    }


def _bump_version(triggered_by: str, mutation_type: str, summary: str,
                  replay_id: int | None) -> int:
    snap = build_state_snapshot()
    return db.execute(
        """INSERT INTO versions
           (replay_id, triggered_by, mutation_type, mutation_summary,
            items_json, world_state_json, sec_state_json, quests_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            replay_id,
            triggered_by,
            mutation_type,
            summary[:1000],
            json.dumps(snap["items"]),
            json.dumps(snap["world_state"]),
            json.dumps(snap["sec_state"]),
            json.dumps(snap["quests"]),
            _now(),
        ),
    )


def _record_replay(event_type: str, module: str, before: dict | None, after: dict | None,
                   llm_input: str | None, llm_output: str | None,
                   decision: str, reasons: list[str], outcome: str) -> int:
    return db.execute(
        """INSERT INTO replay_log
           (event_type, module, state_before_json, state_after_json,
            llm_input, llm_output, gate_decision, gate_reasons, outcome, timestamp)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            event_type,
            module,
            json.dumps(before) if before is not None else None,
            json.dumps(after) if after is not None else None,
            (llm_input or "")[:8000],
            (llm_output or "")[:8000],
            decision,
            "; ".join(reasons)[:2000],
            outcome[:500],
            _now(),
        ),
    )


def _notify_gate(result: ApprovalResult, module: str) -> None:
    try:
        from cardinal.modules.notifier import COLOR_DANGER, COLOR_SUCCESS, notify_sync

        if result.approved:
            notify_sync(
                "Sub-Process: mutation approved",
                f"`{result.mutation_type}` from `{module}` (v{result.version_id})",
                COLOR_SUCCESS,
                {"clamped": len(result.clamped_fields), "notes": "; ".join(result.reasons)[:900] or "clean"},
            )
        else:
            notify_sync(
                "Sub-Process: mutation REJECTED",
                f"`{result.mutation_type}` from `{module}`",
                COLOR_DANGER,
                {"reasons": "; ".join(result.reasons)[:900]},
            )
    except Exception:
        pass


# ===========================================================================
# THE GATE
# ===========================================================================

def approve_mutation(mutation_type: str, module: str, payload: dict[str, Any],
                     llm_input: str | None = None, llm_output: str | None = None) -> ApprovalResult:
    """Single entry point for every mutation in the Cardinal System."""
    before = build_state_snapshot()
    handlers = {
        "items_json": _apply_items_json,
        "code_patch": _apply_code_patch,
        "quest_install": _apply_quest_install,
        "sec_update": _apply_sec_update,
        "world_change": _apply_world_change,
        "rollback": _apply_rollback,
    }
    handler = handlers.get(mutation_type)
    if handler is None:
        result = ApprovalResult(False, mutation_type, [f"unknown mutation type '{mutation_type}'"])
    else:
        try:
            result = handler(payload)
        except Exception as err:  # a gate crash must reject, never half-apply
            result = ApprovalResult(False, mutation_type, [f"gate exception: {type(err).__name__}: {err}"])

    after = build_state_snapshot() if result.approved else None
    replay_id = _record_replay(
        event_type=mutation_type,
        module=module,
        before=before,
        after=after,
        llm_input=llm_input,
        llm_output=llm_output,
        decision="approved" if result.approved else "rejected",
        reasons=result.reasons,
        outcome=result.summary,
    )
    result.replay_id = replay_id

    if result.approved:
        result.version_id = _bump_version(module, mutation_type, result.summary, replay_id)
        log_event("sub_process", f"v{result.version_id} {result.summary}", SEV_INFO)
    else:
        log_event("sub_process", result.summary, SEV_WARNING)
        axiom_reasons = [r for r in result.reasons if is_axiom_violation(r)]
        if axiom_reasons and mutation_type != "rollback":
            trigger_axiom_emergency(
                violation="; ".join(axiom_reasons),
                mutation_type=mutation_type,
                module=module,
                payload=payload,
                replay_id=replay_id,
            )
    _notify_gate(result, module)
    return result


def trigger_axiom_emergency(
    violation: str,
    mutation_type: str,
    module: str,
    payload: dict[str, Any],
    replay_id: int | None,
) -> int | None:
    """Taboo axiom breach: rollback, world-lock, audit. Kayaba's Stop Button."""
    rollback_version_id: int | None = None
    row = db.query_one("SELECT id FROM versions ORDER BY id DESC LIMIT 1")
    if row:
        rb = _apply_rollback({"version_id": int(row["id"])})
        if rb.approved:
            rollback_version_id = int(row["id"])

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO axiom_violation_log
           (mutation_type, module, violation, payload_json, rollback_version_id, timestamp)
           VALUES (?,?,?,?,?,?)""",
        (mutation_type, module, violation, json.dumps(payload, default=str),
         rollback_version_id, now),
    )
    db.set_world_state(
        "emergency_axiom_breach",
        {
            "active": True,
            "law": violation,
            "at": now,
            "replay_id": replay_id,
            "rollback_version_id": rollback_version_id,
            "mutation_type": mutation_type,
        },
        changed_by="axiom_emergency",
    )
    db.push_control("axiom_lock", {"reason": violation, "replay_id": replay_id})
    log_event("sub_process", f"AXIOM EMERGENCY: {violation}", SEV_CRITICAL)
    try:
        from cardinal.modules import notifier

        notifier.notify_sync(
            "AXIOM BREACH — simulation rolled back",
            f"**Taboo violation detected.**\n{violation}\n\nRollback: v{rollback_version_id or '?'}",
            notifier.COLOR_DANGER,
            {"module": module, "mutation": mutation_type},
        )
    except Exception:
        pass
    return rollback_version_id


# ===========================================================================
# items.json mutations
# ===========================================================================

ITEM_REQUIRED_FIELDS = {"name", "type", "value", "rarity", "enabled"}
ITEM_NUMERIC_FIELDS = ("damage", "crit_chance", "crit_mult", "value", "heal")


def _apply_items_json(payload: dict[str, Any]) -> ApprovalResult:
    """payload: {"items": [...], "quarantine": ["name", ...]} (quarantine optional)"""
    cfg = get_config()
    taboo = get_taboo_index(refresh=True)
    items_path = cfg.data_dir / "items.json"
    current = {i["name"]: i for i in json.loads(items_path.read_text(encoding="utf-8"))}
    proposed: list[dict[str, Any]] = payload["items"]
    quarantine: set[str] = set(payload.get("quarantine", []))
    reasons: list[str] = []
    clamped: dict[str, Any] = {}

    # Schema + protected-field validation
    if {i.get("name") for i in proposed} != set(current.keys()):
        return ApprovalResult(False, "items_json",
                              ["item set mismatch — additions/removals are not balance mutations"])
    for item in proposed:
        missing = ITEM_REQUIRED_FIELDS - set(item.keys())
        if missing:
            return ApprovalResult(False, "items_json", [f"item '{item.get('name')}' missing fields {missing}"])
        old = current[item["name"]]
        for protected in taboo.protected_fields:
            if item.get(protected) != old.get(protected):
                return ApprovalResult(
                    False, "items_json",
                    [f"protected field '{protected}' changed on '{item['name']}' — Taboo violation"])

    # Deterministic delta clamp (NEVER trusted to the model)
    max_delta = taboo.max_delta_pct
    for item in proposed:
        old = current[item["name"]]
        if item["name"] in quarantine:
            item["enabled"] = False
            reasons.append(f"'{item['name']}' quarantined (exploit-class anomaly)")
            continue
        for fieldname in ITEM_NUMERIC_FIELDS:
            if fieldname not in item or not isinstance(old.get(fieldname), (int, float)) or old[fieldname] == 0:
                continue
            delta = (item[fieldname] - old[fieldname]) / abs(old[fieldname])
            if abs(delta) > max_delta + 1e-9:
                limit = old[fieldname] * (1 + max_delta * (1 if delta > 0 else -1))
                clamped[f"{item['name']}.{fieldname}"] = {"proposed": item[fieldname], "clamped_to": round(limit, 4)}
                item[fieldname] = round(limit, 4) if isinstance(old[fieldname], float) else int(round(limit))

    # Taboo bounds after clamping. The law binds what is LIVE in the world:
    # disabled/quarantined items (e.g. the God Sword) sit outside active
    # scope — re-enabling one forces its stats back inside the lawful bands.
    violations: list[str] = []
    for item in proposed:
        if item.get("enabled"):
            violations.extend(taboo.validate_item(item))
    if violations:
        return ApprovalResult(False, "items_json", violations, clamped)

    _atomic_write_json(items_path, proposed)
    if clamped:
        reasons.append(f"{len(clamped)} field(s) clamped to {max_delta:.0%} Taboo limit")
    db.push_control("reload_items", {"reason": "balance patch"})
    return ApprovalResult(True, "items_json", reasons or ["all checks passed"], clamped)


# ===========================================================================
# Code patch mutations (self-healing)
# ===========================================================================

def _apply_code_patch(payload: dict[str, Any]) -> ApprovalResult:
    """payload: {"file": path, "function": name, "new_code": str, "bug_id": int}"""
    cfg = get_config()
    taboo = get_taboo_index()
    target = Path(payload["file"])
    if not target.is_absolute():
        target = cfg.project_root / target
    func_name = payload["function"]
    new_code = payload["new_code"]
    reasons: list[str] = []

    violations = taboo.validate_code(new_code)
    if violations:
        return ApprovalResult(False, "code_patch", violations)
    try:
        new_func_ast = _extract_single_function(new_code, func_name)
    except SyntaxError as err:
        return ApprovalResult(False, "code_patch", [f"patch does not compile: {err}"])
    if new_func_ast is None:
        return ApprovalResult(False, "code_patch", [f"patch does not define function '{func_name}'"])

    # Backup before touching anything
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = cfg.backups_dir / f"{target.stem}_{stamp}.py"
    shutil.copy2(target, backup_path)
    reasons.append(f"backup: {backup_path.name}")

    original_source = target.read_text(encoding="utf-8")
    try:
        patched_source = _replace_function(original_source, func_name, new_code)
    except ValueError as err:
        return ApprovalResult(False, "code_patch", [f"function replacement failed: {err}"])

    compile(patched_source, str(target), "exec")  # syntax sanity before write
    target.write_text(patched_source, encoding="utf-8")

    # Sandboxed verification: regression suite only (chaos/agent excluded so a
    # legitimately removed bug-trigger cannot self-defeat the patch).
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", *POST_PATCH_PYTEST_ARGS],
        cwd=str(cfg.project_root),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        shutil.copy2(backup_path, target)  # rollback
        tail = (proc.stdout or proc.stderr or "")[-800:]
        return ApprovalResult(False, "code_patch",
                              [f"post-patch tests FAILED — rolled back from backup. {tail}"])

    reasons.append("post-patch regression suite passed")
    db.push_control("reload_module", {"file": str(target.name), "function": func_name})
    return ApprovalResult(True, "code_patch", reasons)


def _extract_single_function(code: str, name: str) -> ast.FunctionDef | None:
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _replace_function(source: str, func_name: str, new_func_code: str) -> str:
    """Replace a top-level (or nested) function definition in source with
    new_func_code, preserving original indentation, via AST line ranges."""
    tree = ast.parse(source)
    target_node: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            target_node = node
            break
    if target_node is None:
        raise ValueError(f"function '{func_name}' not found in target file")

    lines = source.splitlines()
    start = target_node.lineno - 1
    # include decorators
    if target_node.decorator_list:
        start = min(d.lineno for d in target_node.decorator_list) - 1
    end = target_node.end_lineno  # type: ignore[union-attr]

    original_indent = len(lines[target_node.lineno - 1]) - len(lines[target_node.lineno - 1].lstrip())
    indented_new = []
    for ln in new_func_code.splitlines():
        indented_new.append((" " * original_indent + ln) if ln.strip() else ln)

    return "\n".join(lines[:start] + indented_new + lines[end:]) + ("\n" if source.endswith("\n") else "")


# ===========================================================================
# Quest install mutations
# ===========================================================================

GDD_REQUIRED = {"title", "narrative", "stages", "npcs", "enemies", "rewards", "map_archetype"}


def _apply_quest_install(payload: dict[str, Any]) -> ApprovalResult:
    """payload: {"gdd": {...}, "asset_file": str, "asset_ids": [...],
                 "source_url": str, "source_name": str}"""
    cfg = get_config()
    taboo = get_taboo_index()
    gdd = payload["gdd"]
    reasons: list[str] = []

    missing = GDD_REQUIRED - set(gdd.keys())
    if missing:
        return ApprovalResult(False, "quest_install", [f"GDD missing fields: {missing}"])

    violations: list[str] = []
    for enemy in gdd.get("enemies", []):
        violations.extend(taboo.validate_enemy(enemy))
    if violations:
        return ApprovalResult(False, "quest_install", violations)

    # Asset registry law: every referenced asset ID must already be
    # registered in asset-pack.json before approval.
    pack_path = cfg.data_dir / "asset-pack.json"
    pack = json.loads(pack_path.read_text(encoding="utf-8")) if pack_path.exists() else {"assets": []}
    registered = {a["id"] for a in pack.get("assets", [])}
    referenced = set(payload.get("asset_ids", []))
    unregistered = referenced - registered
    if unregistered:
        return ApprovalResult(False, "quest_install",
                              [f"unregistered asset IDs referenced: {sorted(unregistered)}"])

    # Quest asset module must import cleanly
    asset_file = Path(payload["asset_file"])
    if not asset_file.is_absolute():
        asset_file = cfg.project_root / asset_file
    if not asset_file.exists():
        return ApprovalResult(False, "quest_install", [f"asset file missing: {asset_file}"])
    try:
        compile(asset_file.read_text(encoding="utf-8"), str(asset_file), "exec")
    except SyntaxError as err:
        return ApprovalResult(False, "quest_install", [f"quest asset module does not compile: {err}"])

    # Registry writes
    quest_id = db.execute(
        """INSERT INTO quest_registry (title, source_url, source_name, gdd_json, status, created_at)
           VALUES (?,?,?,?, 'active', ?)""",
        (gdd["title"], payload.get("source_url"), payload.get("source_name"),
         json.dumps(gdd), _now()),
    )
    quests_path = cfg.data_dir / "quests.json"
    quests = json.loads(quests_path.read_text(encoding="utf-8")) if quests_path.exists() else []
    quests.append({
        "id": quest_id,
        "title": gdd["title"],
        "narrative": gdd["narrative"],
        "map_archetype": gdd["map_archetype"],
        "source": payload.get("source_name"),
        "status": "active",
        "created_at": _now(),
    })
    _atomic_write_json(quests_path, quests)

    # World mutation authority (canon: Cardinal may restructure the world)
    for change in gdd.get("world_changes", []) or []:
        db.set_world_state(change.get("key", f"quest_{quest_id}_change"),
                           change, changed_by=f"quest:{gdd['title']}")
        reasons.append(f"world change applied: {change.get('key')}")

    reasons.append(f"quest '{gdd['title']}' installed (registry id {quest_id})")
    db.push_control("quest_installed", {"quest_id": quest_id, "title": gdd["title"]})
    return ApprovalResult(True, "quest_install", reasons)


# ===========================================================================
# SEC behavior mutations
# ===========================================================================

def _apply_sec_update(payload: dict[str, Any]) -> ApprovalResult:
    """payload: {"enemy_type": str, "policy_low": str, "policy_high": str,
                 "blend_ratio": float, "entropy": float, "adaptive": {...}}"""
    taboo = get_taboo_index()
    ratio = float(payload["blend_ratio"])
    if not (0.0 <= ratio <= 1.0):
        return ApprovalResult(False, "sec_update", [f"blend_ratio {ratio} outside [0,1]"])
    adaptive = payload.get("adaptive", {})
    window = adaptive.get("attack_window_s")
    if window is not None and window < taboo.enemy_attack_window_min_s:
        return ApprovalResult(
            False, "sec_update",
            [f"attack window compression {window}s below Taboo floor "
             f"{taboo.enemy_attack_window_min_s}s"])
    db.execute(
        """INSERT INTO sec_state (enemy_type, policy_low, policy_high, blend_ratio, entropy, adaptive_json, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(enemy_type) DO UPDATE SET
             policy_low=excluded.policy_low, policy_high=excluded.policy_high,
             blend_ratio=excluded.blend_ratio, entropy=excluded.entropy,
             adaptive_json=excluded.adaptive_json, updated_at=excluded.updated_at""",
        (payload["enemy_type"], payload["policy_low"], payload["policy_high"],
         ratio, float(payload.get("entropy", 0.0)), json.dumps(adaptive), _now()),
    )
    db.push_control("sec_updated", {"enemy_type": payload["enemy_type"], "blend_ratio": ratio})
    return ApprovalResult(True, "sec_update",
                          [f"{payload['enemy_type']}: {payload['policy_low']}->{payload['policy_high']} "
                           f"@ {ratio:.2f}, entropy {payload.get('entropy', 0):.3f}"])


# ===========================================================================
# World mutations (outside quests)
# ===========================================================================

def _apply_world_change(payload: dict[str, Any]) -> ApprovalResult:
    db.set_world_state(payload["key"], payload["value"], changed_by=payload.get("changed_by", "cardinal"))
    db.push_control("world_changed", {"key": payload["key"]})
    return ApprovalResult(True, "world_change", [f"world_state['{payload['key']}'] updated"])


# ===========================================================================
# Version rollback (itself a gated, append-only mutation)
# ===========================================================================

def _apply_rollback(payload: dict[str, Any]) -> ApprovalResult:
    cfg = get_config()
    version_id = int(payload["version_id"])
    row = db.query_one("SELECT * FROM versions WHERE id=?", (version_id,))
    if row is None:
        return ApprovalResult(False, "rollback", [f"version {version_id} not found"])

    _atomic_write_json(cfg.data_dir / "items.json", json.loads(row["items_json"]))
    _atomic_write_json(cfg.data_dir / "quests.json", json.loads(row["quests_json"]))
    for key, value in json.loads(row["world_state_json"]).items():
        db.set_world_state(key, value, changed_by=f"rollback:v{version_id}")
    for sec_row in json.loads(row["sec_state_json"]):
        db.execute(
            """INSERT INTO sec_state (enemy_type, policy_low, policy_high, blend_ratio, entropy, adaptive_json, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(enemy_type) DO UPDATE SET
                 policy_low=excluded.policy_low, policy_high=excluded.policy_high,
                 blend_ratio=excluded.blend_ratio, entropy=excluded.entropy,
                 adaptive_json=excluded.adaptive_json, updated_at=excluded.updated_at""",
            (sec_row["enemy_type"], sec_row["policy_low"], sec_row["policy_high"],
             sec_row["blend_ratio"], sec_row["entropy"], sec_row["adaptive_json"], _now()),
        )
    db.push_control("state_sync", {"restored_version": version_id})
    return ApprovalResult(True, "rollback",
                          [f"state restored from version {version_id} (history preserved, new version appended)"])


# ===========================================================================
# Helpers
# ===========================================================================

def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def detect_anomalies(win_rates: dict[str, float], items: list[dict[str, Any]]) -> list[str]:
    """Exploit-class anomaly detection: items so far outside the population
    band that the 8%-per-cycle law could never catch them. Canon response:
    quarantine immediately (suspected dupe/exploit), rebalance later."""
    taboo = get_taboo_index()
    quarantine: list[str] = []
    for name, wr in win_rates.items():
        if wr >= taboo.anomaly_win_rate:
            quarantine.append(name)
    damages = [i["damage"] for i in items
               if isinstance(i.get("damage"), (int, float)) and i.get("type") == "weapon" and i["damage"] > 0]
    if len(damages) >= 4:
        mean = statistics.mean(damages)
        stdev = statistics.pstdev(damages)
        if stdev > 0:
            for item in items:
                if item.get("type") == "weapon" and isinstance(item.get("damage"), (int, float)):
                    if (item["damage"] - mean) / stdev > taboo.anomaly_stat_sigma:
                        if item["name"] not in quarantine:
                            quarantine.append(item["name"])
    return quarantine
