"""Main Tkinter window for PaneldeContolOpenClaw.

The `MainWindow` class assembles the application UI and binds
interactions to backend services such as the log manager, database,
permissions and proxy server.  The interface is organised into
multiple tabs: a live view for current executions, a history view
listing past runs, a permissions editor for managing allowed
commands, and a stats dashboard showing basic metrics.  The user can
select the active provider and model from drop‑down lists and apply
changes which are persisted to the configuration file and take effect
immediately.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Dict, List, Optional

import tkinter as tk
from tkinter import messagebox
from tkinter import ttk


def _human_bytes(n: int) -> str:
    """Return a human readable representation of a byte count."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024.0:
            return f"{n:3.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


class MainWindow:
    """Top level GUI for the control panel."""

    def __init__(
        self,
        root: tk.Tk,
        config_path: str,
        db,
        log_manager,
        permissions,
        get_config: callable,
        update_config_callback: callable,
        integration_helper=None,
        icon_path: Optional[str] = None,
    ) -> None:
        self.root = root
        self.root.title("PaneldeContolOpenClaw")
        self.config_path = Path(config_path)
        self.db = db
        self.log_manager = log_manager
        self.permissions = permissions
        self.get_config = get_config
        self.update_config_callback = update_config_callback
        self.integration_helper = integration_helper
        self.icon_path = icon_path
        # Load current config to populate UI
        self._load_config()
        # Build UI
        self._create_widgets()
        # Periodic updates for stats and history
        self._schedule_periodic_updates()

    def _load_config(self) -> None:
        """Load configuration from file into local variables."""
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.current_provider = cfg.get("provider", "openai")
            self.current_model = cfg.get("model", "")
            # Predefine provider->models map (could be fetched from API)
            self.available_providers = ["openai", "anthropic", "gemini"]
            self.available_models_map = {
                "openai": [
                    "gpt-3.5-turbo",
                    "gpt-3.5-turbo-0125",
                    "gpt-3.5-turbo-1106",
                    "gpt-4",
                    "gpt-4-turbo",
                    "text-embedding-ada-002"
                ],
                "anthropic": ["claude-3-sonnet", "claude-3-haiku", "claude-3-opus"],
                "gemini": ["gemini-pro", "gemini-vision-pro"]
            }
        else:
            self.current_provider = "openai"
            self.current_model = "gpt-3.5-turbo"
            self.available_providers = ["openai"]
            self.available_models_map = {"openai": [self.current_model]}

    def _create_widgets(self) -> None:
        """Create all widgets in the main window."""
        # Configure dark theme styles
        style = ttk.Style()
        try:
            # Use clam theme for better styling flexibility
            style.theme_use('clam')
        except Exception:
            pass
        dark_bg = '#2b2b2b'
        dark_fg = '#f2f2f2'
        accent_bg = '#3c3f41'
        # Configure default colors for frames and labels
        style.configure('.', background=dark_bg, foreground=dark_fg)
        style.configure('TFrame', background=dark_bg)
        style.configure('TLabel', background=dark_bg, foreground=dark_fg)
        style.configure('TButton', background=accent_bg, foreground=dark_fg)
        style.configure('TNotebook', background=dark_bg)
        style.configure('TNotebook.Tab', background=accent_bg, foreground=dark_fg)
        style.map('TNotebook.Tab', background=[('selected', '#5c5c5c')], foreground=[('selected', dark_fg)])
        # Set root background
        self.root.configure(bg=dark_bg)

        # Header with icon and title
        header = ttk.Frame(self.root, padding=(10, 5))
        header.pack(fill=tk.X)
        if self.icon_path:
            try:
                img = tk.PhotoImage(file=self.icon_path)
                # Resize image to 48px height for header if larger
                # Determine subsample factor
                h = img.height()
                factor = max(h // 48, 1)
                img_small = img.subsample(factor, factor)
                # Keep references to avoid GC
                self._header_img_orig = img
                self._header_img = img_small
                icon_label = ttk.Label(header, image=self._header_img)
                icon_label.pack(side=tk.LEFT, padx=(0, 8))
            except Exception:
                pass
        title_label = ttk.Label(header, text='Panel de Control OpenClaw', font=('Arial', 16, 'bold'))
        title_label.pack(side=tk.LEFT)

        # Provider/model selection frame
        top_frame = ttk.Frame(self.root, padding=10)
        top_frame.pack(fill=tk.X)
        ttk.Label(top_frame, text="Proveedor:").pack(side=tk.LEFT)
        self.provider_var = tk.StringVar(value=self.current_provider)
        self.provider_combo = ttk.Combobox(
            top_frame, textvariable=self.provider_var, values=self.available_providers, state="readonly"
        )
        self.provider_combo.pack(side=tk.LEFT, padx=5)
        self.provider_combo.bind("<<ComboboxSelected>>", self._on_provider_changed)

        ttk.Label(top_frame, text="Modelo:").pack(side=tk.LEFT, padx=(15, 0))
        self.model_var = tk.StringVar(value=self.current_model)
        # Use list of models for current provider
        self.model_combo = ttk.Combobox(
            top_frame,
            textvariable=self.model_var,
            values=self.available_models_map.get(self.current_provider, []),
            state="readonly",
            width=30,
        )
        self.model_combo.pack(side=tk.LEFT, padx=5)

        self.apply_button = ttk.Button(top_frame, text="Aplicar", command=self._apply_provider_model)
        self.apply_button.pack(side=tk.LEFT, padx=10)

        # Notebook for tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Live tab
        self.live_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.live_frame, text="En vivo")
        self._build_live_tab(self.live_frame)

        # History tab
        self.history_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.history_frame, text="Historial")
        self._build_history_tab(self.history_frame)

        # Permissions tab
        self.permissions_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.permissions_frame, text="Permisos")
        self._build_permissions_tab(self.permissions_frame)

        # Stats tab
        self.stats_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.stats_frame, text="Estadísticas")
        self._build_stats_tab(self.stats_frame)

        # Integration tab, if helper provided
        if self.integration_helper is not None:
            self.integration_frame = ttk.Frame(self.notebook)
            self.notebook.add(self.integration_frame, text="Integración")
            self._build_integration_tab(self.integration_frame)

    # Provider/model selection handlers
    def _on_provider_changed(self, event: tk.Event) -> None:
        """Update model list when provider selection changes."""
        provider = self.provider_var.get()
        models = self.available_models_map.get(provider, [])
        self.model_combo["values"] = models
        # If current model not in list, reset to first
        if self.model_var.get() not in models:
            self.model_var.set(models[0] if models else "")

    def _apply_provider_model(self) -> None:
        """Persist selected provider and model to config and apply changes."""
        provider = self.provider_var.get()
        model = self.model_var.get()
        # Write to config
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["provider"] = provider
            cfg["model"] = model
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            # update runtime config via callback
            self.update_config_callback()
            messagebox.showinfo("Configuración aplicada", f"Proveedor: {provider}\nModelo: {model}")
        except Exception as exc:
            messagebox.showerror("Error", f"No se pudo aplicar la configuración: {exc}")

    # Live tab
    def _build_live_tab(self, frame: ttk.Frame) -> None:
        # Controls: refresh button
        controls_frame = ttk.Frame(frame, padding=5)
        controls_frame.pack(fill=tk.X)
        ttk.Button(controls_frame, text="Actualizar", command=self._refresh_live_log).pack(side=tk.LEFT)
        self.live_log_text = tk.Text(frame, wrap=tk.NONE, height=20, bg='#1e1e1e', fg='#f2f2f2', insertbackground='#f2f2f2')
        # Add scrollbars
        yscroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.live_log_text.yview)
        xscroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.live_log_text.xview)
        self.live_log_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        xscroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.live_log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # Label for current run id and status
        self.live_run_label = ttk.Label(controls_frame, text="No hay ejecución en curso")
        self.live_run_label.pack(side=tk.LEFT, padx=10)

    def _refresh_live_log(self) -> None:
        """Refresh the live log view with the most recent run's log."""
        runs = self.db.get_recent_runs(limit=1)
        if not runs:
            self.live_run_label.config(text="No hay ejecuciones recientes")
            self.live_log_text.delete("1.0", tk.END)
            return
        run = runs[0]
        run_id = run["id"]
        status = run["status"]
        self.live_run_label.config(text=f"Run {run_id[:8]}... estado: {status}")
        # Read log file if exists
        log_file = run["log_file"]
        try:
            data = self._read_log_file(log_file)
            self.live_log_text.delete("1.0", tk.END)
            self.live_log_text.insert(tk.END, data)
        except Exception:
            self.live_log_text.delete("1.0", tk.END)
            self.live_log_text.insert(tk.END, "No se pudo leer el archivo de log")

    # History tab
    def _build_history_tab(self, frame: ttk.Frame) -> None:
        controls_frame = ttk.Frame(frame, padding=5)
        controls_frame.pack(fill=tk.X)
        ttk.Button(controls_frame, text="Actualizar", command=self._refresh_history).pack(side=tk.LEFT)
        self.show_all_var = tk.IntVar(value=0)
        ttk.Checkbutton(
            controls_frame,
            text="Mostrar todo",
            variable=self.show_all_var,
            command=self._refresh_history,
        ).pack(side=tk.LEFT, padx=10)
        ttk.Button(controls_frame, text="Exportar CSV", command=self._export_history_csv).pack(side=tk.LEFT, padx=5)

        self.history_tree = ttk.Treeview(frame, columns=("start_time", "provider", "model", "status"), show="headings")
        for col, text in [
            ("start_time", "Inicio"),
            ("provider", "Proveedor"),
            ("model", "Modelo"),
            ("status", "Estado"),
        ]:
            self.history_tree.heading(col, text=text)
            self.history_tree.column(col, width=120, stretch=True)
        # Vertical scrollbar
        vscroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.history_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # Bind double click to open log
        self.history_tree.bind("<Double-1>", self._on_history_double_click)

    def _export_history_csv(self) -> None:
        """Export the current history view to a CSV file."""
        import csv
        if self.show_all_var.get():
            runs = self.db.get_all_runs()
        else:
            runs = self.db.get_recent_runs(limit=100)
        export_path = Path(self.config_path).parent / "history_export.csv"
        try:
            with open(export_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["id", "start_time", "provider", "model", "status", "log_file"])
                for r in runs:
                    dt = datetime.fromtimestamp(r["start_time"]).strftime("%Y-%m-%d %H:%M:%S")
                    writer.writerow([
                        r["id"],
                        dt,
                        r["provider"],
                        r["model"],
                        r["status"],
                        r["log_file"],
                    ])
            messagebox.showinfo("Exportación", f"Historial exportado a {export_path}")
        except Exception as exc:
            messagebox.showerror("Error", f"No se pudo exportar: {exc}")

    def _refresh_history(self) -> None:
        """Reload the history list from the database."""
        self.history_tree.delete(*self.history_tree.get_children())
        if self.show_all_var.get():
            runs = self.db.get_all_runs()
        else:
            runs = self.db.get_recent_runs(limit=100)
        for row in runs:
            # Convert start_time to readable format
            ts = row["start_time"]
            dt = datetime.fromtimestamp(ts)
            ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            self.history_tree.insert(
                "",
                tk.END,
                iid=row["id"],
                values=(ts_str, row["provider"], row["model"], row["status"]),
            )

    def _on_history_double_click(self, event) -> None:
        """Open the log for the selected run in a new window."""
        item_id = self.history_tree.focus()
        if not item_id:
            return
        run = self.db.get_run(item_id)
        if not run:
            return
        log_path = run["log_file"]
        try:
            data = self._read_log_file(log_path)
        except Exception:
            data = "No se pudo leer el log"
        # Create new window
        win = tk.Toplevel(self.root)
        win.title(f"Log de {item_id[:8]}")
        text = tk.Text(win, wrap=tk.NONE, bg='#1e1e1e', fg='#f2f2f2', insertbackground='#f2f2f2')
        text.pack(fill=tk.BOTH, expand=True)
        text.insert(tk.END, data)
        # Add scrollbars
        yscroll = ttk.Scrollbar(win, orient=tk.VERTICAL, command=text.yview)
        xscroll = ttk.Scrollbar(win, orient=tk.HORIZONTAL, command=text.xview)
        text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        xscroll.pack(side=tk.BOTTOM, fill=tk.X)

    # Permissions tab
    def _build_permissions_tab(self, frame: ttk.Frame) -> None:
        sudo_frame = ttk.Frame(frame, padding=5)
        sudo_frame.pack(fill=tk.X)
        self.sudo_var = tk.IntVar(value=1 if self.permissions.is_sudo_allowed() else 0)
        self.sudo_check = ttk.Checkbutton(
            sudo_frame,
            text="Permitir sudo",
            variable=self.sudo_var,
            command=self._toggle_sudo,
        )
        self.sudo_check.pack(side=tk.LEFT)

        # Allowed commands list and controls
        cmds_frame = ttk.Frame(frame, padding=5)
        cmds_frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(cmds_frame, text="Comandos permitidos:").pack(anchor=tk.W)
        self.cmds_listbox = tk.Listbox(cmds_frame, height=10, bg='#1e1e1e', fg='#f2f2f2')
        self.cmds_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # Scrollbar
        cmds_scroll = ttk.Scrollbar(cmds_frame, orient=tk.VERTICAL, command=self.cmds_listbox.yview)
        self.cmds_listbox.configure(yscrollcommand=cmds_scroll.set)
        cmds_scroll.pack(side=tk.LEFT, fill=tk.Y)
        # Buttons to add/remove
        buttons_frame = ttk.Frame(cmds_frame)
        buttons_frame.pack(side=tk.LEFT, padx=10)
        self.add_cmd_entry = ttk.Entry(buttons_frame, width=20)
        self.add_cmd_entry.pack(pady=2)
        ttk.Button(buttons_frame, text="Añadir", command=self._add_allowed_command).pack(pady=2)
        ttk.Button(buttons_frame, text="Eliminar", command=self._remove_selected_command).pack(pady=2)
        self._load_allowed_commands()

    def _load_allowed_commands(self) -> None:
        """Populate the listbox with currently allowed commands."""
        self.cmds_listbox.delete(0, tk.END)
        for cmd in self.permissions.get_allowlist():
            self.cmds_listbox.insert(tk.END, cmd)

    def _toggle_sudo(self) -> None:
        """Enable or disable sudo permission."""
        allowed = bool(self.sudo_var.get())
        self.permissions.set_sudo(allowed)
        messagebox.showinfo("Permisos", f"sudo {'habilitado' if allowed else 'deshabilitado'}")

    def _add_allowed_command(self) -> None:
        cmd = self.add_cmd_entry.get().strip()
        if not cmd:
            return
        self.permissions.add_command(cmd)
        self.add_cmd_entry.delete(0, tk.END)
        self._load_allowed_commands()

    def _remove_selected_command(self) -> None:
        selection = self.cmds_listbox.curselection()
        if not selection:
            return
        index = selection[0]
        cmd = self.cmds_listbox.get(index)
        self.permissions.remove_command(cmd)
        self._load_allowed_commands()

    # Stats tab
    def _build_stats_tab(self, frame: ttk.Frame) -> None:
        # We'll populate stats into labels and update periodically
        self.stats_vars: Dict[str, tk.StringVar] = {}
        labels = [
            ("runs_total", "Ejecuciones totales:"),
            ("runs_last_24h", "Ejecuciones últimas 24h:"),
            ("errors_total", "Errores totales:"),
            ("avg_duration", "Duración media (s):"),
            ("tokens_total", "Tokens totales:"),
            ("log_usage", "Tamaño de logs:"),
            ("top_logs", "Logs más pesados:"),
        ]
        for key, text in labels:
            row = ttk.Frame(frame, padding=5)
            row.pack(fill=tk.X)
            ttk.Label(row, text=text).pack(side=tk.LEFT)
            var = tk.StringVar(value="-")
            self.stats_vars[key] = var
            ttk.Label(row, textvariable=var).pack(side=tk.LEFT)

    def _refresh_stats(self) -> None:
        """Calculate and update statistics on the stats tab."""
        # Total runs
        all_runs = self.db.get_all_runs()
        self.stats_vars["runs_total"].set(str(len(all_runs)))
        # Runs last 24h
        now = time.time()
        runs_24h = [r for r in all_runs if r["start_time"] > now - 86400]
        self.stats_vars["runs_last_24h"].set(str(len(runs_24h)))
        # Errors
        errors = [r for r in all_runs if r["status"] != "success"]
        self.stats_vars["errors_total"].set(str(len(errors)))
        # Average duration
        durations = [r["end_time"] - r["start_time"] for r in all_runs if r["end_time"] and r["start_time"]]
        if durations:
            avg = sum(durations) / len(durations)
            self.stats_vars["avg_duration"].set(f"{avg:.2f}")
        else:
            self.stats_vars["avg_duration"].set("-")
        # Tokens total (in + out)
        tokens = 0
        for r in all_runs:
            if r["tokens_in"]:
                tokens += r["tokens_in"]
            if r["tokens_out"]:
                tokens += r["tokens_out"]
        self.stats_vars["tokens_total"].set(str(tokens) if tokens else "-")
        # Log usage
        stats = self.log_manager.get_stats()
        total_bytes = stats.get("total_bytes", 0)
        limit_bytes = self.log_manager.max_bytes
        self.stats_vars["log_usage"].set(f"{_human_bytes(total_bytes)} / {_human_bytes(limit_bytes)}")

        # Top logs by size
        top_files = self.log_manager.get_top_files(3)
        if top_files:
            top_strs = [f"{name} ({_human_bytes(size)})" for name, size in top_files]
            self.stats_vars["top_logs"].set(", ".join(top_strs))
        else:
            self.stats_vars["top_logs"].set("-")

    # Integration tab
    def _build_integration_tab(self, frame: ttk.Frame) -> None:
        """Build the integration tab where connection status and instructions are shown."""
        # Status labels
        status_frame = ttk.Frame(frame, padding=5)
        status_frame.pack(fill=tk.X)
        ttk.Label(status_frame, text="Servicio OpenClaw:").pack(side=tk.LEFT)
        self.service_status_var = tk.StringVar(value="desconocido")
        ttk.Label(status_frame, textvariable=self.service_status_var).pack(side=tk.LEFT)
        ttk.Label(status_frame, text="Conexión al proxy:").pack(side=tk.LEFT, padx=(20, 0))
        self.connection_status_var = tk.StringVar(value="desconocido")
        ttk.Label(status_frame, textvariable=self.connection_status_var).pack(side=tk.LEFT)
        ttk.Button(status_frame, text="Comprobar", command=self._update_integration_status).pack(side=tk.LEFT, padx=10)

        # Instructions toggle
        instr_button = ttk.Button(frame, text="Mostrar instrucciones", command=self._toggle_instructions)
        instr_button.pack(pady=5)
        self.instructions_visible = False
        self.instructions_text = tk.Text(frame, height=12, wrap=tk.WORD, bg='#1e1e1e', fg='#f2f2f2', insertbackground='#f2f2f2')
        self.instructions_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.instructions_text.insert(tk.END, self.integration_helper.get_integration_instructions(self.current_provider, self.integration_helper.proxy_port))
        self.instructions_text.configure(state=tk.DISABLED)
        self.instructions_text.pack_forget()  # hidden by default
        # Immediately update status
        self._update_integration_status()

    def _update_integration_status(self) -> None:
        """Update labels showing whether the OpenClaw service is active and connected."""
        helper = self.integration_helper
        if helper is None:
            return
        # Service status
        svc = helper.is_service_active()
        if svc is True:
            self.service_status_var.set("activo")
        elif svc is False:
            self.service_status_var.set("inactivo")
        else:
            self.service_status_var.set("no disponible")
        # Connection status (recent runs)
        connected = helper.has_recent_runs(120)
        self.connection_status_var.set("conectado" if connected else "desconectado")

    def _toggle_instructions(self) -> None:
        """Show or hide the integration instructions text."""
        if self.instructions_visible:
            self.instructions_text.pack_forget()
            self.instructions_visible = False
        else:
            self.instructions_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
            self.instructions_visible = True

    # Helper to read plain or compressed log files
    def _read_log_file(self, path: str) -> str:
        """Return the contents of a log file, decompressing if gzipped."""
        if not path:
            return ""
        if path.endswith(".gz"):
            import gzip
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                return f.read()
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()

    # Periodic update scheduling
    def _schedule_periodic_updates(self) -> None:
        def update():
            self._refresh_stats()
            self._refresh_history()
            self._refresh_live_log()
            # Reschedule after 5 seconds
            self.root.after(5000, update)
        self.root.after(5000, update)
