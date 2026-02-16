"""Integration helpers for connecting OpenClaw to PaneldeContolOpenClaw.

This module provides utilities to detect how OpenClaw is deployed (as a
system service, docker container or manual process), determine
whether it is currently connected to the local proxy, and generate
instructions for configuring OpenClaw to point at the proxy.  It
relies on heuristics such as the presence of a systemd unit named
"openclaw.service" and recent activity recorded in the database.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional


class IntegrationHelper:
    """Helper for detecting and guiding integration with OpenClaw."""

    def __init__(self, db, proxy_port: int, service_name: str = "openclaw") -> None:
        self.db = db
        self.proxy_port = proxy_port
        self.service_name = service_name

    def is_service_active(self) -> Optional[bool]:
        """Return True if a systemd service is active, False if inactive, None if undetermined."""
        try:
            result = subprocess.run(
                ["systemctl", "is-active", f"{self.service_name}.service"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            state = result.stdout.strip()
            if state == "active":
                return True
            elif state == "inactive" or state == "failed" or state == "unknown":
                return False
            else:
                return None
        except FileNotFoundError:
            # systemctl not available (e.g. container without systemd)
            return None
        except Exception:
            return None

    def has_recent_runs(self, within_seconds: int = 60) -> bool:
        """Return True if there has been a run in the last `within_seconds` seconds."""
        now = time.time()
        runs = self.db.get_recent_runs(limit=10)
        for r in runs:
            if r["start_time"] >= now - within_seconds:
                return True
        return False

    def get_integration_instructions(self, provider: str, port: int) -> str:
        """Return textual instructions on how to configure OpenClaw to use the local proxy.

        The returned string contains recommended steps for both systemd-managed deployments
        (using a drop-in override) and manual launches.  The provided ``provider`` is not
        currently used but reserved for future use when per-provider configuration may differ.
        The ``port`` is interpolated into the endpoint URL.
        """
        return (
            "Para que OpenClaw utilice el proxy local, debes apuntar su cliente de API al "
            "proxy y especificar la clave API del proveedor.\n\n"
            "**Opción A: Servicio systemd**\n"
            "Si OpenClaw se ejecuta como un servicio systemd, crea o edita un fichero de override\n"
            "utilizando el comando:\n\n"
            "    sudo systemctl edit {svc}.service\n\n"
            "En el editor, añade:\n\n"
            "    [Service]\n"
            "    Environment=OPENAI_BASE_URL=http://127.0.0.1:{port}\n"
            "    Environment=OPENAI_API_KEY=<tu_clave>\n\n"
            "Guarda, recarga y reinicia el servicio:\n\n"
            "    sudo systemctl daemon-reload\n"
            "    sudo systemctl restart {svc}.service\n\n"
            "**Opción B: Ejecución manual**\n"
            "Si ejecutas OpenClaw manualmente, exporta estas variables antes de lanzarlo:\n\n"
            "    export OPENAI_BASE_URL=http://127.0.0.1:{port}\n"
            "    export OPENAI_API_KEY=<tu_clave>\n"
            "    ./openclaw ...\n\n"
            "Reemplaza `<tu_clave>` por la clave real del proveedor.\n"
        ).format(port=port, svc=self.service_name)

    def generate_dropin_override(self, api_key_placeholder: str = "<tu_clave>") -> str:
        """Generate the contents of a systemd override file for OpenClaw.

        This helper constructs a minimal override section containing the environment
        variables needed to route OpenClaw through the local proxy.  The caller can
        write this content into a drop-in file named ``{service_name}_override.conf`` or
        use ``systemctl edit``.  The ``api_key_placeholder`` indicates the string
        inserted for the API key.  The proxy port used is ``self.proxy_port``.
        """
        content = (
            "[Service]\n"
            f"Environment=OPENAI_BASE_URL=http://127.0.0.1:{self.proxy_port}\n"
            f"Environment=OPENAI_API_KEY={api_key_placeholder}\n"
        )
        return content

    def write_override_file(self, directory: Path, api_key_placeholder: str = "<tu_clave>") -> Path:
        """Write a drop-in override file in the given directory.

        The file will be named ``{service_name}_override.conf`` and contain the environment
        variables required to connect OpenClaw to the proxy.  This function returns the
        path of the file written.  It will overwrite any existing file of the same name.

        Parameters
        ----------
        directory : Path
            Directory in which to create the override file.  Must exist and be writeable.
        api_key_placeholder : str, optional
            Placeholder for the API key value; defaults to ``"<tu_clave>"``.
        """
        content = self.generate_dropin_override(api_key_placeholder)
        override_path = directory / f"{self.service_name}_override.conf"
        with open(override_path, "w", encoding="utf-8") as f:
            f.write(content)
        return override_path

    def apply_override_to_systemd(self, override_path: Path) -> tuple[bool, str]:
        """Attempt to install the given override file into systemd and restart the service.

        This method copies the override file to `/etc/systemd/system/{service_name}.service.d/override.conf`
        using `sudo` and then reloads systemd and restarts the service.  It returns a tuple
        `(success, message)` where `success` indicates whether the operations succeeded and
        `message` contains a human readable description of the outcome.

        Note: This requires the user to have sudo privileges without a password prompt.  If
        any of the steps fail, the process stops and an error message is returned.
        """
        dropin_dir = Path("/etc/systemd/system") / f"{self.service_name}.service.d"
        try:
            cmds = [
                ["sudo", "mkdir", "-p", str(dropin_dir)],
                ["sudo", "cp", str(override_path), str(dropin_dir / "override.conf")],
                ["sudo", "systemctl", "daemon-reload"],
                ["sudo", "systemctl", "restart", f"{self.service_name}.service"],
            ]
            for cmd in cmds:
                result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if result.returncode != 0:
                    return False, f"Error ejecutando {' '.join(cmd)}: {result.stderr.strip()}"
            return True, f"Override instalado en {dropin_dir}/override.conf y servicio reiniciado"
        except Exception as exc:
            return False, str(exc)
