"""Cardinal configuration + Ghost Protocol verbosity mask.

All environment-derived settings flow through this module. No other module
reads os.environ for Cardinal settings directly, and NOTHING sensitive is
ever hardcoded anywhere in the codebase.

Ghost Protocol — verbosity_mask levels (CARDINAL_VERBOSITY, default 1):
  0  Ghost : completely silent; only CRITICAL alerts fire
             (MHCP critical intervention, spend cap warning/lockout,
              gate rollback failure, crash).
  1  Admin : standard operational logging for manual troubleshooting.
  2  Debug : full verbose logging for development.

NOTE: the game's server.log event stream is Cardinal's *input data*
(the healer reads it) — it is never masked. The mask governs
operator-facing terminal output only.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

# --- Verbosity levels --------------------------------------------------------
GHOST = 0
ADMIN = 1
DEBUG = 2

# --- Severity levels for log_event ------------------------------------------
SEV_DEBUG = "debug"        # shown at verbosity 2
SEV_INFO = "info"          # shown at verbosity >= 1
SEV_WARNING = "warning"    # shown at verbosity >= 1
SEV_CRITICAL = "critical"  # ALWAYS shown, even in Ghost mode

_SEVERITY_STYLE = {
    SEV_DEBUG: "dim",
    SEV_INFO: "cyan",
    SEV_WARNING: "yellow",
    SEV_CRITICAL: "bold red",
}

console = Console()


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is not None and value.strip() == "":
        value = None
    return value if value is not None else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Config:
    """Snapshot of all Cardinal settings. Reload-safe (re-reads env)."""

    def __init__(self) -> None:
        self.project_root = PROJECT_ROOT
        self.db_path = PROJECT_ROOT / "cardinal.db"
        self.server_log = PROJECT_ROOT / "server.log"
        self.data_dir = PROJECT_ROOT / "data"
        self.backups_dir = PROJECT_ROOT / "backups"

        # Ghost Protocol
        self.verbosity_mask = _env_int("CARDINAL_VERBOSITY", ADMIN)

        # L3 Evolver
        self.anthropic_api_key = _env("ANTHROPIC_API_KEY")
        self.model = _env("CARDINAL_MODEL", "claude-fable-5")
        self.use_mock = _env_bool("CARDINAL_USE_MOCK", False)

        # Spend guard
        self.max_daily_spend_usd = _env_float("MAX_DAILY_SPEND_USD", 5.0)
        self.price_input_per_mtok = _env_float("CARDINAL_PRICE_INPUT_PER_MTOK", 10.0)
        self.price_output_per_mtok = _env_float("CARDINAL_PRICE_OUTPUT_PER_MTOK", 50.0)
        self.price_cache_read_per_mtok = _env_float("CARDINAL_PRICE_CACHE_READ_PER_MTOK", 1.0)

        # Dashboard / API
        self.dashboard_port = _env_int("CARDINAL_DASHBOARD_PORT", 8000)
        self.api_token = _env("CARDINAL_API_TOKEN")
        self.api_port = _env_int("CARDINAL_API_PORT", 8001)

        # Discord
        self.discord_webhook_url = _env("DISCORD_WEBHOOK_URL")

        # Reddit sentiment
        self.reddit_client_id = _env("REDDIT_CLIENT_ID")
        self.reddit_client_secret = _env("REDDIT_CLIENT_SECRET")
        self.reddit_user_agent = _env("REDDIT_USER_AGENT")
        self.subreddits = [
            s.strip() for s in (_env("CARDINAL_SUBREDDITS", "gamedev,roguelikes") or "").split(",") if s.strip()
        ]
        self.sentiment_interval_hours = _env_float("CARDINAL_SENTIMENT_INTERVAL_HOURS", 6.0)

        # Biometrics / MHCP
        self.biometric_port = _env_int("CARDINAL_BIOMETRIC_PORT", 8765)
        self.mhcp_interaction_permitted = _env_bool("MHCP_INTERACTION_PERMITTED", True)

    @property
    def reddit_configured(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret and self.reddit_user_agent)


_config: Config | None = None


def get_config(refresh: bool = False) -> Config:
    global _config
    if _config is None or refresh:
        _config = Config()
    return _config


def log_event(module: str, message: str, severity: str = SEV_INFO) -> None:
    """The single operator-facing logging facade. All modules route here.

    Respects the Ghost Protocol verbosity mask:
      Ghost (0): only SEV_CRITICAL is emitted.
      Admin (1): info/warning/critical.
      Debug (2): everything.
    """
    level = get_config().verbosity_mask
    if severity == SEV_CRITICAL:
        pass  # always emitted
    elif level <= GHOST:
        return
    elif severity == SEV_DEBUG and level < DEBUG:
        return
    style = _SEVERITY_STYLE.get(severity, "white")
    console.print(f"[{style}]\\[CARDINAL][/{style}] [dim]{module}[/dim] {message}")
