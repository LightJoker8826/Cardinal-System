"""Discord notification layer.

Posts clean embeds (not plain text) to a configured webhook when significant
Cardinal events occur:

  - bug detected / patched
  - weapon quarantined or rebalanced
  - new quest generated and installed
  - player triggered Incarnate Mode
  - Gini coefficient crossed a danger threshold
  - Sub-Process rejected a change
  - community sentiment triggered a soft balance change
  - spend-guard warnings / lockout

Reads DISCORD_WEBHOOK_URL from .env and FAILS SILENTLY if not configured —
a missing webhook can never crash any Cardinal module.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from cardinal.core.config import SEV_DEBUG, get_config, log_event

# Embed colors by event class
COLOR_INFO = 0x3498DB      # blue
COLOR_SUCCESS = 0x2ECC71   # green
COLOR_WARNING = 0xF1C40F   # yellow
COLOR_DANGER = 0xE74C3C    # red
COLOR_PURPLE = 0x9B59B6    # incarnate / ghost events


def _build_embed(title: str, description: str, color: int,
                 fields: dict[str, Any] | None = None) -> dict:
    embed: dict[str, Any] = {
        "title": title,
        "description": description[:3900],
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Cardinal System"},
    }
    if fields:
        embed["fields"] = [
            {"name": str(k)[:250], "value": str(v)[:1000] or "—", "inline": True}
            for k, v in fields.items()
        ]
    return embed


async def notify(title: str, description: str, color: int = COLOR_INFO,
                 fields: dict[str, Any] | None = None) -> bool:
    """Async embed post. Returns True on success; never raises."""
    cfg = get_config()
    if not cfg.discord_webhook_url:
        return False  # fail silently — not configured
    payload = {"username": "Cardinal", "embeds": [_build_embed(title, description, color, fields)]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(cfg.discord_webhook_url, json=payload)
            ok = resp.status_code in (200, 204)
            log_event("notifier", f"discord post {'ok' if ok else resp.status_code}", SEV_DEBUG)
            return ok
    except Exception:
        return False  # network failure must never propagate


def notify_sync(title: str, description: str, color: int = COLOR_INFO,
                fields: dict[str, Any] | None = None) -> bool:
    """Bridge for synchronous contexts. Never raises, never blocks a running loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    try:
        if loop is not None:
            loop.create_task(notify(title, description, color, fields))
            return True
        return asyncio.run(notify(title, description, color, fields))
    except Exception:
        return False
