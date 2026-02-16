"""Qt-based main window for PaneldeContolOpenClaw.

This module implements the graphical user interface using PySide6 (Qt).
It provides a more modern, minimal aesthetic compared to the Tkinter
implementation and supports switching between dark and light themes as
well as selecting between different icon variants.  The GUI is
organised into tabs similar to the Tkinter version: live view,
history, permissions, statistics, integration and settings.  All
backend interactions are delegated to the existing classes (Database,
LogManager, Permissions, IntegrationHelper).

To run this interface you need the PySide6 package installed.  If
PySide6 is not available the application will fall back to the
Tkinter UI defined in ``main_window.py``.  PySide6 can be installed
with ``pip install pyside6``.

The main entry class here is :class:`QtMainWindow` which can be
instantiated from ``main.py``.  It expects the same parameters as
``MainWindow`` with additional optional parameters for the icon
directory.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Dict

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QFont, QPalette, QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QSplitter,
    QHeaderView,
)

# Import the simple in-process event bus.  This allows the UI to
# subscribe to events emitted by the proxy in real time.  If the
# import fails (for example when running outside of the packaged
# environment), event streaming will simply be disabled and the UI
# will fall back to periodic refreshes.
try:
    from ..backend import event_bus  # type: ignore
except Exception:
    event_bus = None  # type: ignore

# Import QtCharts for simple graphs
try:
    from PySide6.QtCharts import QChart, QChartView, QLineSeries, QDateTimeAxis, QValueAxis
    from PySide6.QtCore import QDateTime
    _QTCHARTS_AVAILABLE = True
except Exception:
    # Charts unavailable if QtCharts is not present
    _QTCHARTS_AVAILABLE = False

import base64

# Import encryption helpers
try:
    from ..backend import crypto_utils  # type: ignore
except Exception:
    crypto_utils = None  # type: ignore


def _human_bytes(n: int) -> str:
    """Return a human readable representation of a byte count."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024.0:
            return f"{n:3.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


class QtMainWindow(QMainWindow):
    """Main application window using Qt widgets."""

    def __init__(
        self,
        config_path: str,
        db,
        log_manager,
        permissions,
        get_config: Callable[[], dict],
        update_config_callback: Callable[[], None],
        integration_helper=None,
        resources_dir: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.config_path = Path(config_path)
        self.db = db
        self.log_manager = log_manager
        self.permissions = permissions
        self.get_config = get_config
        self.update_config_callback = update_config_callback
        self.integration_helper = integration_helper
        self.resources_dir = Path(resources_dir or Path(config_path).parent / "resources")
        # Load configuration (provider, model, theme, icon variant)
        self._load_config()
        # Apply theme and icon
        self._apply_theme(self.theme)
        self._apply_window_icon()
        # Build UI
        self._init_ui()
        # Schedule periodic updates
        self._setup_timers()
        # If the in-process event bus is available, subscribe to live
        # events.  The event queue will be drained regularly by a
        # timer to update the timeline in real time.  We set
        # ``self.event_queue`` to None when the event bus is not
        # present so that downstream logic can short-circuit.  Also
        # initialise internal variables used to compute relative
        # timestamps for live events (run ID and start timestamp).
        if event_bus is not None:
            try:
                self.event_queue = event_bus.subscribe()
            except Exception:
                self.event_queue = None
        else:
            self.event_queue = None
        # Live run tracking for streaming: maintain the current run ID
        # and the timestamp of the first event in that run so that
        # relative times can be computed on the fly when processing
        # incremental events.  These values are updated whenever the
        # live view refreshes.
        self._live_run_id: Optional[str] = None
        self._live_start_ts: Optional[float] = None

    def _load_config(self) -> None:
        """Load user configuration from JSON file."""
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.provider = cfg.get("provider", "openai")
            self.model = cfg.get("model", "")
            self.theme = cfg.get("theme", "dark")
            self.icon_variant = cfg.get("icon_variant", "full")
            # Predefine models; this could be dynamic via API
            # Available providers come from configuration; fall back to defaults
            providers_cfg = cfg.get("providers", {}) if isinstance(cfg.get("providers"), dict) else {}
            if providers_cfg:
                self.available_providers = list(providers_cfg.keys())
            else:
                self.available_providers = ["openai"]
            # Models mapping.  If a provider has no predefined models,
            # fallback to an empty list; these can be customised later.
            self.available_models_map = {
                "openai": [
                    "gpt-3.5-turbo",
                    "gpt-3.5-turbo-0125",
                    "gpt-3.5-turbo-1106",
                    "gpt-4",
                    "gpt-4-turbo",
                    "text-embedding-ada-002",
                ],
                "anthropic": ["claude-3-sonnet", "claude-3-haiku", "claude-3-opus"],
                "gemini": ["gemini-pro", "gemini-vision-pro"],
            }
        else:
            self.provider = "openai"
            self.model = "gpt-3.5-turbo"
            self.theme = "dark"
            self.icon_variant = "full"
            self.available_providers = ["openai"]
            self.available_models_map = {"openai": [self.model]}

    # Helper functions for encoding/decoding API keys
    def _encode_key(self, key: str) -> str:
        """Encrypt a key using the configured encryption key.

        This method delegates to :mod:`crypto_utils` if available.  If
        encryption helpers are unavailable, falls back to base64
        encoding prefixed with ``"ENC:"``.  A falsy input returns an
        empty string.
        """
        if not key:
            return ""
        # Use crypto_utils if available
        if crypto_utils is not None:
            try:
                return crypto_utils.encrypt_value(key, str(self.config_path))
            except Exception:
                pass
        # Fallback: base64 encode
        try:
            encoded = base64.b64encode(key.encode("utf-8")).decode("utf-8")
            return "ENC:" + encoded
        except Exception:
            return key

    def _decode_key(self, key: str) -> str:
        """Decode a key previously encrypted or encoded.

        If :mod:`crypto_utils` is available and the key is prefixed with
        ``"ENC:"``, it will be decrypted using the encryption key from
        configuration.  If decryption fails or crypto utilities are
        unavailable, a base64 decode fallback is used.  Non-encrypted
        values are returned unchanged.
        """
        if not key:
            return ""
        if not isinstance(key, str):
            return key
        if key.startswith("ENC:"):
            # Try cryptography-based decryption first
            if crypto_utils is not None:
                try:
                    dec = crypto_utils.decrypt_value(key, str(self.config_path))
                    if dec:
                        return dec
                except Exception:
                    pass
            # Fallback: base64 decode
            try:
                data = base64.b64decode(key[4:])
                return data.decode("utf-8")
            except Exception:
                return ""
        return key

    def _apply_theme(self, theme: str) -> None:
        """Apply a light or dark colour palette to the application."""
        app = QApplication.instance()
        palette = QPalette()
        if theme == "dark":
            # Dark colours
            palette.setColor(QPalette.Window, QColor(43, 43, 43))
            palette.setColor(QPalette.WindowText, QColor(242, 242, 242))
            palette.setColor(QPalette.Base, QColor(30, 30, 30))
            palette.setColor(QPalette.AlternateBase, QColor(38, 38, 38))
            palette.setColor(QPalette.ToolTipBase, QColor(255, 255, 255))
            palette.setColor(QPalette.ToolTipText, QColor(0, 0, 0))
            palette.setColor(QPalette.Text, QColor(242, 242, 242))
            palette.setColor(QPalette.Button, QColor(60, 63, 65))
            palette.setColor(QPalette.ButtonText, QColor(242, 242, 242))
            palette.setColor(QPalette.BrightText, QColor(255, 0, 0))
            palette.setColor(QPalette.Highlight, QColor(90, 80, 160))
            palette.setColor(QPalette.HighlightedText, QColor(242, 242, 242))
        else:
            # Light colours
            palette.setColor(QPalette.Window, QColor(250, 250, 250))
            palette.setColor(QPalette.WindowText, QColor(0, 0, 0))
            palette.setColor(QPalette.Base, QColor(255, 255, 255))
            palette.setColor(QPalette.AlternateBase, QColor(242, 242, 242))
            palette.setColor(QPalette.ToolTipBase, QColor(255, 255, 220))
            palette.setColor(QPalette.ToolTipText, QColor(0, 0, 0))
            palette.setColor(QPalette.Text, QColor(0, 0, 0))
            palette.setColor(QPalette.Button, QColor(240, 240, 240))
            palette.setColor(QPalette.ButtonText, QColor(0, 0, 0))
            palette.setColor(QPalette.BrightText, QColor(255, 0, 0))
            palette.setColor(QPalette.Highlight, QColor(30, 144, 255))
            palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
        app.setPalette(palette)
        self.theme = theme

    def _apply_window_icon(self) -> None:
        """Set the window icon based on the selected variant."""
        variant = self.icon_variant or "full"
        icon_name = "icon_full.png" if variant == "full" else "icon_simple.png"
        icon_path = self.resources_dir / icon_name
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

    def _save_config(self) -> None:
        """Persist current configuration to disk."""
        if not self.config_path.exists():
            return
        with open(self.config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["provider"] = self.provider
        cfg["model"] = self.model
        cfg["theme"] = self.theme
        cfg["icon_variant"] = self.icon_variant
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        # Notify other components if needed
        self.update_config_callback()

    # UI construction
    def _init_ui(self) -> None:
        """Construct the user interface.

        The layout is organised into a sidebar for navigation and a main area
        containing a top bar with provider/model selectors and the page
        content.  The sidebar uses minimalist buttons with icons and
        descriptive labels.  The top bar summarises the active provider,
        model and displays a status indicator for the last run.  Pages
        themselves correspond to the previous tabs (En vivo, Historial,
        Permisos, Estadísticas, Integración y Ajustes).
        """
        self.setWindowTitle("Panel de Control OpenClaw")
        # Accent colour for highlighting (red tone)
        self.accent_color = QColor(211, 47, 47)  # material red 700
        # Central widget with horizontal layout (sidebar + main)
        central = QWidget()
        self.setCentralWidget(central)
        h_layout = QHBoxLayout(central)
        h_layout.setContentsMargins(0, 0, 0, 0)
        h_layout.setSpacing(0)
        # Sidebar
        self.nav_container = QWidget()
        # Fixed width for sidebar to ensure consistent appearance
        self.nav_container.setFixedWidth(180)
        nav_layout = QVBoxLayout(self.nav_container)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(0)
        self.nav_buttons = []
        # Helper to create nav buttons
        def add_nav_button(text: str, icon_name: Optional[str] = None) -> QPushButton:
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton {padding: 12px 16px; border: none; text-align: left; color: %s;} "
                "QPushButton:checked {background-color: %s; color: white;}" % (
                    self.palette().color(QPalette.ButtonText).name(), self.accent_color.name()
                )
            )
            nav_layout.addWidget(btn)
            self.nav_buttons.append(btn)
            return btn
        # Create nav buttons (order matches stack pages)
        self.live_btn = add_nav_button("En vivo")
        self.history_btn = add_nav_button("Historial")
        self.permissions_btn = add_nav_button("Permisos")
        self.stats_btn = add_nav_button("Estadísticas")
        if self.integration_helper is not None:
            self.integration_btn = add_nav_button("Integración")
        else:
            self.integration_btn = None
        self.settings_btn = add_nav_button("Ajustes")
        # Add stretch to push items to top
        nav_layout.addStretch()
        # Main area
        self.main_container = QWidget()
        v_layout = QVBoxLayout(self.main_container)
        v_layout.setContentsMargins(16, 16, 16, 16)
        v_layout.setSpacing(12)
        # Top bar (provider/model selectors + status)
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)
        provider_label = QLabel("Proveedor:")
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(self.available_providers)
        idx = self.available_providers.index(self.provider) if self.provider in self.available_providers else 0
        self.provider_combo.setCurrentIndex(idx)
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        model_label = QLabel("Modelo:")
        self.model_combo = QComboBox()
        self._update_model_combo()
        if self.model:
            try:
                m_idx = self.model_combo.findText(self.model)
                if m_idx >= 0:
                    self.model_combo.setCurrentIndex(m_idx)
            except Exception:
                pass
        apply_btn = QPushButton("Aplicar")
        apply_btn.clicked.connect(self._apply_provider_model)
        # Status indicator (placeholder; updated in live refresh)
        self.status_indicator = QLabel()
        self._update_status_indicator(None)
        top_bar.addWidget(provider_label)
        top_bar.addWidget(self.provider_combo)
        top_bar.addSpacing(8)
        top_bar.addWidget(model_label)
        top_bar.addWidget(self.model_combo)
        top_bar.addSpacing(8)
        top_bar.addWidget(apply_btn)
        top_bar.addStretch()
        top_bar.addWidget(self.status_indicator)
        v_layout.addLayout(top_bar)
        # Stacked pages
        self.stack = QStackedWidget()
        v_layout.addWidget(self.stack, 1)
        # Build pages and add to stack
        # Live page
        self.live_widget = QWidget()
        self._build_live_tab()
        self.stack.addWidget(self.live_widget)
        # History page
        self.history_widget = QWidget()
        self._build_history_tab()
        self.stack.addWidget(self.history_widget)
        # Permissions page
        self.permissions_widget = QWidget()
        self._build_permissions_tab()
        self.stack.addWidget(self.permissions_widget)
        # Stats page
        self.stats_widget = QWidget()
        self._build_stats_tab()
        self.stack.addWidget(self.stats_widget)
        # Integration page (optional)
        if self.integration_helper is not None:
            self.integration_widget = QWidget()
            self._build_integration_tab()
            self.stack.addWidget(self.integration_widget)
        # Settings page
        self.settings_widget = QWidget()
        self._build_settings_tab()
        self.stack.addWidget(self.settings_widget)
        # Add nav and main area to main layout
        h_layout.addWidget(self.nav_container)
        h_layout.addWidget(self.main_container, 1)
        # Connect nav buttons to change page
        # Connect nav buttons to switch pages. Use enumerate to capture index
        for i, btn in enumerate(self.nav_buttons):
            btn.clicked.connect(lambda checked, idx=i: self._switch_page(idx))
        # Default page is the first one
        if self.nav_buttons:
            self.nav_buttons[0].setChecked(True)
            self.stack.setCurrentIndex(0)
        # Global font size for comfortable reading
        central.setStyleSheet("QWidget { font-size: 13px; }")

    def _switch_page(self, index: int) -> None:
        """Switch to the given page index in the stacked widget."""
        self.stack.setCurrentIndex(index)
        # Ensure the corresponding nav button is checked
        for i, btn in enumerate(self.nav_buttons):
            btn.setChecked(i == index)

    # Helpers to build each tab
    def _build_live_tab(self) -> None:
        layout = QVBoxLayout(self.live_widget)
        # Top controls bar
        controls = QHBoxLayout()
        self.live_refresh_btn = QPushButton("Actualizar")
        self.live_refresh_btn.clicked.connect(self._refresh_live_log)
        self.live_run_label = QLabel("No hay ejecución en curso")
        controls.addWidget(self.live_refresh_btn)
        controls.addSpacing(10)
        controls.addWidget(self.live_run_label)
        controls.addStretch()
        layout.addLayout(controls)
        # Split log and timeline horizontally
        split_layout = QHBoxLayout()
        # Log area (left)
        self.live_log_text = QPlainTextEdit()
        self.live_log_text.setReadOnly(True)
        self.live_log_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        # Use monospace font for log
        font = QFont("Courier New")
        self.live_log_text.setFont(font)
        split_layout.addWidget(self.live_log_text, 2)
        # Timeline table (right)
        from PySide6.QtWidgets import QHeaderView
        self.events_table = QTableWidget()
        self.events_table.setColumnCount(3)
        self.events_table.setHorizontalHeaderLabels(["Tiempo", "Evento", "Detalles"])
        header = self.events_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.events_table.verticalHeader().setVisible(False)
        self.events_table.setEditTriggers(QTableWidget.NoEditTriggers)
        split_layout.addWidget(self.events_table, 1)
        layout.addLayout(split_layout, 1)

    def _build_history_tab(self) -> None:
        layout = QVBoxLayout(self.history_widget)
        # Top controls: refresh, show all, search/filter and export
        ctrl_layout = QHBoxLayout()
        self.history_refresh_btn = QPushButton("Actualizar")
        self.history_refresh_btn.clicked.connect(self._refresh_history)
        ctrl_layout.addWidget(self.history_refresh_btn)
        ctrl_layout.addSpacing(10)
        # Show all checkbox
        self.show_all_checkbox = QCheckBox("Mostrar todo")
        self.show_all_checkbox.stateChanged.connect(self._refresh_history)
        ctrl_layout.addWidget(self.show_all_checkbox)
        ctrl_layout.addSpacing(20)
        # Search box
        ctrl_layout.addWidget(QLabel("Buscar:"))
        self.history_search_input = QLineEdit()
        self.history_search_input.setPlaceholderText("Texto a buscar")
        self.history_search_input.textChanged.connect(self._refresh_history)
        ctrl_layout.addWidget(self.history_search_input)
        ctrl_layout.addSpacing(10)
        # Provider filter
        ctrl_layout.addWidget(QLabel("Proveedor:"))
        self.history_provider_filter = QComboBox()
        # Populate with available providers plus 'Todos'
        providers = list(self.available_providers)
        self.history_provider_filter.addItem("Todos")
        for p in providers:
            self.history_provider_filter.addItem(p)
        self.history_provider_filter.currentIndexChanged.connect(self._refresh_history)
        ctrl_layout.addWidget(self.history_provider_filter)
        ctrl_layout.addSpacing(10)
        # Status filter
        ctrl_layout.addWidget(QLabel("Estado:"))
        self.history_status_filter = QComboBox()
        self.history_status_filter.addItems(["Todos", "success", "error"])
        self.history_status_filter.currentIndexChanged.connect(self._refresh_history)
        ctrl_layout.addWidget(self.history_status_filter)
        ctrl_layout.addSpacing(20)
        # Export button
        self.export_btn = QPushButton("Exportar CSV")
        self.export_btn.clicked.connect(self._export_history_csv)
        ctrl_layout.addWidget(self.export_btn)
        ctrl_layout.addStretch()
        layout.addLayout(ctrl_layout)
        # Splitter to hold table and detail view
        splitter = QSplitter(Qt.Horizontal)
        # Left side: table container
        table_container = QWidget()
        table_layout = QVBoxLayout(table_container)
        table_layout.setContentsMargins(0, 0, 0, 0)
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(5)
        self.history_table.setHorizontalHeaderLabels([
            "Inicio",
            "Proveedor",
            "Modelo",
            "Estado",
            "Duración (s)"
        ])
        self.history_table.horizontalHeader().setStretchLastSection(True)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.history_table.setSelectionMode(QTableWidget.SingleSelection)
        # When selecting a row, update the detail panel
        self.history_table.selectionModel().selectionChanged.connect(self._open_selected_history_log)
        table_layout.addWidget(self.history_table)
        splitter.addWidget(table_container)
        # Right side: detail panel
        self.history_detail_widget = QWidget()
        detail_layout = QVBoxLayout(self.history_detail_widget)
        detail_layout.setContentsMargins(8, 0, 0, 0)
        # Summary label
        self.history_detail_summary = QLabel("Seleccione una ejecución para ver detalles")
        detail_layout.addWidget(self.history_detail_summary)
        # Timeline table for the run
        self.history_detail_events = QTableWidget()
        self.history_detail_events.setColumnCount(3)
        self.history_detail_events.setHorizontalHeaderLabels(["Tiempo", "Evento", "Detalles"])
        header = self.history_detail_events.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.history_detail_events.verticalHeader().setVisible(False)
        self.history_detail_events.setEditTriggers(QTableWidget.NoEditTriggers)
        detail_layout.addWidget(self.history_detail_events)
        # Log text for the run
        self.history_detail_log = QPlainTextEdit()
        self.history_detail_log.setReadOnly(True)
        self.history_detail_log.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = QFont("Courier New")
        self.history_detail_log.setFont(font)
        detail_layout.addWidget(self.history_detail_log, 1)
        # Action buttons for selected run
        action_layout = QHBoxLayout()
        # Replay button: plays back the run timeline
        self.replay_run_btn = QPushButton("Reproducir")
        self.replay_run_btn.clicked.connect(self._replay_selected_run)
        action_layout.addWidget(self.replay_run_btn)
        # Export button: exports run to ZIP
        self.export_run_btn = QPushButton("Exportar ZIP")
        self.export_run_btn.clicked.connect(self._export_selected_run)
        action_layout.addWidget(self.export_run_btn)
        action_layout.addStretch()
        detail_layout.addLayout(action_layout)
        splitter.addWidget(self.history_detail_widget)
        # Set reasonable initial sizes
        splitter.setSizes([400, 400])
        layout.addWidget(splitter, 1)

    def _build_permissions_tab(self) -> None:
        layout = QVBoxLayout(self.permissions_widget)
        # Sudo toggle
        self.sudo_checkbox = QCheckBox("Permitir sudo")
        self.sudo_checkbox.setChecked(self.permissions.is_sudo_allowed())
        self.sudo_checkbox.stateChanged.connect(self._toggle_sudo)
        layout.addWidget(self.sudo_checkbox)
        # Allowlist management
        cmd_layout = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("Comandos permitidos:"))
        self.command_list = QListWidget()
        # Populate
        for cmd in self.permissions.get_allowlist():
            item = QListWidgetItem(cmd)
            self.command_list.addItem(item)
        left.addWidget(self.command_list, 1)
        cmd_layout.addLayout(left, 3)
        # Right: add/remove
        right = QVBoxLayout()
        self.cmd_input = QLineEdit()
        self.cmd_input.setPlaceholderText("Nuevo comando")
        add_btn = QPushButton("Añadir")
        add_btn.clicked.connect(self._add_allowed_command)
        remove_btn = QPushButton("Eliminar seleccionado")
        remove_btn.clicked.connect(self._remove_selected_command)
        right.addWidget(self.cmd_input)
        right.addWidget(add_btn)
        right.addWidget(remove_btn)
        right.addStretch()
        cmd_layout.addLayout(right, 1)
        layout.addLayout(cmd_layout, 1)

    def _build_stats_tab(self) -> None:
        layout = QVBoxLayout(self.stats_widget)
        # Create labels for metrics
        self.stat_labels = {}
        metrics = [
            ("runs_total", "Ejecuciones totales:"),
            ("runs_24h", "Ejecuciones últimas 24h:"),
            ("errors_total", "Errores totales:"),
            ("avg_duration", "Duración media (s):"),
            ("tokens_total", "Tokens totales:"),
            ("log_usage", "Tamaño de logs:"),
            ("top_logs", "Logs más pesados:"),
            ("service_status", "Servicio:"),
            ("proxy_status", "Proxy:"),
            ("provider_status", "Proveedor:"),
            ("internet_status", "Internet:"),
        ]
        for key, label in metrics:
            h = QHBoxLayout()
            h.addWidget(QLabel(label))
            lbl = QLabel("-")
            self.stat_labels[key] = lbl
            h.addWidget(lbl)
            h.addStretch()
            layout.addLayout(h)
        # Denied commands list and control
        layout.addSpacing(20)
        denied_label = QLabel("Comandos denegados recientes:")
        layout.addWidget(denied_label)
        from PySide6.QtWidgets import QListWidget
        self.denied_list = QListWidget()
        layout.addWidget(self.denied_list, 1)
        # Button to allow selected denied command
        btn_layout = QHBoxLayout()
        self.allow_denied_btn = QPushButton("Permitir seleccionado")
        self.allow_denied_btn.clicked.connect(self._allow_selected_denied)
        btn_layout.addWidget(self.allow_denied_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        layout.addStretch()

        # If QtCharts is available, add a chart view for tokens per day
        if _QTCHARTS_AVAILABLE:
            self.chart_view = QChartView()
            # Enable anti-aliasing for smoother lines
            try:
                self.chart_view.setRenderHint(QPainter.Antialiasing)
            except Exception:
                pass
            layout.addSpacing(20)
            layout.addWidget(QLabel("Tokens por día (últimos 30 días):"))
            layout.addWidget(self.chart_view, 1)

    def _build_integration_tab(self) -> None:
        layout = QVBoxLayout(self.integration_widget)
        # Status row
        status_layout = QHBoxLayout()
        status_layout.addWidget(QLabel("Servicio OpenClaw:"))
        self.service_status_label = QLabel("desconocido")
        status_layout.addWidget(self.service_status_label)
        status_layout.addSpacing(20)
        status_layout.addWidget(QLabel("Conexión al proxy:"))
        self.connection_status_label = QLabel("desconocido")
        status_layout.addWidget(self.connection_status_label)
        check_btn = QPushButton("Comprobar")
        check_btn.clicked.connect(self._update_integration_status)
        status_layout.addSpacing(10)
        status_layout.addWidget(check_btn)
        status_layout.addStretch()
        layout.addLayout(status_layout)
        # Instructions toggle
        self.instructions_visible = False
        self.instructions_btn = QPushButton("Mostrar instrucciones")
        self.instructions_btn.clicked.connect(self._toggle_instructions)
        layout.addWidget(self.instructions_btn)
        # Generate override button
        if self.integration_helper is not None:
            self.generate_override_btn = QPushButton("Generar override systemd")
            self.generate_override_btn.clicked.connect(self._generate_override)
            layout.addWidget(self.generate_override_btn)
            # Button to apply override
            self.apply_override_btn = QPushButton("Aplicar override systemd")
            self.apply_override_btn.clicked.connect(self._apply_override_systemd)
            layout.addWidget(self.apply_override_btn)
        # Text area for instructions
        self.instructions_text = QTextEdit()
        self.instructions_text.setReadOnly(True)
        self.instructions_text.setVisible(False)
        # Load instructions text
        if self.integration_helper is not None:
            instructions = self.integration_helper.get_integration_instructions(self.provider, self.integration_helper.proxy_port)
            self.instructions_text.setPlainText(instructions)
        layout.addWidget(self.instructions_text)

    def _build_settings_tab(self) -> None:
        layout = QVBoxLayout(self.settings_widget)
        # Theme selection
        theme_layout = QHBoxLayout()
        theme_layout.addWidget(QLabel("Tema:"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["dark", "light"])
        # Set current theme
        idx = 0 if self.theme == "dark" else 1
        self.theme_combo.setCurrentIndex(idx)
        theme_layout.addWidget(self.theme_combo)
        layout.addLayout(theme_layout)
        # Icon selection
        icon_layout = QHBoxLayout()
        icon_layout.addWidget(QLabel("Icono:"))
        self.icon_combo = QComboBox()
        self.icon_combo.addItems(["full", "simple"])
        idx_icon = 0 if self.icon_variant == "full" else 1
        self.icon_combo.setCurrentIndex(idx_icon)
        icon_layout.addWidget(self.icon_combo)
        layout.addLayout(icon_layout)
        # API keys section
        layout.addSpacing(20)
        layout.addWidget(QLabel("Claves API por proveedor:"))
        self.provider_key_edits = {}
        for prov in self.available_providers:
            h = QHBoxLayout()
            h.addWidget(QLabel(prov))
            line = QLineEdit()
            line.setEchoMode(QLineEdit.Password)
            # Load existing key from config if present
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            key = cfg.get("providers", {}).get(prov, {}).get("api_key", "")
            # Decode encoded key if necessary
            if key:
                key = self._decode_key(key)
            except Exception:
                key = ""
            line.setText(key)
            self.provider_key_edits[prov] = line
            h.addWidget(line, 1)
            # Test button
            btn = QPushButton("Probar")
            # Need to capture prov in lambda
            btn.clicked.connect(lambda chk=False, p=prov: self._test_api_key(p))
            h.addWidget(btn)
            layout.addLayout(h)
        # Apply settings button
        apply_settings_btn = QPushButton("Aplicar ajustes")
        apply_settings_btn.clicked.connect(self._apply_settings)
        layout.addSpacing(10)
        layout.addWidget(apply_settings_btn)
        layout.addStretch()

    # Provider/model selection handlers
    def _provider_changed(self, idx: int) -> None:
        self.provider = self.available_providers[idx]
        self._update_model_combo()
        # Set default model for provider
        models = self.available_models_map.get(self.provider, [])
        if models:
            self.model_combo.setCurrentIndex(0)
            self.model = models[0]
        else:
            self.model_combo.clear()
            self.model = ""

    def _update_model_combo(self) -> None:
        models = self.available_models_map.get(self.provider, [])
        self.model_combo.clear()
        self.model_combo.addItems(models)

    def _apply_provider_model(self) -> None:
        # Update provider/model selection
        self.provider = self.provider_combo.currentText()
        self.model = self.model_combo.currentText()
        # Save configuration
        self._save_config()
        # Notify user via status bar
        self.statusBar().showMessage(f"Proveedor: {self.provider} / Modelo: {self.model} aplicado", 3000)

    def _update_status_indicator(self, status: Optional[str]) -> None:
        """Update the status indicator in the top bar based on the last run status.

        :param status: One of 'success', 'error' or None.  If None,
          displays a neutral indicator.  Success renders a green dot,
          error a red dot.  The indicator also shows a tooltip with the
          last run ID if available (set when refreshing live log).
        """
        if status == "success":
            colour = QColor(76, 175, 80)  # green
        elif status == "error":
            colour = QColor(211, 47, 47)  # red
        else:
            colour = QColor(189, 189, 189)  # grey
        # Create a small coloured circle using HTML in QLabel
        self.status_indicator.setText(
            f"<span style='display:inline-block; width:14px; height:14px; border-radius:7px; background:{colour.name()};'></span>"
        )

    # Live tab updates
    def _refresh_live_log(self) -> None:
        runs = self.db.get_recent_runs(limit=1)
        if not runs:
            self.live_run_label.setText("No hay ejecuciones recientes")
            self.live_log_text.setPlainText("")
            # Clear timeline as well
            self.events_table.setRowCount(0)
            return
        run = runs[0]
        run_id = run["id"]
        status = run["status"]
        # Compute latency metrics from events
        events = []
        try:
            events = self.db.get_events_for_run(run_id)
        except Exception:
            events = []
        request_sent_time = first_token_time = finish_time = None
        for ev in events:
            if ev["event"] == "request_sent" and request_sent_time is None:
                request_sent_time = ev["timestamp"]
            elif ev["event"] == "first_token" and first_token_time is None:
                first_token_time = ev["timestamp"]
            elif ev["event"] == "request_finished" and finish_time is None:
                finish_time = ev["timestamp"]
        ttft_str = duration_str = ""
        if request_sent_time and first_token_time:
            ttft = first_token_time - request_sent_time
            ttft_str = f", TTFT {ttft:.2f}s"
        if request_sent_time and finish_time:
            dur = finish_time - request_sent_time
            duration_str = f", Duración {dur:.2f}s"
        self.live_run_label.setText(
            f"Run {run_id[:8]}… estado: {status}{ttft_str}{duration_str}"
        )
        # Update status indicator in top bar
        self._update_status_indicator(status)
        self.status_indicator.setToolTip(f"Último run: {run_id[:8]}")
        log_file = run["log_file"]
        try:
            data = self._read_log_file(log_file)
            self.live_log_text.setPlainText(data)
        except Exception:
            self.live_log_text.setPlainText("No se pudo leer el archivo de log")
        # Refresh timeline events for this run
        self._refresh_live_events(run_id)

        # Update live run tracking information.  Compute the
        # approximate start timestamp based on the first event for
        # relative timing in streaming mode.  These will be used by
        # ``_process_event_queue`` to append new events on the fly.
        self._live_run_id = run_id
        # Determine start_ts from earliest event in this run
        start_ts = None
        if events:
            start_ts = events[0]["timestamp"]
        self._live_start_ts = start_ts

    def _refresh_live_events(self, run_id: str) -> None:
        """Populate the timeline table for the given run ID."""
        # Clear table
        self.events_table.setRowCount(0)
        events = []
        try:
            events = self.db.get_events_for_run(run_id)
        except Exception:
            return
        # Determine start time to compute relative times
        start_ts = None
        if events:
            start_ts = events[0]["timestamp"]
        for ev in events:
            row = self.events_table.rowCount()
            self.events_table.insertRow(row)
            ts = ev["timestamp"]
            # Compute relative time if possible
            if start_ts is not None:
                rel = ts - start_ts
                time_str = f"{rel:.2f}s"
            else:
                time_str = time.strftime("%H:%M:%S", time.localtime(ts))
            event_name = ev["event"]
            details = ev["details"] or ""
            self.events_table.setItem(row, 0, QTableWidgetItem(time_str))
            self.events_table.setItem(row, 1, QTableWidgetItem(event_name))
            self.events_table.setItem(row, 2, QTableWidgetItem(details))

    def _process_event_queue(self) -> None:
        """Drain the in-memory event queue and update the live timeline.

        This method is called by a high frequency timer (250 ms).  It
        consumes any pending events from the event bus and, if they
        belong to the currently displayed run, appends them to the
        timeline table with a relative timestamp.  This avoids
        reloading all events from the database and provides a fluid
        streaming experience.  If the start timestamp or run ID is
        unknown, the event will be ignored until the live view has
        been refreshed.
        """
        if self.event_queue is None:
            return
        # If no live run is currently being displayed, do nothing.
        run_id = self._live_run_id
        start_ts = self._live_start_ts
        if not run_id:
            # Drain queue but ignore events
            try:
                while True:
                    self.event_queue.get_nowait()
            except Exception:
                pass
            return
        # Process all events currently in the queue
        updated = False
        try:
            while True:
                evt = self.event_queue.get_nowait()
                if evt is None:
                    continue
                # Only process events for the current live run
                if evt.get("run_id") != run_id:
                    continue
                # Append row to events table
                ts = evt.get("timestamp")
                # Fallback: if timestamp is missing, use current time
                if ts is None:
                    ts = time.time()
                if start_ts is not None:
                    rel = ts - start_ts
                    time_str = f"{rel:.2f}s"
                else:
                    # Without a start timestamp we cannot compute a
                    # relative offset; use absolute time
                    time_str = time.strftime("%H:%M:%S", time.localtime(ts))
                event_name = evt.get("event", "")
                details = evt.get("details") or ""
                row_idx = self.events_table.rowCount()
                self.events_table.insertRow(row_idx)
                self.events_table.setItem(row_idx, 0, QTableWidgetItem(time_str))
                self.events_table.setItem(row_idx, 1, QTableWidgetItem(event_name))
                self.events_table.setItem(row_idx, 2, QTableWidgetItem(details))
                updated = True
        except Exception:
            pass
        # If we appended any events, optionally scroll to bottom to
        # ensure the newest entry is visible.  This can be tuned to
        # preserve manual scroll position if desired.
        if updated:
            self.events_table.scrollToBottom()

    # History updates
    def _refresh_history(self) -> None:
        # Clear table
        self.history_table.setRowCount(0)
        show_all = self.show_all_checkbox.isChecked()
        runs = self.db.get_all_runs() if show_all else self.db.get_recent_runs(limit=100)
        # Apply filters
        search_text = self.history_search_input.text().strip().lower() if hasattr(self, "history_search_input") else ""
        provider_filter = self.history_provider_filter.currentText() if hasattr(self, "history_provider_filter") else "Todos"
        status_filter = self.history_status_filter.currentText() if hasattr(self, "history_status_filter") else "Todos"
        filtered_runs = []
        for r in runs:
            # Search filter: match in id, provider, model, status or timestamp string
            ts_str = datetime.fromtimestamp(r["start_time"]).strftime("%Y-%m-%d %H:%M:%S")
            if search_text:
                text_target = " ".join([
                    r["id"],
                    r["provider"],
                    r["model"],
                    r["status"],
                    ts_str,
                ]).lower()
                if search_text not in text_target:
                    continue
            # Provider filter
            if provider_filter != "Todos" and r["provider"] != provider_filter:
                continue
            # Status filter
            if status_filter != "Todos" and r["status"] != status_filter:
                continue
            filtered_runs.append(r)
        for r in filtered_runs:
            row_idx = self.history_table.rowCount()
            self.history_table.insertRow(row_idx)
            ts = r["start_time"]
            dt = datetime.fromtimestamp(ts)
            ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            # Compute duration if available
            dur = "-"
            st = r.get("start_time")
            et = r.get("end_time")
            if st and et:
                dur_val = et - st
                if dur_val >= 0:
                    dur = f"{dur_val:.2f}"
            values = [ts_str, r["provider"], r["model"], r["status"], dur]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                self.history_table.setItem(row_idx, col, item)

    def _open_selected_history_log(self) -> None:
        # When a row is selected in the history table, update the detail view instead of opening a dialog.
        row = self.history_table.currentRow()
        if row < 0:
            # Clear detail if no selection
            self.history_detail_summary.setText("Seleccione una ejecución para ver detalles")
            self.history_detail_log.setPlainText("")
            self.history_detail_events.setRowCount(0)
            return
        # Reapply the same filters used in _refresh_history to map row index to the correct run
        show_all = self.show_all_checkbox.isChecked()
        runs = self.db.get_all_runs() if show_all else self.db.get_recent_runs(limit=100)
        search_text = self.history_search_input.text().strip().lower() if hasattr(self, "history_search_input") else ""
        provider_filter = self.history_provider_filter.currentText() if hasattr(self, "history_provider_filter") else "Todos"
        status_filter = self.history_status_filter.currentText() if hasattr(self, "history_status_filter") else "Todos"
        filtered = []
        for r in runs:
            ts_str = datetime.fromtimestamp(r["start_time"]).strftime("%Y-%m-%d %H:%M:%S")
            if search_text:
                target = " ".join([
                    r["id"],
                    r["provider"],
                    r["model"],
                    r["status"],
                    ts_str,
                ]).lower()
                if search_text not in target:
                    continue
            if provider_filter != "Todos" and r["provider"] != provider_filter:
                continue
            if status_filter != "Todos" and r["status"] != status_filter:
                continue
            filtered.append(r)
        if row >= len(filtered):
            # Should not happen, but clear detail
            self.history_detail_summary.setText("Seleccione una ejecución para ver detalles")
            self.history_detail_log.setPlainText("")
            self.history_detail_events.setRowCount(0)
            return
        run = filtered[row]
        run_id = run["id"]
        # Summary line: run ID, provider, model, status, tokens, durations
        summary_parts = [f"Run {run_id[:8]}…", f"Estado: {run['status']}"]
        # Duration
        if run.get("start_time") and run.get("end_time"):
            dur_val = run["end_time"] - run["start_time"]
            summary_parts.append(f"Duración: {dur_val:.2f}s")
        # Tokens info
        tokens_in = run.get("tokens_in") or 0
        tokens_out = run.get("tokens_out") or 0
        total_tokens = run.get("total_tokens") or (tokens_in + tokens_out)
        if total_tokens:
            summary_parts.append(f"Tokens: {total_tokens}")
        # Provider/model
        summary_parts.append(f"Proveedor: {run['provider']} / Modelo: {run['model']}")
        self.history_detail_summary.setText(" | ".join(summary_parts))
        # Log content
        log_path = run.get("log_file")
        try:
            data = self._read_log_file(log_path)
        except Exception:
            data = "No se pudo leer el log"
        self.history_detail_log.setPlainText(data)
        # Timeline events
        self.history_detail_events.setRowCount(0)
        try:
            events = self.db.get_events_for_run(run_id)
        except Exception:
            events = []
        start_ts = events[0]["timestamp"] if events else None
        for ev in events:
            row_idx = self.history_detail_events.rowCount()
            self.history_detail_events.insertRow(row_idx)
            ts = ev.get("timestamp")
            # Relative time from start
            if start_ts:
                rel = ts - start_ts
                tstr = f"{rel:.2f}s"
            else:
                tstr = time.strftime("%H:%M:%S", time.localtime(ts))
            self.history_detail_events.setItem(row_idx, 0, QTableWidgetItem(tstr))
            self.history_detail_events.setItem(row_idx, 1, QTableWidgetItem(ev.get("event", "")))
            self.history_detail_events.setItem(row_idx, 2, QTableWidgetItem(ev.get("details", "")))

    def _export_history_csv(self) -> None:
        # Ask file path
        default_path = str(self.config_path.parent / "history_export.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Guardar historial como CSV", default_path, "CSV Files (*.csv)")
        if not path:
            return
        show_all = self.show_all_checkbox.isChecked()
        runs = self.db.get_all_runs() if show_all else self.db.get_recent_runs(limit=100)
        try:
            import csv
            with open(path, "w", newline="", encoding="utf-8") as f:
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
            self.statusBar().showMessage(f"Historial exportado a {path}", 3000)
        except Exception as exc:
            self.statusBar().showMessage(f"Error al exportar: {exc}", 5000)

    # Permissions handlers
    def _toggle_sudo(self, state: int) -> None:
        allowed = state == Qt.Checked
        self.permissions.set_sudo(allowed)
        self.statusBar().showMessage("sudo " + ("habilitado" if allowed else "deshabilitado"), 3000)

    def _add_allowed_command(self) -> None:
        cmd = self.cmd_input.text().strip()
        if not cmd:
            return
        self.permissions.add_command(cmd)
        # Add to list widget
        self.command_list.addItem(cmd)
        self.cmd_input.clear()

    def _remove_selected_command(self) -> None:
        row = self.command_list.currentRow()
        if row < 0:
            return
        cmd = self.command_list.item(row).text()
        self.permissions.remove_command(cmd)
        self.command_list.takeItem(row)

    # Stats updates
    def _refresh_stats(self) -> None:
        all_runs = self.db.get_all_runs()
        # runs total
        self.stat_labels["runs_total"].setText(str(len(all_runs)))
        # runs 24h
        now = time.time()
        runs_24h = [r for r in all_runs if r["start_time"] > now - 86400]
        self.stat_labels["runs_24h"].setText(str(len(runs_24h)))
        # errors
        errors = [r for r in all_runs if r["status"] != "success"]
        self.stat_labels["errors_total"].setText(str(len(errors)))
        # avg duration
        durations = [r["end_time"] - r["start_time"] for r in all_runs if r.get("end_time") and r.get("start_time")]
        if durations:
            avg = sum(durations) / len(durations)
            self.stat_labels["avg_duration"].setText(f"{avg:.2f}")
        else:
            self.stat_labels["avg_duration"].setText("-")
        # tokens total
        tokens = 0
        for r in all_runs:
            if r.get("tokens_in"):
                tokens += r["tokens_in"]
            if r.get("tokens_out"):
                tokens += r["tokens_out"]
        self.stat_labels["tokens_total"].setText(str(tokens) if tokens else "-")
        # log usage
        stats = self.log_manager.get_stats()
        total_bytes = stats.get("total_bytes", 0)
        limit_bytes = self.log_manager.max_bytes
        self.stat_labels["log_usage"].setText(f"{_human_bytes(total_bytes)} / {_human_bytes(limit_bytes)}")
        # top logs
        top_files = self.log_manager.get_top_files(3)
        if top_files:
            top_strs = [f"{name} ({_human_bytes(size)})" for name, size in top_files]
            self.stat_labels["top_logs"].setText(", ".join(top_strs))
        else:
            self.stat_labels["top_logs"].setText("-")

        # Denied commands list (show last 10)
        try:
            denied = self.db.get_denied_commands()
        except Exception:
            denied = []
        # Keep the most recent 10 entries
        latest_denied = denied[:10] if denied else []
        # Clear list
        self.denied_list.clear()
        if latest_denied:
            for dc in latest_denied:
                ts = dc["timestamp"]
                cmd = dc["command"]
                dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                self.denied_list.addItem(f"{dt}: {cmd}")
        else:
            self.denied_list.addItem("-")

        # Health status metrics
        # Service status via integration helper
        svc_status = None
        if self.integration_helper is not None:
            try:
                svc_state = self.integration_helper.is_service_active()
                if svc_state is True:
                    svc_status = "activo"
                elif svc_state is False:
                    svc_status = "inactivo"
                else:
                    svc_status = "desconocido"
            except Exception:
                svc_status = "error"
        if svc_status is not None:
            self.stat_labels.get("service_status", QLabel()).setText(svc_status)
        # Proxy status: call local /health endpoint
        proxy_stat = "-"
        proxy_port = getattr(self.integration_helper, "proxy_port", None) if self.integration_helper else None
        if proxy_port:
            try:
                import requests
                resp = requests.get(f"http://127.0.0.1:{proxy_port}/health", timeout=2)
                if resp.ok:
                    proxy_stat = "activo"
                else:
                    proxy_stat = f"{resp.status_code}"
            except Exception:
                proxy_stat = "inactivo"
        self.stat_labels.get("proxy_status", QLabel()).setText(proxy_stat)
        # Provider status: attempt to call models endpoint for selected provider
        prov_stat = "-"
        try:
            config = self.get_config()
            provider = config.get("provider", "openai")
            providers_cfg = config.get("providers", {}) if isinstance(config.get("providers"), dict) else {}
            provider_cfg = providers_cfg.get(provider, {}) if providers_cfg else {}
            base_url = provider_cfg.get("base_url") or "https://api.openai.com"
            api_key = provider_cfg.get("api_key") or None
            api_header = provider_cfg.get("api_key_header") or "Authorization"
            api_prefix = provider_cfg.get("api_key_prefix") or ""
            headers = {}
            if api_key:
                headers[api_header] = f"{api_prefix}{api_key}"
            # Determine models endpoint based on provider
            models_path = "/v1/models"
            # Use GET for list
            import requests
            resp = requests.get(f"{base_url}{models_path}", headers=headers, timeout=4)
            if resp.status_code < 400:
                prov_stat = "ok"
            else:
                prov_stat = str(resp.status_code)
        except Exception:
            prov_stat = "error"
        self.stat_labels.get("provider_status", QLabel()).setText(prov_stat)
        # Internet status: ping a known host
        inet_stat = "-"
        try:
            import socket
            # Try to resolve and connect to a well-known DNS (google DNS)
            sock = socket.create_connection(("8.8.8.8", 53), timeout=2)
            sock.close()
            inet_stat = "ok"
        except Exception:
            inet_stat = "sin red"
        self.stat_labels.get("internet_status", QLabel()).setText(inet_stat)

        # Update tokens per day chart if charts are available
        if _QTCHARTS_AVAILABLE:
            self._update_tokens_chart()


    # Integration
    def _update_integration_status(self) -> None:
        helper = self.integration_helper
        if helper is None:
            return
        svc = helper.is_service_active()
        if svc is True:
            self.service_status_label.setText("activo")
        elif svc is False:
            self.service_status_label.setText("inactivo")
        else:
            self.service_status_label.setText("no disponible")
        connected = helper.has_recent_runs(120)
        self.connection_status_label.setText("conectado" if connected else "desconectado")

    # Allow a denied command via button
    def _allow_selected_denied(self) -> None:
        # If user selects an entry in the denied list, extract the command and add it to allowlist
        current_item = self.denied_list.currentItem() if hasattr(self, "denied_list") else None
        if not current_item:
            return
        text = current_item.text()
        if ":" in text:
            # Format is "timestamp: command"
            cmd = text.split(":", 1)[1].strip()
        else:
            cmd = text.strip()
        if not cmd or cmd == "-":
            return
        # Add to allowlist
        self.permissions.add_command(cmd.split()[0])
        # Update command list UI
        if hasattr(self, "command_list"):
            cmds = [self.command_list.item(i).text() for i in range(self.command_list.count())]
            if cmd.split()[0] not in cmds:
                self.command_list.addItem(cmd.split()[0])
        # Clear status message
        self.statusBar().showMessage(f"Comando '{cmd.split()[0]}' añadido a la lista permitida", 5000)

    def _toggle_instructions(self) -> None:
        self.instructions_visible = not self.instructions_visible
        self.instructions_text.setVisible(self.instructions_visible)
        self.instructions_btn.setText("Ocultar instrucciones" if self.instructions_visible else "Mostrar instrucciones")

    def _generate_override(self) -> None:
        """Prompt the user to choose a directory and write a systemd override file there."""
        if self.integration_helper is None:
            return
        # Ask for directory
        directory = QFileDialog.getExistingDirectory(
            self,
            "Seleccionar directorio para override",
            str(Path.home()),
        )
        if not directory:
            return
        try:
            dir_path = Path(directory)
            path = self.integration_helper.write_override_file(dir_path)
            self.statusBar().showMessage(f"Override creado: {path}", 5000)
        except Exception as exc:
            self.statusBar().showMessage(f"Error creando override: {exc}", 5000)

    def _apply_override_systemd(self) -> None:
        """Attempt to apply the generated override to systemd (requires sudo)."""
        if self.integration_helper is None:
            return
        # Ask user to select the override file to install
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Seleccionar archivo override",
            str(self.config_path.parent),
            "Override files (*.conf *.override)"
        )
        if not path:
            return
        try:
            override_path = Path(path)
            success, message = self.integration_helper.apply_override_to_systemd(override_path)
            if success:
                self.statusBar().showMessage(message, 7000)
            else:
                self.statusBar().showMessage(f"Fallo aplicando override: {message}", 7000)
        except Exception as exc:
            self.statusBar().showMessage(f"Error: {exc}", 7000)

    # Settings
    def _apply_settings(self) -> None:
        selected_theme = self.theme_combo.currentText()
        selected_icon = self.icon_combo.currentText()
        changed = False
        if selected_theme != self.theme:
            self._apply_theme(selected_theme)
            changed = True
        if selected_icon != self.icon_variant:
            self.icon_variant = selected_icon
            self._apply_window_icon()
            changed = True
        # Save provider API keys if changed (encode keys when saving)
        cfg_changed = False
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        providers_cfg = cfg.get("providers", {}) if isinstance(cfg.get("providers"), dict) else {}
        for prov, line in getattr(self, "provider_key_edits", {}).items():
            new_key_plain = line.text().strip()
            # Encode key when saving
            new_key = self._encode_key(new_key_plain) if new_key_plain else ""
            old_key = providers_cfg.get(prov, {}).get("api_key", "")
            if old_key != new_key:
                providers_cfg.setdefault(prov, {})["api_key"] = new_key
                cfg_changed = True
        if cfg_changed:
            cfg["providers"] = providers_cfg
        if changed or cfg_changed:
            # Write back
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            # Update in-memory config
            self.update_config_callback()
            self.statusBar().showMessage("Ajustes aplicados", 3000)

    def _test_api_key(self, provider: str) -> None:
        """Test the API key for the given provider by calling its models endpoint."""
        key = self.provider_key_edits.get(provider).text().strip() if hasattr(self, "provider_key_edits") else ""
        # Determine endpoint and headers
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        prov_cfg = cfg.get("providers", {}).get(provider, {})
        base_url = prov_cfg.get("base_url", "https://api.openai.com")
        api_header = prov_cfg.get("api_key_header", "Authorization")
        api_prefix = prov_cfg.get("api_key_prefix", "")
        headers = {}
        if key:
            headers[api_header] = f"{api_prefix}{key}"
        # Determine models path (OpenAI is /v1/models; others may differ)
        models_path = "/v1/models"
        import requests
        try:
            resp = requests.get(f"{base_url}{models_path}", headers=headers, timeout=5)
            if resp.status_code < 400:
                self.statusBar().showMessage(f"Clave {provider} válida", 4000)
            else:
                self.statusBar().showMessage(f"Clave {provider} no válida ({resp.status_code})", 5000)
        except Exception as exc:
            self.statusBar().showMessage(f"Error al probar clave {provider}: {exc}", 5000)

    def _update_tokens_chart(self) -> None:
        """Update the tokens-per-day chart.  Aggregates tokens over the last 30 days and draws a line chart."""
        if not _QTCHARTS_AVAILABLE:
            return
        try:
            from datetime import datetime, timedelta
            runs = self.db.get_all_runs()
            now = datetime.now()
            # Aggregate tokens by date (YYYY-MM-DD)
            aggregates: Dict[datetime.date, int] = {}
            for r in runs:
                st = r.get("start_time")
                if not st:
                    continue
                dt = datetime.fromtimestamp(st)
                # Consider only last 30 days
                if (now.date() - dt.date()).days > 29:
                    continue
                tokens = 0
                if r.get("tokens_in"):
                    tokens += r.get("tokens_in")
                if r.get("tokens_out"):
                    tokens += r.get("tokens_out")
                if tokens == 0:
                    continue
                aggregates[dt.date()] = aggregates.get(dt.date(), 0) + tokens
            # Prepare series data for last 30 days, oldest to newest
            dates = []
            values = []
            for i in range(29, -1, -1):
                day = now.date() - timedelta(days=i)
                dates.append(day)
                values.append(aggregates.get(day, 0))
            # Build series
            series = QLineSeries()
            for day, value in zip(dates, values):
                # Use midnight for each date
                dt = datetime.combine(day, datetime.min.time())
                qdt = QDateTime(dt)
                series.append(qdt.toMSecsSinceEpoch(), value)
            chart = QChart()
            chart.addSeries(series)
            chart.setTitle("Tokens por día (últimos 30 días)")
            # Configure axes
            axis_x = QDateTimeAxis()
            axis_x.setFormat("dd/MM")
            axis_x.setTickCount(7)
            # Range from oldest to newest date
            start_dt = datetime.combine((now.date() - timedelta(days=29)), datetime.min.time())
            end_dt = datetime.combine(now.date(), datetime.min.time())
            axis_x.setRange(QDateTime(start_dt), QDateTime(end_dt))
            chart.addAxis(axis_x, Qt.AlignBottom)
            series.attachAxis(axis_x)
            # Y axis
            axis_y = QValueAxis()
            axis_y.setLabelFormat("%i")
            axis_y.setMin(0)
            # Compute max with margin
            max_val = max(values) if values else 0
            axis_y.setMax(max_val * 1.2 if max_val > 0 else 1)
            chart.addAxis(axis_y, Qt.AlignLeft)
            series.attachAxis(axis_y)
            chart.legend().hide()
            # Apply chart to view
            self.chart_view.setChart(chart)
        except Exception:
            # In case of error, clear chart
            if hasattr(self, "chart_view"):
                self.chart_view.setChart(QChart())
    # Helpers
    def _read_log_file(self, path: str) -> str:
        if not path:
            return ""
        if path.endswith(".gz"):
            import gzip
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                return f.read()
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()

    # Periodic timers
    def _setup_timers(self) -> None:
        self.timer = QTimer(self)
        # Update interval (in ms). Shorter interval improves live streaming responsiveness.
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

        # Start a separate event processing timer if the in-memory
        # event bus is available.  This timer drains the event queue
        # at a higher frequency than the general timer to deliver near
        # instantaneous updates to the live timeline without polling
        # the database.  When the event bus is disabled, this section
        # does nothing.
        if getattr(self, "event_queue", None) is not None:
            self.event_timer = QTimer(self)
            self.event_timer.setInterval(250)
            self.event_timer.timeout.connect(self._process_event_queue)
            self.event_timer.start()

    def _tick(self) -> None:
        # Update stats, history, live logs, integration status (lightweight)
        self._refresh_stats()
        self._refresh_history()
        self._refresh_live_log()
        if self.integration_helper is not None:
            self._update_integration_status()