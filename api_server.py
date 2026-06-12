"""Cardinal REST API entry point.

Requires CARDINAL_API_TOKEN in .env — the server refuses to start without
it (an unauthenticated write-capable API would be a security hole).

  python api_server.py            serve on CARDINAL_API_PORT (default 8001)
"""
from __future__ import annotations

import sys

import uvicorn

from cardinal.core.config import get_config


def main() -> None:
    cfg = get_config()
    if not cfg.api_token:
        print("[CARDINAL] FATAL: CARDINAL_API_TOKEN is not set in .env — "
              "the REST API refuses to serve unauthenticated. Set a token and retry.")
        sys.exit(1)
    uvicorn.run("cardinal.api.app:app", host="127.0.0.1", port=cfg.api_port, log_level="warning")


if __name__ == "__main__":
    main()
