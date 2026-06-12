"""Cardinal live dashboard entry point.

  python dashboard.py        serve on CARDINAL_DASHBOARD_PORT (default 8000)
"""
from __future__ import annotations

import uvicorn

from cardinal.core.config import get_config


def main() -> None:
    cfg = get_config()
    print(f"[CARDINAL] dashboard: http://localhost:{cfg.dashboard_port}")
    uvicorn.run("cardinal.dashboard.app:app", host="127.0.0.1",
                port=cfg.dashboard_port, log_level="warning")


if __name__ == "__main__":
    main()
