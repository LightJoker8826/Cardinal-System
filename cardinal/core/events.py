"""Normalized game event schema + parser.

This is the adapter seam for log-based game integration. Any game that can
write lines in this schema can be managed by Cardinal:

    [TIMESTAMP] [LEVEL] [MODULE] message

    [2026-06-11 14:32:01] [CARDINAL_ERROR] [game.combat] ZeroDivisionError...
    [2026-06-11 14:32:05] [INFO] [game.combat] Player dealt 47 dmg to Goblin

CARDINAL_ERROR entries may be followed by traceback continuation lines
(lines not matching the schema are treated as continuations of the
previous entry).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

LINE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+\[(?P<level>[A-Z_]+)\]\s+\[(?P<module>[^\]]+)\]\s+(?P<message>.*)$"
)

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class GameEvent:
    timestamp: str
    level: str
    module: str
    message: str
    continuation: list[str] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n".join([self.message, *self.continuation])

    @property
    def is_error(self) -> bool:
        return self.level == "CARDINAL_ERROR"


def format_line(level: str, module: str, message: str, ts: datetime | None = None) -> str:
    stamp = (ts or datetime.now()).strftime(TS_FORMAT)
    return f"[{stamp}] [{level}] [{module}] {message}"


def parse_line(line: str) -> GameEvent | None:
    m = LINE_RE.match(line.rstrip("\n"))
    if not m:
        return None
    return GameEvent(
        timestamp=m.group("ts"),
        level=m.group("level"),
        module=m.group("module"),
        message=m.group("message"),
    )


def parse_stream(lines: list[str]) -> list[GameEvent]:
    """Parse lines into events, folding unmatched lines (tracebacks) into the
    previous event as continuations."""
    events: list[GameEvent] = []
    for raw in lines:
        if not raw.strip():
            continue
        event = parse_line(raw)
        if event is not None:
            events.append(event)
        elif events:
            events[-1].continuation.append(raw.rstrip("\n"))
    return events


TRACEBACK_FILE_RE = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')
EXC_TYPE_RE = re.compile(r"^(?P<etype>[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Interrupt|Warning))\b")


def parse_traceback(text: str) -> dict:
    """Extract filename, line number, function and exception type from a
    Python traceback (innermost frame wins)."""
    file, line, func = None, None, None
    for m in TRACEBACK_FILE_RE.finditer(text):
        file, line, func = m.group("file"), int(m.group("line")), m.group("func")
    etype = None
    for raw in reversed(text.splitlines()):
        m = EXC_TYPE_RE.match(raw.strip())
        if m:
            etype = m.group("etype")
            break
    return {"file": file, "line": line, "function": func, "error_type": etype}
