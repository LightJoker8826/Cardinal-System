"""Error Control Module — the self-healing daemon.

Watches the game's event stream (server.log) for [CARDINAL_ERROR] entries
and autonomously repairs the broken code:

  detect -> parse traceback -> log bug -> isolate broken function ->
  ONE L3 patch attempt (L2 fallback immediately on failure) ->
  Sub-Process gate (taboo check, AST replacement, backup, sandboxed
  regression tests, atomic write, rollback on failure, version bump) ->
  control-channel reload signal -> the game hot-swaps at a safe boundary.

The healer itself never writes code to disk — only the gate does.

API key handling: set ANTHROPIC_API_KEY in your .env file or shell
environment before running this module. Without it, the deterministic L2
LocalRuleProvider repairs the known bug classes locally.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel

from cardinal.core import db
from cardinal.core.config import SEV_CRITICAL, SEV_INFO, SEV_WARNING, get_config, log_event
from cardinal.core.events import GameEvent, parse_stream, parse_traceback
from cardinal.llm.provider import MAX_TOKENS_PATCH, complete_with_fallback
from cardinal import sub_process

console = Console()

POLL_INTERVAL_S = 2.0

HEALER_SYSTEM_PROMPT = (
    "You are a code repair engine. When you have enough information to act, "
    "act immediately. Output only the corrected Python code for the function "
    "that caused the error. Do not add commentary, explanations, or markdown "
    "fences. Output raw Python only."
)


# ===========================================================================
# Source isolation
# ===========================================================================

def find_enclosing_function(source: str, line: int) -> tuple[str, str] | None:
    """Return (function_name, function_source) for the function containing
    `line`, or None."""
    tree = ast.parse(source)
    best: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= line <= end:
                if best is None or node.lineno >= best.lineno:
                    best = node  # innermost wins
    if best is None:
        return None
    lines = source.splitlines()
    segment = "\n".join(lines[best.lineno - 1: best.end_lineno])  # type: ignore[union-attr]
    # normalize indentation so the snippet is a valid top-level function
    indent = len(lines[best.lineno - 1]) - len(lines[best.lineno - 1].lstrip())
    if indent:
        segment = "\n".join(ln[indent:] if len(ln) >= indent else ln for ln in segment.splitlines())
    return best.name, segment


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


# ===========================================================================
# Repair pipeline
# ===========================================================================

def repair_from_event(event: GameEvent) -> bool:
    """Full repair pipeline for one [CARDINAL_ERROR] event.
    Returns True if a patch was approved and applied."""
    cfg = get_config()
    info = parse_traceback(event.full_text)
    error_type = info.get("error_type") or "UnknownError"
    file = info.get("file")
    line = info.get("line")

    if not file or not line:
        log_event("self_healing", f"could not parse traceback for: {event.message[:120]}", SEV_WARNING)
        return False
    target = Path(file)
    if not target.is_absolute():
        target = cfg.project_root / target
    if not target.exists():
        log_event("self_healing", f"traceback references missing file {target}", SEV_WARNING)
        return False

    # Cross-session dedupe: if this signature was already patched, the log
    # entry is from before the repair — never re-patch a healed function.
    already = db.query_one(
        "SELECT id FROM bugs WHERE error_type=? AND file=? AND status='patched' LIMIT 1",
        (error_type, str(target.name)))
    if already:
        log_event("self_healing",
                  f"{error_type} in {target.name} already patched (bug #{already['id']}) — skipping",
                  SEV_INFO)
        return False

    bug_id = db.log_bug({
        "error_type": error_type,
        "file": str(target.name),
        "line": line,
        "traceback": event.full_text[:8000],
        "status": "detected",
    })
    log_event("self_healing", f"bug #{bug_id} detected: {error_type} in {target.name}:{line}", SEV_INFO)
    _notify("Bug detected",
            f"`{error_type}` in `{target.name}:{line}` — initiating autonomous repair.",
            "warning", {"bug_id": bug_id})

    source = target.read_text(encoding="utf-8")
    located = find_enclosing_function(source, line)
    if located is None:
        db.mark_bug_patched(bug_id, status="patch_failed")
        log_event("self_healing", f"bug #{bug_id}: no enclosing function found", SEV_WARNING)
        return False
    func_name, func_source = located

    user_prompt = (
        f"File: {target.name}\n\n"
        f"Broken code:\n{func_source}\n\n"
        f"Error:\n{event.full_text[:3000]}\n\n"
        "Rewrite only the broken function to eliminate this error safely."
    )
    resp = complete_with_fallback(
        "self_healing", "patch", HEALER_SYSTEM_PROMPT, user_prompt,
        max_tokens=MAX_TOKENS_PATCH,
        context={"code": func_source, "error_type": error_type,
                 "message": event.message, "file": str(target.name), "line": line},
    )
    new_code = _strip_fences(resp.text)

    result = sub_process.approve_mutation(
        "code_patch", "self_healing",
        {"file": str(target), "function": func_name, "new_code": new_code, "bug_id": bug_id},
        llm_input=user_prompt, llm_output=resp.text,
    )

    if result.approved:
        db.mark_bug_patched(bug_id, patch_code=new_code, status="patched")
        console.print(Panel("[bold green]\\[CARDINAL] Bug repaired. System restored.[/bold green]",
                            border_style="green"))
        log_event("self_healing",
                  f"bug #{bug_id} PATCHED ({func_name} in {target.name}, provider {resp.provider}, "
                  f"v{result.version_id})", SEV_INFO)
        _notify("Bug patched",
                f"`{func_name}` in `{target.name}` repaired and verified (version v{result.version_id}).",
                "success", {"bug_id": bug_id, "provider": resp.provider})
        return True

    # Gate rejected: backup already restored by the gate. Alert human.
    db.mark_bug_patched(bug_id, patch_code=new_code, status="patch_failed")
    log_event("self_healing",
              f"bug #{bug_id} PATCH FAILED — original restored from backup. "
              f"HUMAN ATTENTION REQUIRED. Reasons: {result.reasons}", SEV_CRITICAL)
    _notify("Patch FAILED — human attention required",
            f"Repair of `{func_name}` in `{target.name}` was rejected by the Sub-Process. "
            "Original code restored from backup.",
            "danger", {"bug_id": bug_id, "reasons": "; ".join(result.reasons)[:900]})
    return False


# ===========================================================================
# Log tail daemon (async)
# ===========================================================================

class LogTail:
    """Incremental file tail that survives truncation/rotation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = path.stat().st_size if path.exists() else 0

    def read_new(self) -> list[str]:
        if not self.path.exists():
            self.offset = 0
            return []
        size = self.path.stat().st_size
        if size < self.offset:  # truncated/rotated
            self.offset = 0
        if size == self.offset:
            return []
        with open(self.path, encoding="utf-8", errors="replace") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
            self.offset = fh.tell()
        return chunk.splitlines()


async def daemon(once: bool = False, from_start: bool = False) -> None:
    cfg = get_config()
    db.init_db()
    tail = LogTail(cfg.server_log)
    if from_start:
        tail.offset = 0
    handled: set[tuple[str, str]] = set()  # (error_type, function-ish signature) per session
    log_event("self_healing", f"Error Control daemon online — tailing {cfg.server_log.name} "
                              f"every {POLL_INTERVAL_S:.0f}s", SEV_INFO)
    while True:
        lines = await asyncio.to_thread(tail.read_new)
        if lines:
            events = parse_stream(lines)
            for event in events:
                if not event.is_error:
                    continue
                info = parse_traceback(event.full_text)
                signature = (info.get("error_type") or "?", f"{info.get('file')}:{info.get('function')}")
                if signature in handled:
                    log_event("self_healing", f"duplicate {signature[0]} — already handled this session",
                              SEV_INFO)
                    continue
                handled.add(signature)
                await asyncio.to_thread(repair_from_event, event)
        if once:
            return
        await asyncio.sleep(POLL_INTERVAL_S)


def _notify(title: str, message: str, kind: str, fields: dict | None = None) -> None:
    try:
        from cardinal.modules import notifier

        color = {"danger": notifier.COLOR_DANGER, "success": notifier.COLOR_SUCCESS,
                 "warning": notifier.COLOR_WARNING}.get(kind, notifier.COLOR_INFO)
        notifier.notify_sync(title, message, color, fields)
    except Exception:
        pass
