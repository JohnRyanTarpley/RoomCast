"""RoomCast main window."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox,
                               QDoubleSpinBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QRadioButton, QSlider, QSpinBox,
                               QSplitter, QStackedWidget, QTableWidget, QVBoxLayout, QWidget)

import dsp
from engine import Engine, MixSettings, SpeakerSettings
from room_view import LISTENER, RoomView

COLORS = ["#ff6b6b", "#4dabf7", "#51cf66", "#fcc419", "#cc5de8", "#ff922b", "#22b8cf", "#f783ac"]
MODES = ["surround", "frequency", "sync"]
CONFIG = Path(os.environ.get("APPDATA", Path.home())) / "RoomCast" / "settings.json"


@dataclass
class SpeakerCfg:
    name: str
    pos: list = field(default_factory=lambda: [0.0, 1.0, 1.2])
    volume: float = 1.0
    trim_ms: float = 0.0
    mute: bool = False
    lo: float = 20.0
    hi: float = 20000.0
    color: str = COLORS[0]
    placed: bool = False


def apply_theme(app):
    app.setStyle("Fusion")
    pal = QPalette()
    for role, col in [(QPalette.Window, "#1e2228"), (QPalette.WindowText, "#e3e6ea"),
                      (QPalette.Base, "#171a1f"), (QPalette.AlternateBase, "#1e2228"),
                      (QPalette.Text, "#e3e6ea"), (QPalette.Button, "#2a2f37"),
                      (QPalette.ButtonText, "#e3e6ea"), (QPalette.Highlight, "#3b82f6"),
                      (QPalette.HighlightedText, "#ffffff"), (QPalette.ToolTipBase, "#2a2f37"),
                      (QPalette.ToolTipText, "#e3e6ea"), (QPalette.PlaceholderText, "#7b8494")]:
        pal.setColor(role, QColor(col))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#6b7380"))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#6b7380"))
    app.setPalette(pal)
    app.setStyleSheet("""
        QGroupBox { border: 1px solid #343a44; border-radius: 6px; margin-top: 10px; padding-top: 6px; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #9aa4b2; }
        QLabel[hint="true"] { color: #8b94a3; }
        QPushButton#start { font-weight: bold; padding: 9px; background: #2563eb; border-radius: 6px; }
        QPushButton#start:checked { background: #dc2626; }
        QCheckBox::indicator, QRadioButton::indicator, QListWidget::indicator {
            width: 13px; height: 13px; border: 1px solid #7b8494; background: #171a1f; }
        QCheckBox::indicator, QListWidget::indicator { border-radius: 3px; }
        QRadioButton::indicator { border-radius: 7px; }
        QCheckBox::indicator:checked, QRadioButton::indicator:checked, QListWidget::indicator:checked {
            background: #3b82f6; border-color: #9cc2ff; }
        QProgressBar { border: 1px solid #343a44; border-radius: 3px; background: #171a1f; }
        QProgressBar::chunk { background: #51cf66; }
    """)


def hint(text):
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setProperty("hint", True)
    return lab


class BandView(QWidget):
    """Shows which slice of the spectrum each speaker plays."""

    def __init__(self, state):
        super().__init__()
        self.state = state
        self.setMinimumHeight(160)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#14171c"))
        left, right, top, bottom = 150, self.width() - 16, 12, self.height() - 26
        X = lambda f: left + (right - left) * (np.log10(max(f, 20)) - np.log10(20)) / 3.0
        p.setPen(QColor("#2b313a"))
        for f, lab in [(20, "20"), (50, "50"), (100, "100"), (200, "200"), (500, "500"), (1000, "1k"),
                       (2000, "2k"), (5000, "5k"), (10000, "10k"), (20000, "20k")]:
            x = int(X(f))
            p.setPen(QColor("#2b313a"))
            p.drawLine(x, top, x, bottom)
            p.setPen(QColor("#8b94a3"))
            p.drawText(x - 12, bottom + 16, lab)
        n = max(1, len(self.state.active))
        h = min(26.0, (bottom - top) / n - 4)
        for i, s in enumerate(self.state.active):
            y = top + 2 + i * (bottom - top) / n
            x0, x1 = X(s.lo), X(min(s.hi, 20000))
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(s.color))
            p.drawRoundedRect(int(x0), int(y), max(3, int(x1 - x0)), int(h), 4, 4)
            p.setPen(QColor("#d6dbe3"))
            p.drawText(8, int(y), left - 14, int(h), Qt.AlignVCenter | Qt.AlignRight,
                       self.fontMetrics().elidedText(s.name, Qt.ElideRight, left - 14))
        p.end()


class MainWindow(QMainWindow):
    def __init__(self, backend=None):
        super().__init__()
        self.setWindowTitle("RoomCast – multi-speaker Bluetooth audio")
        self.resize(1240, 800)
        self.speakers: dict[str, SpeakerCfg] = {}
        self.active: list[SpeakerCfg] = []
        self.checked: list[str] = []
        self.room = [5.0, 4.0, 2.5]
        self.listener = [0.0, 0.0, 1.2]
        self.mode = "sync"
        self.opts = {"master": 0.8, "base_ms": 120, "focus": 2.0, "delay_comp": True,
                     "level_comp": True, "upmix": True, "source": ""}
        self.outputs, self.sources = [], []
        self._loading = False
        self._load()

        self.backend, self.backend_error = backend, ""
        if backend is None:
            try:
                from backend import WasapiBackend
                self.backend = WasapiBackend()
            except Exception as e:      # not on Windows / package missing
                self.backend_error = str(e)
        self.engine = Engine(self.backend)

        self._build()
        self.refresh_devices()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(60)
        if self.backend is None:
            self.status.setText("Audio system unavailable: " + self.backend_error)

    # ---- persistence -------------------------------------------------
    def _load(self):
        try:
            d = json.loads(CONFIG.read_text("utf-8"))
            self.speakers = {k: SpeakerCfg(**v) for k, v in d["speakers"].items()}
            self.checked = d["checked"]
            self.room, self.listener, self.mode = d["room"], d["listener"], d["mode"]
            self.opts.update(d["opts"])
        except Exception:
            pass

    def _save(self):
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps({
                "speakers": {k: asdict(v) for k, v in self.speakers.items()},
                "checked": self.checked, "room": self.room, "listener": self.listener,
                "mode": self.mode, "opts": self.opts}, indent=1), "utf-8")
        except Exception:
            pass

    def closeEvent(self, e):
        self.engine.stop()
        self._save()
        e.accept()

    # ---- UI construction ---------------------------------------------
    def _build(self):
        # left column
        left = QVBoxLayout()
        g = QGroupBox("1 · Speakers")
        v = QVBoxLayout(g)
        v.addWidget(hint("Pair and connect each speaker in Windows Bluetooth settings, "
                         "then press Refresh and tick the ones to use."))
        self.dev_list = QListWidget()
        self.dev_list.itemChanged.connect(self._on_checked)
        v.addWidget(self.dev_list, 1)
        row = QHBoxLayout()
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh_devices)
        row.addWidget(b)
        b = QPushButton("Bluetooth settings…")
        b.clicked.connect(lambda: self._open_settings("ms-settings:bluetooth"))
        row.addWidget(b)
        v.addLayout(row)
        left.addWidget(g, 1)

        g = QGroupBox("2 · Audio source (what the PC is playing)")
        v = QVBoxLayout(g)
        self.src_combo = QComboBox()
        self.src_combo.currentIndexChanged.connect(self._on_source)
        v.addWidget(self.src_combo)
        v.addWidget(hint("Choose the device Windows is set to play through. It must not be one "
                         "of the ticked speakers, or the sound would feed back on itself."))
        b = QPushButton("Windows sound settings…")
        b.clicked.connect(lambda: self._open_settings("ms-settings:sound"))
        v.addWidget(b)
        left.addWidget(g)

        g = QGroupBox("3 · Mode")
        v = QVBoxLayout(g)
        self.mode_group = QButtonGroup(self)
        for i, text in enumerate(["Surround – place speakers in the room",
                                  "Frequency split – each speaker plays a band",
                                  "Sync – same music on every speaker"]):
            rb = QRadioButton(text)
            rb.setChecked(MODES[i] == self.mode)
            self.mode_group.addButton(rb, i)
            v.addWidget(rb)
        self.mode_group.idClicked.connect(self._on_mode)
        left.addWidget(g)

        g = QGroupBox("Output")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        row.addWidget(QLabel("Master volume"))
        self.master = QSlider(Qt.Horizontal)
        self.master.setRange(0, 100)
        self.master.setValue(int(self.opts["master"] * 100))
        self.master.valueChanged.connect(lambda x: self._opt("master", x / 100))
        row.addWidget(self.master)
        v.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Safety buffer"))
        self.base = QSpinBox()
        self.base.setRange(40, 500)
        self.base.setSuffix(" ms")
        self.base.setValue(int(self.opts["base_ms"]))
        self.base.setToolTip("Extra buffering before audio is sent to the speakers. "
                             "Raise it if you hear dropouts; lower it for less lag.")
        self.base.valueChanged.connect(lambda x: self._opt("base_ms", x))
        row.addWidget(self.base)
        row.addStretch()
        v.addLayout(row)
        left.addWidget(g)

        self.start_btn = QPushButton("Start")
        self.start_btn.setObjectName("start")
        self.start_btn.setCheckable(True)
        self.start_btn.clicked.connect(self._toggle)
        left.addWidget(self.start_btn)
        self.status = hint("Stopped")
        left.addWidget(self.status)
        lw = QWidget()
        lw.setLayout(left)
        lw.setFixedWidth(340)

        # mode pages
        self.stack = QStackedWidget()
        self.stack.addWidget(self._surround_page())
        self.stack.addWidget(self._frequency_page())
        self.stack.addWidget(self._sync_page())
        self.stack.setCurrentIndex(MODES.index(self.mode))

        # speaker table
        g = QGroupBox("Speakers in use")
        v = QVBoxLayout(g)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Speaker", "Volume", "Delay", "Mute", "Identify", "Level"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c, w in [(1, 170), (2, 100), (3, 50), (4, 70), (5, 150)]:
            self.table.setColumnWidth(c, w)
        v.addWidget(self.table)
        row = QHBoxLayout()
        self.click = QCheckBox("Calibration click (replaces the music)")
        self.click.toggled.connect(lambda on: setattr(self.engine, "click_on", on))
        row.addWidget(self.click)
        row.addWidget(hint("Bluetooth speakers each lag by a different amount. With the click on, "
                           "add Delay to the speakers that click first until you hear one click."), 1)
        v.addLayout(row)

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.stack)
        split.addWidget(g)
        split.setSizes([520, 240])
        central = QWidget()
        lay = QHBoxLayout(central)
        lay.addWidget(lw)
        lay.addWidget(split, 1)
        self.setCentralWidget(central)

    def _dspin(self, lo, hi, val, step=0.1, suffix=" m"):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setDecimals(2)
        s.setSuffix(suffix)
        s.setValue(val)
        return s

    def _surround_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        self.room_view = RoomView(self)
        self.room_view.changed.connect(self._positions_changed)
        self.room_view.selected.connect(lambda i: self._show_selected())
        v.addWidget(self.room_view, 1)

        row = QHBoxLayout()
        row.addWidget(QLabel("Room W × D × H"))
        self.room_spins = []
        for i in range(3):
            s = self._dspin(1.0, 30.0, self.room[i], 0.5)
            s.valueChanged.connect(lambda val, i=i: self._room_changed(i, val))
            self.room_spins.append(s)
            row.addWidget(s)
        row.addSpacing(16)
        self.sel_label = QLabel("Listener  X / Y / Z")
        row.addWidget(self.sel_label)
        self.pos_spins = []
        for i in range(3):
            s = self._dspin(-15.0, 15.0, 0.0, 0.1)
            s.valueChanged.connect(lambda val, i=i: self._pos_spin(i, val))
            self.pos_spins.append(s)
            row.addWidget(s)
        row.addStretch()
        b = QPushButton("Arrange in a circle")
        b.clicked.connect(lambda: self._arrange())
        row.addWidget(b)
        v.addLayout(row)

        row = QHBoxLayout()
        for key, text, tip in [
            ("delay_comp", "Distance delay", "Delay nearer speakers so sound from every speaker "
                                             "reaches the listener at the same moment."),
            ("level_comp", "Distance level", "Turn nearer speakers down so all are equally loud "
                                             "at the listener."),
            ("upmix", "Upmix stereo", "Derive centre and rear channels from stereo music. Real 5.1/7.1 "
                                      "sources are used as-is.")]:
            cb = QCheckBox(text)
            cb.setChecked(bool(self.opts[key]))
            cb.setToolTip(tip)
            cb.toggled.connect(lambda on, key=key: self._opt(key, on))
            row.addWidget(cb)
        row.addSpacing(16)
        row.addWidget(QLabel("Focus"))
        fs = QSlider(Qt.Horizontal)
        fs.setRange(5, 80)
        fs.setValue(int(self.opts["focus"] * 10))
        fs.setFixedWidth(160)
        fs.setToolTip("Low = each channel spreads over many speakers; high = each channel "
                      "sticks to the speakers nearest its direction.")
        fs.valueChanged.connect(lambda x: self._opt("focus", x / 10))
        row.addWidget(fs)
        row.addStretch()
        v.addLayout(row)
        return w

    def _frequency_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        self.band_view = BandView(self)
        v.addWidget(self.band_view, 1)
        self.band_table = QTableWidget(0, 3)
        self.band_table.setHorizontalHeaderLabels(["Speaker", "From", "To"])
        self.band_table.verticalHeader().setVisible(False)
        self.band_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.band_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        v.addWidget(self.band_table, 1)
        row = QHBoxLayout()
        b = QPushButton("Split evenly (lows → highs, in list order)")
        b.clicked.connect(self._auto_bands)
        row.addWidget(b)
        row.addWidget(hint("Bands may overlap or leave gaps. 20 Hz – 20000 Hz means the full range."), 1)
        v.addLayout(row)
        return w

    def _sync_page(self):
        w = QWidget()
        v = QVBoxLayout(w)
        lab = QLabel("Sync mode")
        f = lab.font()
        f.setPointSize(16)
        lab.setFont(f)
        v.addStretch()
        v.addWidget(lab, 0, Qt.AlignHCenter)
        t = hint("Every ticked speaker plays the same stereo music at the same time.\n"
                 "Use the Delay column below (with the calibration click) to line the speakers up.")
        t.setAlignment(Qt.AlignHCenter)
        v.addWidget(t)
        v.addStretch()
        return w

    # ---- devices -----------------------------------------------------
    def _open_settings(self, uri):
        try:
            os.startfile(uri)
        except Exception:
            QMessageBox.information(self, "RoomCast", "This button only works on Windows.")

    def refresh_devices(self):
        if self.engine.running:
            self._stop()
        self.outputs, self.sources = [], []
        if self.backend is not None:
            try:
                self.backend.refresh()
                self.outputs = self.backend.list_outputs()
                self.sources = self.backend.list_sources()
            except Exception as e:
                self.status.setText("Could not list audio devices: %s" % e)
        self._loading = True
        self.dev_list.clear()
        for d in self.outputs:
            it = QListWidgetItem(d["name"])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if d["name"] in self.checked else Qt.Unchecked)
            self.dev_list.addItem(it)
        self.src_combo.clear()
        for s in self.sources:
            self.src_combo.addItem("%s  (%d ch, %d Hz)" % (s["name"].replace(" [Loopback]", ""),
                                                           s["channels"], s["rate"]))
        want = self.opts["source"]
        names = [s["name"] for s in self.sources]
        if want not in names and self.backend is not None:
            default = self.backend.default_output_name()
            want = next((n for n in names if default and n.startswith(default)), "")
        if want in names:
            self.src_combo.setCurrentIndex(names.index(want))
        self._loading = False
        self._rebuild_active()

    def _on_source(self, i):
        if not self._loading and 0 <= i < len(self.sources):
            self.opts["source"] = self.sources[i]["name"]
            self._restart_if_running()

    def _on_checked(self, _):
        if self._loading:
            return
        self.checked = [self.dev_list.item(i).text() for i in range(self.dev_list.count())
                        if self.dev_list.item(i).checkState() == Qt.Checked]
        self._rebuild_active()
        self._restart_if_running()

    def _rebuild_active(self):
        present = [d["name"] for d in self.outputs]
        self.active = []
        for name in [n for n in present if n in self.checked]:
            cfg = self.speakers.get(name)
            if cfg is None:
                cfg = self.speakers[name] = SpeakerCfg(name=name)
            self.active.append(cfg)
        for i, s in enumerate(self.active):
            s.color = COLORS[i % len(COLORS)]
        self._arrange(only_new=True)
        self._fill_tables()
        self.room_view.sel = LISTENER
        self._show_selected()
        self.room_view.update()
        self.band_view.update()

    # ---- tables ------------------------------------------------------
    def _fill_tables(self):
        self.table.setRowCount(0)
        self.band_table.setRowCount(0)
        self.table.setRowCount(len(self.active))
        self.band_table.setRowCount(len(self.active))
        self.meters, self.band_spins = [], []
        for r, s in enumerate(self.active):
            name = QLabel("  <span style='color:%s'>●</span> %d · %s" % (s.color, r + 1, s.name))
            self.table.setCellWidget(r, 0, name)
            vol = QSlider(Qt.Horizontal)
            vol.setRange(0, 150)
            vol.setValue(int(s.volume * 100))
            vol.valueChanged.connect(lambda x, s=s: self._set(s, "volume", x / 100))
            self.table.setCellWidget(r, 1, vol)
            trim = QSpinBox()
            trim.setRange(0, 1500)
            trim.setSingleStep(5)
            trim.setSuffix(" ms")
            trim.setValue(int(s.trim_ms))
            trim.valueChanged.connect(lambda x, s=s: self._set(s, "trim_ms", float(x)))
            self.table.setCellWidget(r, 2, trim)
            mute = QCheckBox()
            mute.setChecked(s.mute)
            mute.toggled.connect(lambda on, s=s: self._set(s, "mute", on))
            self.table.setCellWidget(r, 3, mute)
            b = QPushButton("Beep")
            b.setToolTip("Plays a short tone on this speaker only (while running).")
            b.clicked.connect(lambda _, r=r: self.engine.beep(r))
            self.table.setCellWidget(r, 4, b)
            m = QProgressBar()
            m.setRange(0, 100)
            m.setTextVisible(False)
            self.table.setCellWidget(r, 5, m)
            self.meters.append(m)

            self.band_table.setCellWidget(r, 0, QLabel(
                "  <span style='color:%s'>●</span> %d · %s" % (s.color, r + 1, s.name)))
            for c, key in [(1, "lo"), (2, "hi")]:
                sp = QSpinBox()
                sp.setRange(20, 20000)
                sp.setSingleStep(50)
                sp.setSuffix(" Hz")
                sp.setValue(int(getattr(s, key)))
                sp.valueChanged.connect(lambda x, s=s, key=key: self._set_band(s, key, x))
                self.band_table.setCellWidget(r, c, sp)
                self.band_spins.append((s, key, sp))

    def _set(self, s, key, val):
        setattr(s, key, val)
        self._push()

    def _set_band(self, s, key, val):
        setattr(s, key, float(val))
        self.band_view.update()
        self._push()

    def _auto_bands(self):
        for s, (lo, hi) in zip(self.active, dsp.auto_bands(len(self.active))):
            s.lo, s.hi = float(lo), float(hi)
        for s, key, sp in self.band_spins:
            sp.blockSignals(True)
            sp.setValue(int(getattr(s, key)))
            sp.blockSignals(False)
        self.band_view.update()
        self._push()

    # ---- surround editing ---------------------------------------------
    def _arrange(self, only_new=False):
        n = len(self.active)
        if not n:
            return
        W, D, H = self.room
        rad = 0.42 * min(W, D)
        for k, s in enumerate(self.active):
            if only_new and s.placed:
                continue
            az = 0.0 if n == 1 else (30.0 * (2 * k - 1) if n == 2 else -180 + 360 * (k + 0.5) / n)
            a = np.radians(az)
            s.pos[:] = [float(np.clip(self.listener[0] + rad * np.sin(a), -W / 2, W / 2)),
                        float(np.clip(self.listener[1] + rad * np.cos(a), -D / 2, D / 2)),
                        min(1.2, H)]
            s.placed = False
        if hasattr(self, "room_view"):
            self._show_selected()
            self._push()
            self.room_view.update()

    def _mark_placed(self):
        i = self.room_view.sel
        if 0 <= i < len(self.active):
            self.active[i].placed = True      # moved by hand: keep it where it is

    def _selected_pos(self):
        i = self.room_view.sel
        if i == LISTENER or i >= len(self.active):
            return "Listener", self.listener
        return "Speaker %d" % (i + 1), self.active[i].pos

    def _show_selected(self):
        label, pos = self._selected_pos()
        self.sel_label.setText(label + "  X / Y / Z")
        self._loading = True
        for sp, val in zip(self.pos_spins, pos):
            sp.setValue(val)
        self._loading = False

    def _positions_changed(self):
        self._mark_placed()
        self._show_selected()
        self._push()

    def _pos_spin(self, i, val):
        if self._loading:
            return
        _, pos = self._selected_pos()
        pos[i] = val
        pos[:] = self.room_view._clamp(pos)
        self._mark_placed()
        self.room_view.update()
        self._push()

    def _room_changed(self, i, val):
        self.room[i] = val
        for pos in [self.listener] + [s.pos for s in self.active]:
            pos[:] = self.room_view._clamp(pos)
        self._show_selected()
        self.room_view.update()
        self._push()

    # ---- engine control ------------------------------------------------
    def _opt(self, key, val):
        self.opts[key] = val
        self._push()

    def _on_mode(self, i):
        self.mode = MODES[i]
        self.stack.setCurrentIndex(i)
        self._push()

    def mix(self) -> MixSettings:
        o = self.opts
        return MixSettings(
            mode=self.mode, master=o["master"], base_ms=o["base_ms"], listener=tuple(self.listener),
            focus=o["focus"], delay_comp=o["delay_comp"], level_comp=o["level_comp"], upmix=o["upmix"],
            speakers=[SpeakerSettings(tuple(s.pos), s.volume, s.mute, s.trim_ms, s.lo, s.hi)
                      for s in self.active])

    def _push(self):
        if self.engine.running:
            self.engine.apply(self.mix())

    def _toggle(self, on):
        if on:
            self._start()
        else:
            self._stop()

    def _fail(self, msg):
        self.start_btn.setChecked(False)
        self.start_btn.setText("Start")
        QMessageBox.warning(self, "RoomCast", msg)

    def _start(self):
        if self.backend is None:
            return self._fail("The Windows audio system could not be loaded:\n" + self.backend_error)
        if not self.active:
            return self._fail("Tick at least one speaker first.")
        i = self.src_combo.currentIndex()
        if not 0 <= i < len(self.sources):
            return self._fail("No audio source found. Press Refresh.")
        src = self.sources[i]
        if any(src["name"].startswith(s.name) for s in self.active):
            return self._fail(
                "The audio source is also ticked as a speaker, which would create a feedback loop.\n\n"
                "Set Windows to play through a device you are NOT using as a speaker (for example the "
                "PC's built-in output turned all the way down, or a virtual cable), choose that device "
                "as the source, and try again.")
        by_name = {d["name"]: d for d in self.outputs}
        try:
            self.engine.start(src, [by_name[s.name] for s in self.active], self.mix())
        except Exception as e:
            return self._fail("Could not open the audio devices:\n%s\n\nPress Refresh and check that "
                              "every ticked speaker is still connected." % e)
        self.engine.click_on = self.click.isChecked()
        self.start_btn.setChecked(True)
        self.start_btn.setText("Stop")
        self._save()

    def _stop(self):
        self.engine.stop()
        self.start_btn.setChecked(False)
        self.start_btn.setText("Start")
        self.status.setText("Stopped")
        for m in getattr(self, "meters", []):
            m.setValue(0)

    def _restart_if_running(self):
        if self.engine.running:
            self._stop()
            self._start()

    def _tick(self):
        if not self.engine.running:
            return
        for m, lv in zip(self.meters, self.engine.levels()):
            db = 20 * np.log10(max(lv, 1e-4))
            m.setValue(int(np.clip((db + 60) / 60 * 100, 0, 100)))
        self.status.setText("Running · %d speaker(s) · source %d ch @ %d Hz · dropouts: %d" % (
            len(self.active), self.engine.n_in, self.engine.src_rate, self.engine.underruns()))
