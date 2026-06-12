"""Adapter for the Python RPG sandbox (game.py).

Demonstrates the integration contract: the game polls this adapter at safe
tick boundaries; the adapter applies sequenced control commands (item
reloads, module hot-reload requests, Incarnate Mode, SEC updates, state
syncs) to the running game. An external engine would implement the exact
same surface over HTTP via cardinal/api — Cardinal cannot tell the
difference.
"""
from __future__ import annotations

import time
from typing import Any

from cardinal.adapters.base import GameAdapter
from cardinal.core.config import SEV_INFO, log_event


class PythonRPGAdapter(GameAdapter):
    target = "game"

    def __init__(self) -> None:
        super().__init__()
        self.reload_module_requested = False
        self._incarnate: dict[str, float] = {}  # player -> until_epoch

    # ------------------------------------------------------------------ #
    def apply_control(self, command: str, payload: dict[str, Any]) -> None:
        import game  # local import to avoid circulars at module load

        if command == "reload_items":
            game.load_items(refresh=True)
            log_event("adapter.rpg", "items.json reloaded into live game", SEV_INFO)
        elif command == "reload_module":
            # The healer patched code on disk. In-process reload happens at a
            # safe boundary owned by the supervisor (sim_runner) between games.
            self.reload_module_requested = True
            log_event("adapter.rpg", f"module reload requested: {payload.get('file')}", SEV_INFO)
        elif command == "incarnate":
            duration = float(payload.get("duration_s", 10.0))
            self._incarnate[payload.get("player", "*")] = time.time() + duration
        elif command == "sec_updated":
            pass  # sec module reads blend state from DB on each spawn — no cache to bust
        elif command == "quest_installed":
            log_event("adapter.rpg", f"quest installed: {payload.get('title')}", SEV_INFO)
        elif command == "world_changed":
            log_event("adapter.rpg", f"world state changed: {payload.get('key')}", SEV_INFO)
        elif command == "state_sync":
            game.load_items(refresh=True)

    def apply_state_snapshot(self, snapshot: dict[str, Any]) -> None:
        import game

        game.load_items(refresh=True)
        log_event("adapter.rpg", "authoritative state snapshot applied", SEV_INFO)

    # ------------------------------------------------------------------ #
    def consume_incarnate(self, player) -> bool:
        """Apply any pending Incarnate Mode grant to this player.
        Returns True if Incarnate Mode was newly activated."""
        until = self._incarnate.pop(player.name, None) or self._incarnate.pop("*", None)
        if until and until > time.time():
            player.incarnate_until = until
            return True
        return False

    def report_bot_live(self, player, floor: int, state: str = "running") -> None:
        """Push live floor/weapon/hp to the dashboard without ending a game."""
        from cardinal.core import db

        weapon = player.weapon.get("name") if player.weapon else None
        db.update_bot_live_status(
            player.name,
            state,
            hp=max(0, player.hp),
            gold=player.gold,
            floor=floor,
            weapon=weapon,
        )
