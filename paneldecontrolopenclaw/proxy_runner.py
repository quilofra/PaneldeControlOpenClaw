"""Standalone runner for the PaneldeContolOpenClaw proxy.

This script can be invoked directly or from a systemd service to run
the proxy component of PaneldeContolOpenClaw without launching the
graphical user interface.  It reads configuration and initialises
supporting classes (database, log manager, permissions) before
starting the proxy.  The proxy listens on the port configured in
``config.json`` under ``proxy_port`` (default 5005).

Usage::

    python proxy_runner.py

The script runs indefinitely until interrupted.  It gracefully
terminates the proxy server on SIGINT/SIGTERM.
"""

from __future__ import annotations

import json
import signal
import sys
from pathlib import Path

from backend.db import Database
from backend.log_manager import LogManager
from backend.permissions import Permissions
from backend.proxy import ProxyServer


def main() -> None:
    # Load configuration
    cfg_path = Path(__file__).resolve().parent / "config.json"
    if not cfg_path.exists():
        print("config.json not found; cannot start proxy", file=sys.stderr)
        sys.exit(1)
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Determine proxy port (default 5005)
    proxy_port = cfg.get("proxy_port", 5005)
    # Setup database
    db_path = Path(cfg.get("database", "runs.db"))
    db = Database(str(db_path))
    # Perform a backup of the database on startup.  This will
    # create a timestamped copy in the same directory.  If you
    # wish to disable backups, comment out the following line.
    try:
        db.backup()
    except Exception:
        pass
    # Setup log manager
    log_dir = cfg.get("log_dir", "logs")
    max_mb = cfg.get("max_log_size_mb", 1024)
    compress_days = cfg.get("log_compress_days", 7)
    log_manager = LogManager(log_dir=log_dir, max_size_mb=max_mb, compress_days=compress_days)
    # Permissions manager
    perms = Permissions(str(cfg_path))
    # Function to load config at runtime
    def get_config() -> dict:
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    # Start proxy server
    proxy = ProxyServer("127.0.0.1", proxy_port, get_config, log_manager, db)
    proxy.start()
    # Setup signal handlers to stop the proxy
    def shutdown(*_args) -> None:
        proxy.shutdown()
        sys.exit(0)
    for s in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(s, shutdown)
    # Wait for the proxy thread to exit
    proxy.join()


if __name__ == "__main__":
    main()