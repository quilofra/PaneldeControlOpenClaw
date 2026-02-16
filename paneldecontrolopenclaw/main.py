"""Entry point for the PaneldeContolOpenClaw application.

This script initialises backend services (database, log manager,
permissions, proxy server) and starts the Tkinter GUI.  When
executed it reads the configuration file located alongside the
repository to determine the initial provider and model.  Changes to
these settings made through the UI are persisted back to that
configuration file and applied immediately.

The proxy server runs on a background thread listening on
localhost:5005 by default.  OpenClaw should be configured to use this
address as its API endpoint instead of directly calling the AI
provider.  The GUI will show live logs and statistics from runs
handled by the proxy.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from pathlib import Path

import tkinter as tk

from backend import Database, LogManager, Permissions, ProxyServer
from gui.main_window import MainWindow


def main() -> None:
    # Determine paths
    base_dir = Path(__file__).resolve().parent
    config_path = base_dir / "config.json"
    # Read config
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    # Set up backend components
    log_dir = base_dir / cfg.get("log_dir", "logs")
    max_size_mb = cfg.get("max_log_size_mb", 1024)
    compress_days = cfg.get("log_compress_days")
    log_manager = LogManager(str(log_dir), max_size_mb, compress_days)
    db_path = base_dir / cfg.get("database", "runs.db")
    db = Database(str(db_path))
    # Backup database on startup (copy to timestamped file).  You can
    # comment this out if backups are not needed.
    try:
        db.backup()
    except Exception:
        pass
    permissions = Permissions(str(config_path))

    def get_config() -> dict:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    # The update callback is called when the UI writes config; we don't need
    # to do anything besides reload config in the proxy on next request
    def update_config() -> None:
        # This could trigger actions such as refreshing provider tokens, but
        # for now the proxy reads config on each request.
        return

    # Create integration helper (before starting proxy)
    from backend.integration import IntegrationHelper
    # Use proxy port from configuration if provided; default to 5005
    proxy_port = cfg.get("proxy_port", 5005)
    integration_helper = IntegrationHelper(db, proxy_port, service_name="openclaw")
    # Start proxy server on background thread
    proxy = ProxyServer("127.0.0.1", proxy_port, get_config, log_manager, db)
    proxy.start()

    # Launch GUI
    # Attempt to use PySide6 (Qt) for a modern UI; fall back to Tkinter if unavailable
    use_qt = False
    try:
        import importlib.util
        if importlib.util.find_spec("PySide6") is not None:
            use_qt = True
    except Exception:
        use_qt = False

    if use_qt:
        # Start the Qt application
        from PySide6.QtWidgets import QApplication
        from gui.qt_main_window import QtMainWindow  # type: ignore
        app_qt = QApplication(sys.argv)
        # Provide resources directory for icons
        resources_dir = base_dir / "resources"
        # Launch Qt main window
        window = QtMainWindow(
            str(config_path),
            db,
            log_manager,
            permissions,
            get_config,
            update_config,
            integration_helper=integration_helper,
            resources_dir=str(resources_dir),
        )
        window.show()
        # Execute Qt application event loop
        app_qt.exec()
        # On exit stop proxy
        proxy.shutdown()
        return

    # Fall back to Tkinter UI if Qt not available
    root = tk.Tk()
    # Set window icon if available. Use the OpenClaw mascot stored in resources.
    icon_path = base_dir / "resources" / "openclaw_icon.png"
    if icon_path.exists():
        try:
            # Use PhotoImage for Tkinter icons. Keep a reference to avoid garbage collection.
            icon_img = tk.PhotoImage(file=str(icon_path))
            root.iconphoto(True, icon_img)
            # Stash icon on root so it's not collected
            root._icon_img = icon_img  # type: ignore[attr-defined]
        except Exception:
            pass
    app = MainWindow(
        root,
        str(config_path),
        db,
        log_manager,
        permissions,
        get_config,
        update_config,
        integration_helper=integration_helper,
        icon_path=str(icon_path) if icon_path.exists() else None,
    )

    def on_close() -> None:
        # Stop proxy and exit
        proxy.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
