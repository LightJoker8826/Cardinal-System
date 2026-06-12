"""GameAdapter — the seam between Cardinal and any game engine.

A game integrates with Cardinal by (any combination of):
  1. Writing events in the normalized log schema (cardinal.core.events)
  2. Exposing player/world stats into cardinal.db
  3. Consuming the sequenced control channel for live commands
     (module hot-reload, Incarnate Mode, SEC updates, state sync)

Temporal Drift Compensation lives HERE so every connected engine —
file-based or REST — inherits the same ordering guarantee:
updates are applied only if sequence_id == last_applied + 1; anything
out-of-order or stale is discarded and triggers a State Refresh Request,
answered from Cardinal's authoritative state (latest approved version).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from cardinal.core import db
from cardinal.core.config import SEV_DEBUG, SEV_WARNING, log_event


class GameAdapter(ABC):
    """Base adapter. Subclasses implement how commands apply to their engine."""

    target = "game"

    def __init__(self) -> None:
        # Start from the channel head: an adapter joining late should not
        # replay the entire historical command stream.
        self.last_applied_seq = db.latest_sequence(self.target)

    # ------------------------------------------------------------------ #
    # Engine-specific surface
    # ------------------------------------------------------------------ #
    @abstractmethod
    def apply_control(self, command: str, payload: dict[str, Any]) -> None:
        """Apply a single in-order control command to the running game."""

    @abstractmethod
    def apply_state_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Resync the game from Cardinal's authoritative state snapshot."""

    # ------------------------------------------------------------------ #
    # Temporal Drift Compensation
    # ------------------------------------------------------------------ #
    def poll_control(self) -> int:
        """Drain pending control messages in strict sequence order.

        Returns the number of commands applied. On a sequence gap (drift),
        discards the stale batch, fires a State Refresh Request, applies the
        authoritative snapshot, and fast-forwards the cursor.
        """
        pending = db.pending_control(self.last_applied_seq, self.target)
        if not pending:
            return 0

        applied = 0
        for row in pending:
            seq = row["sequence_id"]
            if seq != self.last_applied_seq + 1:
                # Drift: out-of-order or stale update — discard and resync.
                log_event(
                    "adapter",
                    f"sequence drift (have {self.last_applied_seq}, got {seq}) — "
                    "discarding and firing State Refresh Request",
                    SEV_WARNING,
                )
                self.request_state_refresh()
                return applied
            self.apply_control(row["command"], row["payload"])
            self.last_applied_seq = seq
            applied += 1
            log_event("adapter", f"applied control #{seq} {row['command']}", SEV_DEBUG)

        db.ack_control(self.last_applied_seq, self.target)
        return applied

    def request_state_refresh(self) -> None:
        """Resync from the Sub-Process's authoritative state and fast-forward."""
        from cardinal.sub_process import build_state_snapshot

        snapshot = build_state_snapshot()
        self.apply_state_snapshot(snapshot)
        head = db.latest_sequence(self.target)
        db.ack_control(head, self.target)
        self.last_applied_seq = head
        log_event("adapter", f"state refreshed; sequence fast-forwarded to {head}", SEV_WARNING)
