#!/usr/bin/env python3
"""LabSupplyControl — desktop control panel for Nice-Power / KUAIQU DC supplies.

Talks the vendor's ASCII '<...>' serial protocol (see kuaiqu/protocol.py).
Defaults target the SPS-D305-232 (30 V / 5 A, single channel) over a
USB-serial connection. Pass --max-voltage / --max-current for other models.

    python3 app.py
    python3 app.py --max-voltage 60 --max-current 5
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime

from PyQt5.QtCore import QEvent, QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QListWidget, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

import charging
import sequence

# Live graphing is optional: the keypad and control panel work without it.
try:
    import numpy as np
    import pyqtgraph as pg
    HAVE_PYQTGRAPH = True
except ImportError:
    HAVE_PYQTGRAPH = False

# Nice-Power supplies connect through a USB-serial bridge, so they rarely report
# "SPS" in their name — they show up as the bridge chip. Match the model name
# and the common bridge identifiers (Silicon Labs CP2102, USB VID 10c4).
DEVICE_NAME_FILTER = ("sps", "spps", "kuaiqu", "cp2102", "cp210x", "silicon lab", "10c4")

from kuaiqu import KuaiquPSU, Measurement, ProtocolError, list_serial_ports

POLL_INTERVAL_MS = 600

# Grace period after switching the output on before the over-current trip arms,
# so a device can draw start-up/inrush current without tripping immediately.
OUTPUT_SETTLE_S = 1.5

_HERE = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(_HERE, "assets", "icon.png")

# User state (presets, settings) lives in the standard per-user config dir,
# rather than next to the code, so it survives updates and stays out of the repo.
_CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "labsupplycontrol")
PRESETS_PATH = os.path.join(_CONFIG_DIR, "presets.json")
SETTINGS_PATH = os.path.join(_CONFIG_DIR, "settings.json")

APP_NAME = "LabSupplyControl"
APP_ID = "labsupplycontrol"   # must match install.sh's .desktop file name
APP_VERSION = "0.1.0"         # early beta
DEFAULT_PRESETS = [
    {"name": "3.3 V logic", "voltage": 3.3, "current": 1.0},
    {"name": "5 V USB", "voltage": 5.0, "current": 2.0},
    {"name": "9 V", "voltage": 9.0, "current": 1.0},
    {"name": "12 V", "voltage": 12.0, "current": 1.0},
]

# Live-graph tuning.
MAX_LOG_POINTS = 200_000          # ~33 h at 600 ms; oldest samples drop beyond this
TIME_WINDOWS = [                  # label -> seconds of history shown (None = all)
    ("30 s", 30), ("1 min", 60), ("5 min", 300), ("10 min", 600), ("All", None),
]


class KeypadDialog(QDialog):
    """Touch-friendly numeric keypad for entering a value within a range."""

    def __init__(self, title: str, value: float, minimum: float, maximum: float,
                 decimals: int, suffix: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self._min = minimum
        self._max = maximum
        self._decimals = decimals
        self._suffix = suffix
        self._text = f"{value:.{decimals}f}".rstrip("0").rstrip(".") or "0"

        layout = QVBoxLayout(self)

        self.display = QLabel()
        self.display.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.display.setFont(QFont("monospace", 26, QFont.Bold))
        self.display.setStyleSheet("background:#111; color:#eee; padding:8px; border-radius:4px;")
        self.display.setMinimumHeight(56)
        layout.addWidget(self.display)

        self.range_label = QLabel(self._range_text())
        self.range_label.setAlignment(Qt.AlignCenter)
        self.range_label.setStyleSheet("color:#888;")
        layout.addWidget(self.range_label)

        grid = QGridLayout()
        layout.addLayout(grid)
        buttons = [
            ("7", 0, 0), ("8", 0, 1), ("9", 0, 2),
            ("4", 1, 0), ("5", 1, 1), ("6", 1, 2),
            ("1", 2, 0), ("2", 2, 1), ("3", 2, 2),
            (".", 3, 0), ("0", 3, 1), ("⌫", 3, 2),
        ]
        for text, r, c in buttons:
            btn = QPushButton(text)
            btn.setMinimumSize(72, 56)
            btn.setFont(QFont("", 16, QFont.Bold))
            btn.clicked.connect(lambda _, t=text: self._press(t))
            grid.addWidget(btn, r, c)

        clear_btn = QPushButton("Clear")
        clear_btn.setMinimumHeight(40)
        clear_btn.clicked.connect(lambda: self._press("C"))
        grid.addWidget(clear_btn, 4, 0, 1, 3)

        actions = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumHeight(44)
        cancel_btn.clicked.connect(self.reject)
        self.ok_btn = QPushButton("OK")
        self.ok_btn.setMinimumHeight(44)
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self.accept)
        actions.addWidget(cancel_btn)
        actions.addWidget(self.ok_btn)
        layout.addLayout(actions)

        self._refresh()

    def _range_text(self) -> str:
        unit = f" {self._suffix}" if self._suffix else ""
        return f"{self._min:g}{unit} – {self._max:g}{unit}"

    def _press(self, key: str) -> None:
        if key.isdigit():
            if "." in self._text and len(self._text.split(".")[1]) >= self._decimals:
                return
            self._text = key if self._text == "0" else self._text + key
        elif key == ".":
            if self._decimals and "." not in self._text:
                self._text = (self._text or "0") + "."
        elif key == "⌫":
            self._text = self._text[:-1]
        elif key == "C":
            self._text = ""
        self._refresh()

    def _parse(self) -> float | None:
        try:
            value = float(self._text)
        except ValueError:
            return None
        if value < self._min or value > self._max:
            return None
        return value

    def _refresh(self) -> None:
        shown = self._text or "0"
        unit = f" {self._suffix}" if self._suffix else ""
        self.display.setText(shown + unit)
        valid = self._parse() is not None
        self.ok_btn.setEnabled(valid)
        self.display.setStyleSheet(
            "background:#111; padding:8px; border-radius:4px; color:"
            + ("#eee" if valid else "#e74c3c")
        )

    def value(self) -> float:
        return self._parse() or 0.0


class KeypadSpinBox(QDoubleSpinBox):
    """Spin box whose (read-only) text field opens an on-screen numeric keypad.

    The up/down arrows still work for fine adjustment; tapping the number opens
    the keypad for direct entry — handy on a touchscreen bench setup.
    """

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._title = title
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setCursor(Qt.PointingHandCursor)
        self.lineEdit().installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.lineEdit() and event.type() == QEvent.MouseButtonRelease:
            if self.isEnabled():
                self.open_keypad()
            return True
        return super().eventFilter(obj, event)

    def open_keypad(self) -> None:
        dialog = KeypadDialog(
            title=self._title, value=self.value(),
            minimum=self.minimum(), maximum=self.maximum(),
            decimals=self.decimals(), suffix=self.suffix().strip(),
            parent=self.window(),
        )
        if dialog.exec_() == QDialog.Accepted:
            self.setValue(dialog.value())


class Poller(QThread):
    """Polls live measurements in the background and emits them to the UI."""

    reading = pyqtSignal(object)   # Measurement
    failed = pyqtSignal(str)

    def __init__(self, psu: KuaiquPSU):
        super().__init__()
        self._psu = psu
        self._running = True

    def run(self) -> None:
        while self._running:
            try:
                self.reading.emit(self._psu.read_measurements())
            except (ProtocolError, OSError) as exc:
                self.failed.emit(str(exc))
            self.msleep(POLL_INTERVAL_MS)

    def stop(self) -> None:
        self._running = False
        self.wait(2000)


class ControlPanel(QMainWindow):
    def __init__(self, max_voltage: float, max_current: float):
        super().__init__()
        self.max_voltage = max_voltage
        self.max_current = max_current
        self.psu: KuaiquPSU | None = None
        self.poller: Poller | None = None
        self.output_on = False
        self._output_on_since: float | None = None
        self._set_current: float | None = None  # last-applied current limit
        self._oc_count = 0                       # consecutive over-current readings
        self._last_measurement: Measurement | None = None
        self._charger: charging.ChargeController | None = None
        self._presets = self._load_presets()
        self._settings = self._load_settings()
        self._suppress_hv_warning = bool(self._settings.get("suppress_hv_warning"))
        self._last_preset_index = 0

        # Timed sequence runner.
        self._sequence: list[sequence.Step] = []
        self._seq_index = 0
        self._seq_start: float | None = None
        self._sequence_running = False
        self._seq_timer = QTimer(self)
        self._seq_timer.setInterval(250)
        self._seq_timer.timeout.connect(self._tick_sequence)

        # Rolling log of live measurements for the graph / CSV export.
        self._mono: list[float] = []   # monotonic seconds (x-axis, immune to clock jumps)
        self._wall: list[datetime] = []
        self._volts: list[float] = []
        self._amps: list[float] = []
        self._cc: list[bool] = []
        self._logging = True           # toggled by the Pause button
        self._window_s: int | None = 60

        self.setWindowTitle(APP_NAME)
        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))
        self._build_ui()
        self._restore_last_setpoints()
        self.refresh_ports()
        self._set_connected(False)
        self._maybe_autoconnect()

    # --- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        # Connection and the live readout are shared across both modes.
        root.addWidget(self._build_connection_group())
        root.addWidget(self._build_readout_group())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_supply_tab(), "Power supply")
        self.tabs.addTab(self._build_charger_tab(), "Charger")
        self.tabs.addTab(self._build_sequence_tab(), "Sequence")
        root.addWidget(self.tabs)

        root.addWidget(self._build_graph_group(), 1)
        root.addWidget(self._build_log())

    def _build_connection_group(self) -> QGroupBox:
        group = QGroupBox("Connection")
        layout = QHBoxLayout(group)

        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(260)
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setFixedWidth(32)
        self.refresh_btn.setToolTip("Rescan serial ports")
        self.refresh_btn.clicked.connect(self.refresh_ports)

        self.show_all_ports = QCheckBox("Show all ports")
        self.show_all_ports.setToolTip(
            "By default only likely Nice-Power supplies are listed (matched by "
            "model name or the CP2102/CP210x USB-serial bridge). Tick this if "
            "your adapter uses a different, generic name."
        )
        self.show_all_ports.toggled.connect(self.refresh_ports)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.toggle_connection)

        layout.addWidget(QLabel("Port:"))
        layout.addWidget(self.port_combo, 1)
        layout.addWidget(self.refresh_btn)
        layout.addWidget(self.show_all_ports)
        layout.addWidget(self.connect_btn)
        return group

    def _build_readout_group(self) -> QGroupBox:
        group = QGroupBox("OUTPUT")
        group.setStyleSheet(
            "QGroupBox {"
            " background:#000; border:1px solid #333; border-radius:6px;"
            " margin-top:14px; }"
            "QGroupBox::title {"
            " subcontrol-origin:margin; subcontrol-position:top left; left:10px;"
            " padding:0 6px; color:#eee; background:#000; font-weight:bold; }"
        )
        grid = QGridLayout(group)
        grid.setContentsMargins(10, 6, 8, 8)  # 10px left margin on the content
        grid.setHorizontalSpacing(22)

        # Column headers (row 0), columns 1-3; column 0 holds the row labels.
        grid.addWidget(self._caption("Volts"), 0, 1)
        grid.addWidget(self._caption("Amps"), 0, 2)
        grid.addWidget(self._caption("Watts"), 0, 3)

        # Actual (measured) row — big figures.
        grid.addWidget(self._row_label("Actual", "#19be6b"), 1, 0)
        self.v_display = self._big_display("--.--", "#2d8cf0")
        self.a_display = self._big_display("--.--", "#19be6b")
        self.w_display = self._big_display("--.--", "#ff9900")
        grid.addWidget(self.v_display, 1, 1)
        grid.addWidget(self.a_display, 1, 2)
        grid.addWidget(self.w_display, 1, 3)

        # Set-point row — smaller; the protocol can't read these back, so we
        # show what the app last applied (no watts set-point).
        grid.addWidget(self._row_label("Set", "#ff9900"), 2, 0)
        self.v_set_label = self._set_value("#2d8cf0")
        self.a_set_label = self._set_value("#19be6b")
        grid.addWidget(self.v_set_label, 2, 1)
        grid.addWidget(self.a_set_label, 2, 2)

        self.mode_badge = QLabel("—")
        self.mode_badge.setAlignment(Qt.AlignCenter)
        self.mode_badge.setFont(QFont("", 11, QFont.Bold))
        self.mode_badge.setFixedHeight(28)
        self.mode_badge.setStyleSheet("color:#888;")
        grid.addWidget(self.mode_badge, 3, 0, 1, 4)

        # Keep columns tight and packed to the left: a trailing stretch column
        # absorbs the extra width instead of spreading the readings apart.
        for col in (1, 2, 3):
            grid.setColumnMinimumWidth(col, 150)
        grid.setColumnStretch(4, 1)
        return group

    def _build_supply_tab(self) -> QWidget:
        """Manual bench control: set-points, presets, and the output switch."""
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.addWidget(self._build_setpoint_group(), 1)
        layout.addWidget(self._build_presets_group(), 1)
        layout.addWidget(self._build_output_group(), 1)
        return tab

    def _build_charger_tab(self) -> QWidget:
        """Battery charging: chemistry template + managed CC-CV charge."""
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.addStretch(1)
        layout.addWidget(self._build_battery_group(), 2)
        layout.addStretch(1)
        return tab

    def _build_sequence_tab(self) -> QWidget:
        """Run a CSV of timed actions (set V/A, output on/off) on a timeline."""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        bar = QHBoxLayout()
        self.seq_load_btn = QPushButton("Load CSV…")
        self.seq_load_btn.setToolTip(
            "Load a sequence: columns time,output,voltage,current — e.g. "
            "'00:00:30,on,5.0,1.0'. Time is elapsed from the start.")
        self.seq_load_btn.clicked.connect(self.load_sequence)
        self.seq_run_btn = QPushButton("Run")
        self.seq_run_btn.clicked.connect(self.toggle_sequence)
        self.seq_run_btn.setEnabled(False)
        bar.addWidget(self.seq_load_btn)
        bar.addWidget(self.seq_run_btn)
        bar.addStretch(1)
        self.seq_status = QLabel("No sequence loaded")
        self.seq_status.setStyleSheet("color:#888;")
        bar.addWidget(self.seq_status)
        layout.addLayout(bar)

        self.seq_list = QListWidget()
        # Read-only view: the user watches progress, can't click-select rows.
        self.seq_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.seq_list.setFocusPolicy(Qt.NoFocus)
        layout.addWidget(self.seq_list, 1)
        return tab

    def _build_setpoint_group(self) -> QGroupBox:
        group = QGroupBox("Set-points")
        form = QFormLayout(group)

        self.v_set = KeypadSpinBox("Set voltage")
        self.v_set.setRange(0, self.max_voltage)
        self.v_set.setDecimals(2)
        self.v_set.setSingleStep(0.1)
        self.v_set.setSuffix(" V")

        self.a_set = KeypadSpinBox("Set current")
        self.a_set.setRange(0, self.max_current)
        self.a_set.setDecimals(3)
        self.a_set.setSingleStep(0.01)
        self.a_set.setSuffix(" A")

        self.apply_btn = QPushButton("Apply V && A")
        self.apply_btn.clicked.connect(self.apply_setpoints)

        hint = QLabel("Tap a value to open the keypad.")
        hint.setStyleSheet("color:#888;")

        form.addRow("Voltage:", self.v_set)
        form.addRow("Current:", self.a_set)
        form.addRow(hint)
        form.addRow(self.apply_btn)
        return group

    def _build_presets_group(self) -> QGroupBox:
        group = QGroupBox("Presets")
        layout = QVBoxLayout(group)

        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip("Pick a saved V/A preset to load it into the "
                                     "set-points.")
        self.preset_combo.activated.connect(self._on_preset_selected)
        layout.addWidget(self.preset_combo)

        self.preset_apply_btn = QPushButton("Apply")
        self.preset_apply_btn.setToolTip("Load the selected preset and write it to the supply.")
        self.preset_apply_btn.clicked.connect(self._on_preset_apply)
        layout.addWidget(self.preset_apply_btn)

        row = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.setToolTip("Update the selected preset with the current set-points.")
        save_btn.clicked.connect(self._on_preset_update)
        save_as_btn = QPushButton("Save as…")
        save_as_btn.setToolTip("Save the current set-points as a new named preset.")
        save_as_btn.clicked.connect(self._on_preset_save)
        delete_btn = QPushButton("Delete")
        delete_btn.setToolTip("Delete the selected preset.")
        delete_btn.clicked.connect(self._on_preset_delete)
        for btn in (save_btn, save_as_btn, delete_btn):
            row.addWidget(btn)
        layout.addLayout(row)
        layout.addStretch(1)  # keep content pushed to the top

        self._refresh_preset_combo()
        return group

    def _build_output_group(self) -> QGroupBox:
        group = QGroupBox("Output control")
        layout = QVBoxLayout(group)
        self.output_btn = QPushButton("OUTPUT OFF")
        self.output_btn.setCheckable(True)
        self.output_btn.setMinimumHeight(56)
        self.output_btn.setFont(QFont("", 13, QFont.Bold))
        self.output_btn.clicked.connect(self.toggle_output)
        layout.addWidget(self.output_btn)

        # Software over-current trip: shut the output off (like a fuse) when the
        # supply reaches its set current limit, instead of just current-limiting.
        self.ocp_enable = QCheckBox("Power off on overcurrent")
        self.ocp_enable.setToolTip(
            "Shut the output off if the supply reaches the set current (enters "
            "current-limiting / CC), rather than holding the limit.")
        self.ocp_enable.toggled.connect(self._on_ocp_toggled)
        layout.addWidget(self.ocp_enable)

        layout.addStretch(1)  # keep the content pushed to the top
        return group

    def _build_battery_group(self) -> QGroupBox:
        group = QGroupBox("Battery charge")
        form = QFormLayout(group)

        self.chem_combo = QComboBox()
        for key, profile in charging.PROFILES.items():
            self.chem_combo.addItem(profile.name, userData=key)
        self.chem_combo.currentIndexChanged.connect(self._on_chemistry_changed)

        self.cells_spin = QSpinBox()
        self.cells_spin.setRange(1, 24)
        self.cells_spin.valueChanged.connect(self._on_cells_changed)

        self.capacity_spin = QDoubleSpinBox()
        self.capacity_spin.setRange(0.05, 100.0)
        self.capacity_spin.setDecimals(2)
        self.capacity_spin.setSingleStep(0.1)
        self.capacity_spin.setSuffix(" Ah")
        self.capacity_spin.setValue(2.0)
        self.capacity_spin.valueChanged.connect(self._update_charge_summary)

        self.crate_spin = QDoubleSpinBox()
        self.crate_spin.setRange(0.01, 2.0)
        self.crate_spin.setDecimals(2)
        self.crate_spin.setSingleStep(0.05)
        self.crate_spin.setSuffix(" C")
        self.crate_spin.valueChanged.connect(self._update_charge_summary)

        self.charge_summary = QLabel()
        self.charge_summary.setWordWrap(True)
        self.charge_summary.setStyleSheet("color:#ccc;")

        self.charge_btn = QPushButton("Start charge")
        self.charge_btn.setMinimumHeight(44)
        self.charge_btn.clicked.connect(self.toggle_charge)

        self.charge_status = QLabel("Idle")
        self.charge_status.setAlignment(Qt.AlignCenter)
        self.charge_status.setFont(QFont("", 11, QFont.Bold))
        self.charge_status.setFixedHeight(28)

        form.addRow("Battery:", self.chem_combo)
        form.addRow("Cells:", self.cells_spin)
        form.addRow("Capacity:", self.capacity_spin)
        form.addRow("Rate:", self.crate_spin)
        form.addRow(self.charge_summary)
        form.addRow(self.charge_btn)
        form.addRow(self.charge_status)

        self._on_chemistry_changed()
        return group

    def _build_log(self) -> QPlainTextEdit:
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(110)
        self.log.setPlaceholderText("Status messages appear here…")
        return self.log

    def _build_graph_group(self) -> QGroupBox:
        group = QGroupBox("Live graph")
        layout = QVBoxLayout(group)

        if not HAVE_PYQTGRAPH:
            msg = QLabel(
                "Live graphing needs pyqtgraph + numpy.\n"
                "Install with:  sudo apt install python3-pyqtgraph python3-numpy"
            )
            msg.setAlignment(Qt.AlignCenter)
            msg.setStyleSheet("color:#888;")
            msg.setMinimumHeight(140)
            layout.addWidget(msg)
            self.plot = None
            return group

        # Control bar: time window + pause / clear / export.
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Window:"))
        self.window_combo = QComboBox()
        for label, seconds in TIME_WINDOWS:
            self.window_combo.addItem(label, userData=seconds)
        self.window_combo.setCurrentText("1 min")
        self.window_combo.currentIndexChanged.connect(self._on_window_changed)
        bar.addWidget(self.window_combo)
        bar.addSpacing(16)

        self.show_v = QCheckBox("Voltage")
        self.show_v.setChecked(True)
        self.show_v.setStyleSheet("color:#2d8cf0;")
        self.show_v.toggled.connect(self._on_curve_toggled)
        self.show_a = QCheckBox("Current")
        self.show_a.setChecked(True)
        self.show_a.setStyleSheet("color:#19be6b;")
        self.show_a.toggled.connect(self._on_curve_toggled)
        bar.addWidget(self.show_v)
        bar.addWidget(self.show_a)
        bar.addStretch(1)

        self.pause_btn = QPushButton("Pause logging")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._on_pause_toggled)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear_log)
        self.export_btn = QPushButton("Export CSV…")
        self.export_btn.clicked.connect(self.export_csv)
        for btn in (self.pause_btn, self.clear_btn, self.export_btn):
            bar.addWidget(btn)
        layout.addLayout(bar)

        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget()
        self.plot.setBackground("#1e1e1e")
        self.plot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.plot.setMinimumHeight(200)
        plot_item = self.plot.getPlotItem()
        plot_item.showGrid(x=True, y=True, alpha=0.2)
        plot_item.setLabel("bottom", "Elapsed", units="s")
        plot_item.setLabel("left", "Voltage", units="V", color="#2d8cf0")

        self.v_curve = plot_item.plot(pen=pg.mkPen("#2d8cf0", width=2))
        self.v_curve.setDownsampling(auto=True)
        self.v_curve.setClipToView(True)

        # Current shares the X axis but gets its own right-hand Y axis.
        self.current_vb = pg.ViewBox()
        plot_item.showAxis("right")
        plot_item.scene().addItem(self.current_vb)
        plot_item.getAxis("right").linkToView(self.current_vb)
        plot_item.getAxis("right").setLabel("Current", units="A", color="#19be6b")
        self.current_vb.setXLink(plot_item)
        self.a_curve = pg.PlotDataItem(pen=pg.mkPen("#19be6b", width=2))
        self.a_curve.setDownsampling(auto=True)
        self.a_curve.setClipToView(True)
        self.current_vb.addItem(self.a_curve)
        plot_item.vb.sigResized.connect(self._sync_current_view)

        layout.addWidget(self.plot)
        return group

    def _sync_current_view(self) -> None:
        """Keep the current axis' view box aligned with the voltage plot."""
        plot_item = self.plot.getPlotItem()
        self.current_vb.setGeometry(plot_item.vb.sceneBoundingRect())
        self.current_vb.linkedViewChanged(plot_item.vb, self.current_vb.XAxis)

    def _big_display(self, text: str, color: str) -> QLabel:
        label = QLabel(text)
        label.setAlignment(Qt.AlignCenter)
        label.setFont(QFont("monospace", 30, QFont.Bold))
        label.setStyleSheet(f"color: {color};")
        return label

    def _caption(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("color: #888;")
        return label

    def _row_label(self, text: str, color: str) -> QLabel:
        label = QLabel(text)
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        label.setFont(QFont("", 13, QFont.Bold))
        label.setStyleSheet(f"color:{color};")
        return label

    def _set_value(self, color: str) -> QLabel:
        label = QLabel("—")
        label.setAlignment(Qt.AlignCenter)
        label.setFont(QFont("monospace", 16))
        label.setStyleSheet(f"color: {color};")
        return label

    # --- Connection handling ------------------------------------------------

    def refresh_ports(self) -> None:
        self.port_combo.clear()
        name_filter = None if self.show_all_ports.isChecked() else DEVICE_NAME_FILTER
        ports = list_serial_ports(name_filter)
        if not ports:
            message = ("No KUAIQU supply found — tick 'Show all ports'"
                       if name_filter else "No serial ports found")
            self.port_combo.addItem(message, userData=None)
            return
        for port in ports:
            self.port_combo.addItem(str(port), userData=port.device)

    def toggle_connection(self) -> None:
        if self.psu is None:
            self.connect()
        else:
            self.disconnect()

    def _maybe_autoconnect(self) -> None:
        """If exactly one device was found, connect to it automatically."""
        devices = [self.port_combo.itemData(i) for i in range(self.port_combo.count())]
        if self.psu is None and sum(d is not None for d in devices) == 1:
            self._log("One device found — connecting automatically.")
            self.connect(silent=True)

    def connect(self, silent: bool = False) -> None:
        device = self.port_combo.currentData()
        if not device:
            self._log("No serial port selected.")
            return
        try:
            self.psu = KuaiquPSU(device)
            self.psu.set_remote(True)
            # Confirm the link by reading once; raises if the device is silent.
            self.psu.read_measurements()
            self._log(f"Connected to {device}; device switched to REMOTE mode.")
        except (ProtocolError, OSError) as exc:
            self._log(f"Connect failed: {exc}")
            if self.psu is not None:
                self.psu.close()
            self.psu = None
            if not silent:
                QMessageBox.critical(
                    self, "Connection failed",
                    f"Could not talk to the supply on {device}.\n\n{exc}\n\n"
                    "Check that the right port is selected and nothing else has "
                    "the port open."
                )
            return

        self.poller = Poller(self.psu)
        self.poller.reading.connect(self.on_reading)
        self.poller.failed.connect(self.on_poll_failed)
        self.poller.start()
        self._set_connected(True)

    def disconnect(self) -> None:
        if self._sequence_running:
            self._seq_timer.stop()
            self._sequence_running = False
            self._set_sequence_running(False)
            self.seq_status.setText("Idle")
        if self._charger is not None:
            self._charger = None
            self._set_charging(False)
            self.charge_status.setText("Idle")
        if self.poller is not None:
            self.poller.stop()
            self.poller = None
        if self.psu is not None:
            try:
                self.psu.set_output(False)
                self.psu.set_remote(False)  # hand control back to the front panel
                self._log("Output off, device returned to LOCAL mode.")
            except (ProtocolError, OSError) as exc:
                self._log(f"Warning during disconnect: {exc}")
            self.psu.close()
            self.psu = None
        self.output_on = False
        self._set_connected(False)
        self._reset_displays()

    # --- Commands -----------------------------------------------------------

    def apply_setpoints(self) -> None:
        if not self._require_connection():
            return
        volts, amps = self.v_set.value(), self.a_set.value()
        try:
            self.psu.set_voltage_current(volts, amps)
            self._set_current = amps
            self.v_set_label.setText(f"{volts:.2f}")
            self.a_set_label.setText(f"{amps:.3f}")
            self._settings["last_voltage"] = volts
            self._settings["last_current"] = amps
            self._save_settings()
            self._log(f"Set {volts:.2f} V / {amps:.3f} A.")
        except (ProtocolError, OSError) as exc:
            self._log(f"Set-point write failed: {exc}")

    def toggle_output(self) -> None:
        if not self._require_connection():
            self.output_btn.setChecked(self.output_on)
            return
        want_on = self.output_btn.isChecked()
        try:
            self.psu.set_output(want_on)
            self.output_on = want_on
            if want_on:
                self._output_on_since = time.monotonic()
            else:
                self._output_on_since = None
                self.mode_badge.setText("—")
                self.mode_badge.setStyleSheet("color:#888;")
            self._log(f"Output {'ON' if want_on else 'OFF'}.")
        except (ProtocolError, OSError) as exc:
            self._log(f"Output switch failed: {exc}")
        finally:
            self._reflect_output_button()

    @staticmethod
    def _fmt_elapsed(seconds: float) -> str:
        s = int(seconds)
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"

    def _output_settling(self) -> bool:
        """True during the power-up grace period after the output came on."""
        return (self._output_on_since is not None
                and time.monotonic() - self._output_on_since < OUTPUT_SETTLE_S)

    def _current_limit(self) -> float:
        """The active current limit the over-current trip compares against."""
        return self._set_current if self._set_current is not None else self.a_set.value()

    def _is_overcurrent(self, current: float) -> bool:
        return current > self._current_limit()

    def _on_ocp_toggled(self, checked: bool) -> None:
        """Enforce immediately if armed while already running over the limit."""
        if (checked and self.output_on and self._charger is None
                and not self._output_settling()
                and self._last_measurement is not None
                and self._is_overcurrent(self._last_measurement.current)):
            self._trip_overcurrent(self._last_measurement.current)

    def _trip_overcurrent(self, current: float) -> None:
        """Shut the output off because the supply hit its set current limit."""
        self._oc_count = 0
        elapsed = (time.monotonic() - self._output_on_since
                   if self._output_on_since is not None else 0.0)
        if self._sequence_running:
            self._seq_timer.stop()
            self._sequence_running = False
            self._set_sequence_running(False)
            self.seq_status.setText("Tripped")
        if self._charger is not None:
            self._charger = None
            self._set_charging(False)
            self.charge_status.setText("Tripped")
        try:
            if self.psu is not None:
                self.psu.set_output(False)
        except (ProtocolError, OSError) as exc:
            self._log(f"Failed to shut off output on over-current: {exc}")
        self.output_on = False
        self._output_on_since = None
        self._reflect_output_button()
        self.mode_badge.setText("OVER-CURRENT — OUTPUT OFF")
        self.mode_badge.setStyleSheet("background:#c0392b; color:white; border-radius:4px;")
        self._log(f"Output shut off after {self._fmt_elapsed(elapsed)} due to "
                  f"overcurrent ({current:.3f} A over the "
                  f"{self._current_limit():.3f} A limit).")

    # --- Presets ------------------------------------------------------------

    def _load_presets(self) -> list[dict]:
        try:
            with open(PRESETS_PATH) as handle:
                data = json.load(handle)
            presets = [{"name": str(p["name"]), "voltage": float(p["voltage"]),
                        "current": float(p["current"])} for p in data]
            return presets or list(DEFAULT_PRESETS)
        except (OSError, ValueError, KeyError, TypeError):
            return list(DEFAULT_PRESETS)

    def _save_presets(self) -> None:
        try:
            os.makedirs(_CONFIG_DIR, exist_ok=True)
            with open(PRESETS_PATH, "w") as handle:
                json.dump(self._presets, handle, indent=2)
        except OSError as exc:
            self._log(f"Could not save presets: {exc}")

    def _load_settings(self) -> dict:
        try:
            with open(SETTINGS_PATH) as handle:
                return dict(json.load(handle))
        except (OSError, ValueError, TypeError):
            return {}

    def _save_settings(self) -> None:
        try:
            os.makedirs(_CONFIG_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w") as handle:
                json.dump(self._settings, handle, indent=2)
        except OSError as exc:
            self._log(f"Could not save settings: {exc}")

    def _restore_last_setpoints(self) -> None:
        """Load the voltage/current set-points from the last session."""
        volts = self._settings.get("last_voltage")
        amps = self._settings.get("last_current")
        if isinstance(volts, (int, float)):
            self.v_set.setValue(min(float(volts), self.max_voltage))
        if isinstance(amps, (int, float)):
            self.a_set.setValue(min(float(amps), self.max_current))

    def _confirm_higher_voltage(self, new_voltage: float) -> bool:
        """Warn (once-dismissable) before a preset raises the voltage on a live
        output. Pointless when the output is off or when raising from 0 V."""
        current = self.v_set.value()
        if (self._suppress_hv_warning or not self.output_on
                or current <= 0 or new_voltage <= current):
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Raising voltage")
        box.setText(f"This preset raises the voltage to {new_voltage:.2f} V "
                    f"(now {self.v_set.value():.2f} V).")
        box.setInformativeText(
            "A higher voltage can damage whatever is connected. Continue?")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        dont_show = QCheckBox("Don't show this warning again")
        box.setCheckBox(dont_show)
        proceed = box.exec_() == QMessageBox.Yes
        if dont_show.isChecked():
            self._suppress_hv_warning = True
            self._settings["suppress_hv_warning"] = True
            self._save_settings()
        return proceed

    def _refresh_preset_combo(self) -> None:
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for preset in self._presets:
            self.preset_combo.addItem(
                f"{preset['name']} — {preset['voltage']:.2f} V / "
                f"{preset['current']:.3f} A", userData=preset)
        self.preset_combo.blockSignals(False)

    def _apply_preset_to_fields(self, preset: dict) -> None:
        self.v_set.setValue(min(preset["voltage"], self.max_voltage))
        self.a_set.setValue(min(preset["current"], self.max_current))

    def _on_preset_selected(self, index: int) -> None:
        preset = self.preset_combo.itemData(index)
        if preset is None:
            return
        if not self._confirm_higher_voltage(preset["voltage"]):
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(self._last_preset_index)
            self.preset_combo.blockSignals(False)
            return
        self._apply_preset_to_fields(preset)
        self._last_preset_index = index

    def _on_preset_apply(self) -> None:
        preset = self.preset_combo.currentData()
        if preset is None:
            return
        if not self._confirm_higher_voltage(preset["voltage"]):
            return
        self._apply_preset_to_fields(preset)
        self._last_preset_index = self.preset_combo.currentIndex()
        self.apply_setpoints()

    def _on_preset_update(self) -> None:
        """Update the selected preset in place with the current set-points."""
        preset = self.preset_combo.currentData()
        if preset is None:        # nothing to update -> behave like Save as…
            self._on_preset_save()
            return
        name = preset["name"]
        updated = {"name": name, "voltage": self.v_set.value(),
                   "current": self.a_set.value()}
        self._presets = [updated if p["name"] == name else p for p in self._presets]
        self._save_presets()
        self._refresh_preset_combo()
        for i in range(self.preset_combo.count()):
            if self.preset_combo.itemData(i)["name"] == name:
                self.preset_combo.setCurrentIndex(i)
                self._last_preset_index = i
                break
        self._log(f"Updated preset '{name}' "
                  f"({updated['voltage']:.2f} V / {updated['current']:.3f} A).")

    def _on_preset_save(self) -> None:
        name, ok = QInputDialog.getText(self, "Save preset as", "Preset name:")
        name = name.strip()
        if not ok or not name:
            return
        preset = {"name": name, "voltage": self.v_set.value(),
                  "current": self.a_set.value()}
        self._presets = [p for p in self._presets if p["name"] != name]
        self._presets.append(preset)
        self._presets.sort(key=lambda p: (p["voltage"], p["current"]))
        self._save_presets()
        self._refresh_preset_combo()
        for i in range(self.preset_combo.count()):
            if self.preset_combo.itemData(i)["name"] == name:
                self.preset_combo.setCurrentIndex(i)
                self._last_preset_index = i
                break
        self._log(f"Saved preset '{name}' "
                  f"({preset['voltage']:.2f} V / {preset['current']:.3f} A).")

    def _on_preset_delete(self) -> None:
        preset = self.preset_combo.currentData()
        if preset is None:
            return
        self._presets = [p for p in self._presets if p["name"] != preset["name"]]
        self._save_presets()
        self._refresh_preset_combo()
        self._last_preset_index = self.preset_combo.currentIndex()
        self._log(f"Deleted preset '{preset['name']}'.")

    # --- Battery charge -----------------------------------------------------

    def _current_profile(self) -> charging.ChemistryProfile:
        return charging.PROFILES[self.chem_combo.currentData()]

    def _build_plan(self) -> charging.ChargePlan:
        return charging.ChargePlan(
            profile=self._current_profile(),
            cells=self.cells_spin.value(),
            capacity_ah=self.capacity_spin.value(),
            c_rate=self.crate_spin.value(),
        )

    def _on_chemistry_changed(self, *_) -> None:
        profile = self._current_profile()
        self.cells_spin.setValue(profile.default_cells)
        self.crate_spin.setValue(profile.default_c_rate)
        self._update_charge_summary()

    def _on_cells_changed(self, cells: int) -> None:
        profile = self._current_profile()
        if profile.needs_balancer and cells > 1:
            response = QMessageBox.warning(
                self, "Multi-cell lithium",
                f"{cells} cells in series ({profile.name}).\n\n"
                "This supply cannot balance cells or sense temperature. Charging "
                "a multi-cell lithium pack without a BMS/balancer risks "
                "overcharging an individual cell — a fire hazard.\n\n"
                "Only continue if your pack has a balancer/BMS. "
                f"Keep {cells} cells?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if response != QMessageBox.Yes:
                self.cells_spin.blockSignals(True)
                self.cells_spin.setValue(1)
                self.cells_spin.blockSignals(False)
        self._update_charge_summary()

    def _update_charge_summary(self, *_) -> None:
        plan = self._build_plan()
        parts = [f"→ {plan.target_voltage:.2f} V", f"{plan.charge_current:.3f} A"]
        if plan.cutoff_current is not None:
            parts.append(f"cutoff {plan.cutoff_current:.3f} A")
        if plan.float_voltage is not None:
            parts.append(f"float {plan.float_voltage:.2f} V")
        text = ", ".join(parts)
        if not plan.fits(self.max_voltage, self.max_current):
            text += "  ⚠ exceeds supply limits"
        self.charge_summary.setText(text)

    def toggle_charge(self) -> None:
        if self._charger is None:
            self.start_charge()
        else:
            self.stop_charge(user=True)

    def start_charge(self) -> None:
        if not self._require_connection():
            return
        plan = self._build_plan()
        if not plan.fits(self.max_voltage, self.max_current):
            QMessageBox.warning(
                self, "Pack doesn't fit",
                f"This pack needs {plan.target_voltage:.2f} V / "
                f"{plan.charge_current:.3f} A, beyond the supply's "
                f"{self.max_voltage:.0f} V / {self.max_current:.0f} A.")
            return

        profile = plan.profile
        warning = "Charging must be supervised.\n\n"
        if profile.needs_balancer and plan.cells > 1:
            warning += ("⚠ Multi-cell lithium needs a BMS / balance board — this "
                        "supply does NOT balance cells or sense temperature.\n\n")
        warning += (
            f"{profile.name} · {plan.cells} cell(s) · {plan.capacity_ah:g} Ah\n"
            f"Target {plan.target_voltage:.2f} V, charge {plan.charge_current:.3f} A"
            + (f", stop at {plan.cutoff_current:.3f} A" if plan.cutoff_current else "")
            + (f", float {plan.float_voltage:.2f} V" if plan.float_voltage else "")
            + "\n\nStart charging?")
        if QMessageBox.question(self, "Start charge", warning) != QMessageBox.Yes:
            return

        try:
            self.psu.set_voltage_current(plan.target_voltage, plan.charge_current)
            self.psu.set_output(True)
        except (ProtocolError, OSError) as exc:
            self._log(f"Could not start charge: {exc}")
            return

        self.output_on = True
        self._output_on_since = time.monotonic()
        self._set_current = plan.charge_current
        self._reflect_output_button()
        self.v_set_label.setText(f"{plan.target_voltage:.2f}")
        self.a_set_label.setText(f"{plan.charge_current:.3f}")
        self._charger = charging.ChargeController(plan)
        self._set_charging(True)
        self.charge_status.setText(self._charger.phase.value)
        self._log(f"Charge started: {profile.name}, "
                  f"{plan.target_voltage:.2f} V / {plan.charge_current:.3f} A.")

    def stop_charge(self, *, user: bool = False, completed: bool = False) -> None:
        if self._charger is None:
            return
        self._charger = None
        try:
            if self.psu is not None:
                self.psu.set_output(False)
                self.output_on = False
                self._reflect_output_button()
        except (ProtocolError, OSError) as exc:
            self._log(f"Warning stopping charge: {exc}")
        self._set_charging(False)
        if completed:
            self.charge_status.setText("Done ✓")
            self._log("Charge complete — output off.")
        elif user:
            self.charge_status.setText("Stopped")
            self._log("Charge stopped.")

    def _advance_charge(self, m: Measurement) -> None:
        action = self._charger.update(m.constant_current, m.current)
        if action is charging.Action.SET_FLOAT:
            try:
                self.psu.set_voltage(self._charger.plan.float_voltage)
                self.v_set_label.setText(f"{self._charger.plan.float_voltage:.2f}")
                self._log(f"Float: holding {self._charger.plan.float_voltage:.2f} V.")
            except (ProtocolError, OSError) as exc:
                self._log(f"Float switch failed: {exc}")
        elif action is charging.Action.STOP:
            self.stop_charge(completed=True)
        if self._charger is not None:
            self.charge_status.setText(self._charger.phase.value)

    def _set_charging(self, active: bool) -> None:
        self.charge_btn.setText("Stop charge" if active else "Start charge")
        for widget in (self.chem_combo, self.cells_spin, self.capacity_spin,
                       self.crate_spin, self.v_set, self.a_set, self.apply_btn,
                       self.preset_apply_btn, self.output_btn, self.seq_run_btn):
            widget.setEnabled(not active)

    # --- Sequence -----------------------------------------------------------

    def load_sequence(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load sequence CSV", "", "CSV files (*.csv);;All files (*)")
        if not path:
            return
        try:
            with open(path) as handle:
                steps = sequence.parse_sequence(
                    handle.read(), self.max_voltage, self.max_current)
        except (OSError, sequence.SequenceError) as exc:
            QMessageBox.warning(self, "Invalid sequence",
                                f"Could not load the sequence:\n\n{exc}")
            return
        self._sequence = steps
        self.seq_list.clear()
        for step in steps:
            volts = f"{step.voltage:.2f} V" if step.voltage is not None else "—"
            amps = f"{step.current:.3f} A" if step.current is not None else "—"
            self.seq_list.addItem(
                f"{self._fmt_elapsed(step.time_s)}   OUTPUT "
                f"{'ON ' if step.output else 'OFF'}   {volts}   {amps}")
        self.seq_status.setText(f"{len(steps)} steps · {os.path.basename(path)}")
        self.seq_run_btn.setEnabled(self.psu is not None)
        self._log(f"Loaded sequence '{os.path.basename(path)}' ({len(steps)} steps).")

    def toggle_sequence(self) -> None:
        if self._sequence_running:
            self.stop_sequence(user=True)
        else:
            self.start_sequence()

    def start_sequence(self) -> None:
        if not self._require_connection() or not self._sequence:
            return
        self._seq_index = 0
        self._seq_start = time.monotonic()
        self._sequence_running = True
        self._set_sequence_running(True)
        self._highlight_seq_step(-1)  # clear any previous highlight
        self._log("Sequence started.")
        self._seq_timer.start()
        self._tick_sequence()  # run any step scheduled at t=0 immediately

    def _tick_sequence(self) -> None:
        if not self._sequence_running or self._seq_start is None:
            return
        elapsed = time.monotonic() - self._seq_start
        while (self._seq_index < len(self._sequence)
               and self._sequence[self._seq_index].time_s <= elapsed):
            self._execute_step(self._sequence[self._seq_index])
            if not self._sequence_running:
                return  # a step failed and stopped the run
            self._highlight_seq_step(self._seq_index)
            self._seq_index += 1
        if self._seq_index >= len(self._sequence):
            self.stop_sequence(completed=True)
            return
        nxt = self._sequence[self._seq_index]
        self.seq_status.setText(
            f"Running · step {self._seq_index + 1}/{len(self._sequence)} · "
            f"next in {int(max(0, nxt.time_s - elapsed))} s")

    def _execute_step(self, step: sequence.Step) -> None:
        try:
            if step.voltage is not None or step.current is not None:
                volts = step.voltage if step.voltage is not None else self.v_set.value()
                amps = step.current if step.current is not None else self.a_set.value()
                self.psu.set_voltage_current(volts, amps)
                self._set_current = amps
                self.v_set.setValue(min(volts, self.max_voltage))
                self.a_set.setValue(min(amps, self.max_current))
                self.v_set_label.setText(f"{volts:.2f}")
                self.a_set_label.setText(f"{amps:.3f}")
            self.psu.set_output(step.output)
            self.output_on = step.output
            self._output_on_since = time.monotonic() if step.output else None
            self._reflect_output_button()
            detail = ((f", {step.voltage:.2f} V" if step.voltage is not None else "")
                      + (f", {step.current:.3f} A" if step.current is not None else ""))
            self._log(f"Step {self._fmt_elapsed(step.time_s)}: output "
                      f"{'ON' if step.output else 'OFF'}{detail}.")
        except (ProtocolError, OSError) as exc:
            self._log(f"Sequence step failed: {exc}")
            self.stop_sequence()

    def stop_sequence(self, *, user: bool = False, completed: bool = False) -> None:
        self._seq_timer.stop()
        self._sequence_running = False
        try:
            if self.psu is not None:
                self.psu.set_output(False)
                self.output_on = False
                self._output_on_since = None
                self._reflect_output_button()
        except (ProtocolError, OSError) as exc:
            self._log(f"Warning stopping sequence: {exc}")
        self._set_sequence_running(False)
        if completed:
            self.seq_status.setText("Done ✓")
            self._log("Sequence complete — output off.")
        elif user:
            self._highlight_seq_step(-1)
            self.seq_status.setText("Stopped")
            self._log("Sequence stopped.")

    def _set_sequence_running(self, active: bool) -> None:
        self.seq_run_btn.setText("Stop" if active else "Run")
        self.seq_load_btn.setEnabled(not active)
        for widget in (self.v_set, self.a_set, self.apply_btn, self.preset_apply_btn,
                       self.output_btn, self.charge_btn, self.chem_combo,
                       self.cells_spin, self.capacity_spin, self.crate_spin):
            widget.setEnabled(not active)

    def _highlight_seq_step(self, index: int) -> None:
        """Highlight the active step (and scroll to it); -1 clears all."""
        for i in range(self.seq_list.count()):
            item = self.seq_list.item(i)
            if i == index:
                item.setBackground(QColor("#2d8cf0"))
                item.setForeground(QColor("white"))
                self.seq_list.scrollToItem(item)
            else:
                item.setBackground(QColor(0, 0, 0, 0))
                item.setForeground(QColor("#000000"))

    # --- Poller callbacks ---------------------------------------------------

    def on_reading(self, m: Measurement) -> None:
        self._last_measurement = m
        self.v_display.setText(f"{m.voltage:5.2f}")
        self.a_display.setText(f"{m.current:5.3f}")
        self.w_display.setText(f"{m.power:5.2f}")
        # The CV/CC badge only means something while the output is live.
        if self.output_on:
            if m.constant_current:
                self.mode_badge.setText("CONSTANT CURRENT (CC)")
                self.mode_badge.setStyleSheet(
                    "background:#19be6b; color:white; border-radius:4px;")
            else:
                self.mode_badge.setText("CONSTANT VOLTAGE (CV)")
                self.mode_badge.setStyleSheet(
                    "background:#2d8cf0; color:white; border-radius:4px;")

        # Only log to the graph while the output is actually on.
        if HAVE_PYQTGRAPH and self._logging and self.output_on:
            self._append_sample(m)
            self._update_plot()

        if self._charger is not None:
            self._advance_charge(m)

        # Over-current trip: the supply reaching its set current limit shows up
        # as CC mode. (Skipped during a managed charge, which is legitimately CC,
        # and during the power-up grace period after switching the output on.)
        if (self.output_on and self.ocp_enable.isChecked()
                and self._charger is None and not self._output_settling()
                and self._is_overcurrent(m.current)):
            self._oc_count += 1
            if self._oc_count >= 2:   # debounce brief glitches before tripping
                self._trip_overcurrent(m.current)
        else:
            self._oc_count = 0

    # --- Graph & logging ----------------------------------------------------

    def _append_sample(self, m: Measurement) -> None:
        self._mono.append(time.monotonic())
        self._wall.append(datetime.now())
        self._volts.append(m.voltage)
        self._amps.append(m.current)
        self._cc.append(m.constant_current)
        if len(self._mono) > MAX_LOG_POINTS:
            drop = len(self._mono) - MAX_LOG_POINTS
            del self._mono[:drop], self._wall[:drop]
            del self._volts[:drop], self._amps[:drop], self._cc[:drop]

    def _update_plot(self) -> None:
        if self.plot is None or not self._mono:
            return
        t0 = self._mono[0]
        rel = np.fromiter((t - t0 for t in self._mono), float, len(self._mono))
        self.v_curve.setData(rel, np.asarray(self._volts))
        self.a_curve.setData(rel, np.asarray(self._amps))
        if self._window_s is not None and rel[-1] > self._window_s:
            self.plot.setXRange(rel[-1] - self._window_s, rel[-1], padding=0.02)
        else:
            self.plot.setXRange(rel[0], max(rel[-1], rel[0] + 1), padding=0.02)

    def _on_window_changed(self, _index: int) -> None:
        self._window_s = self.window_combo.currentData()
        self._update_plot()

    def _on_curve_toggled(self, _checked: bool) -> None:
        self.v_curve.setVisible(self.show_v.isChecked())
        self.a_curve.setVisible(self.show_a.isChecked())
        self.plot.getPlotItem().getAxis("right").setVisible(self.show_a.isChecked())
        self.plot.getPlotItem().getAxis("left").setVisible(self.show_v.isChecked())

    def _on_pause_toggled(self, paused: bool) -> None:
        self._logging = not paused
        self.pause_btn.setText("Resume logging" if paused else "Pause logging")

    def clear_log(self) -> None:
        self._mono.clear(); self._wall.clear()
        self._volts.clear(); self._amps.clear(); self._cc.clear()
        if self.plot is not None:
            self.v_curve.clear()
            self.a_curve.clear()
        self._log("Graph log cleared.")

    def export_csv(self) -> None:
        if not self._wall:
            self._log("Nothing to export yet.")
            return
        default = f"psu_log_{datetime.now():%Y%m%d_%H%M%S}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export log", default, "CSV files (*.csv)")
        if not path:
            return
        t0 = self._mono[0]
        try:
            with open(path, "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["timestamp", "elapsed_s", "voltage_v", "current_a", "power_w", "mode"])
                for mono, wall, v, a, cc in zip(
                    self._mono, self._wall, self._volts, self._amps, self._cc
                ):
                    writer.writerow([
                        wall.isoformat(timespec="milliseconds"), f"{mono - t0:.3f}",
                        f"{v:.3f}", f"{a:.4f}", f"{v * a:.3f}", "CC" if cc else "CV",
                    ])
            self._log(f"Exported {len(self._wall)} samples to {path}.")
        except OSError as exc:
            self._log(f"Export failed: {exc}")

    def on_poll_failed(self, message: str) -> None:
        # Log sparingly — a transient read error shouldn't flood the log.
        if self.log.toPlainText()[-120:].find(message) == -1:
            self._log(f"Read error: {message}")

    # --- Helpers ------------------------------------------------------------

    def _require_connection(self) -> bool:
        if self.psu is None:
            self._log("Not connected.")
            return False
        return True

    def _reflect_output_button(self) -> None:
        self.output_btn.setChecked(self.output_on)
        if self.output_on:
            self.output_btn.setText("OUTPUT ON")
            self.output_btn.setStyleSheet("background:#19be6b; color:white;")
        else:
            self.output_btn.setText("OUTPUT OFF")
            self.output_btn.setStyleSheet("background:#c0392b; color:white;")

    def _set_connected(self, connected: bool) -> None:
        self.connect_btn.setText("Disconnect" if connected else "Connect")
        self.port_combo.setEnabled(not connected)
        self.refresh_btn.setEnabled(not connected)
        self.show_all_ports.setEnabled(not connected)
        for widget in (self.apply_btn, self.output_btn, self.v_set, self.a_set,
                       self.preset_apply_btn, self.charge_btn, self.chem_combo,
                       self.cells_spin, self.capacity_spin, self.crate_spin):
            widget.setEnabled(connected)
        self.seq_run_btn.setEnabled(connected and bool(self._sequence))
        self._reflect_output_button()

    def _reset_displays(self) -> None:
        for display in (self.v_display, self.a_display, self.w_display):
            display.setText("--.--")
        self.v_set_label.setText("—")
        self.a_set_label.setText("—")
        self.mode_badge.setText("—")
        self.mode_badge.setStyleSheet("color:#888;")

    def _log(self, message: str) -> None:
        self.log.appendPlainText(message)

    def closeEvent(self, event) -> None:
        self.disconnect()
        super().closeEvent(event)


def main() -> int:
    parser = argparse.ArgumentParser(description="LabSupplyControl — DC supply control panel")
    parser.add_argument("--version", action="version",
                        version=f"{APP_NAME} {APP_VERSION} (beta)")
    parser.add_argument("--max-voltage", type=float, default=30.0,
                        help="UI voltage limit (default 30 V, SPS-D305-232)")
    parser.add_argument("--max-current", type=float, default=5.0,
                        help="UI current limit (default 5 A, SPS-D305-232)")
    args = parser.parse_args()

    # Silence the harmless "Wayland does not support requestActivate" notice.
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.wayland=false")

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    # On Wayland the panel/taskbar icon is taken from the matching .desktop file
    # (installed by install.sh), matched via this desktop-file name / app_id.
    QApplication.setDesktopFileName(APP_ID)
    if os.path.exists(ICON_PATH):
        app.setWindowIcon(QIcon(ICON_PATH))
    panel = ControlPanel(args.max_voltage, args.max_current)
    panel.resize(880, 860)
    panel.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
