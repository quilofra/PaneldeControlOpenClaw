"""Backend package for PaneldeContolOpenClaw.

This package contains modules implementing the non‑GUI logic of the application.  In
particular it holds:

* A simple proxy server (`proxy.py`) that enforces the selected model/provider
  and forwards requests to the real AI API while capturing metrics.
* A log manager (`log_manager.py`) responsible for writing execution logs
  to disk and pruning them when they exceed a configured size limit.
* A permissions handler (`permissions.py`) that manages which shell
  commands OpenClaw is allowed to invoke and whether sudo is permitted.
* A lightweight database layer (`db.py`) that stores run metadata and
  metrics in SQLite.

These modules are used by the main application to coordinate background
services and share state.
"""

from .proxy import ProxyServer  # noqa: F401
from .log_manager import LogManager  # noqa: F401
from .permissions import Permissions  # noqa: F401
from .db import Database  # noqa: F401
