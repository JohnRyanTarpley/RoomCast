"""RoomCast main window."""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from PySide6.QtCore import QByteArray, QEvent, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox,
                               QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QLayout, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                               QFrame, QProgressBar, QPushButton, QRadioButton, QScrollArea,
                               QSizePolicy, QSlider,
                               QSpinBox, QSplitter, QStackedWidget, QTableWidget, QVBoxLayout,
                               QWidget)

import dsp
from connection import DeviceMonitor, PairDialog, bt
from engine import TONE_TOTAL_S, Engine, MixSettings, SpeakerSettings, open_tone_stream
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
        QSplitter::handle { background: #262b33; }
        QSplitter::handle:hover { background: #3b82f6; }
        QSplitter::handle:horizontal { width: 6px; }
        QSplitter::handle:vertical { height: 6px; }
    """)


def hint(text):
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setProperty("hint", True)
    return lab


GREEN, AMBER, RED, GREY = "#51cf66", "#fcc419", "#ff6b6b", "#8b94a3"


class ElidedCheckBox(QCheckBox):
    """Check box whose label shortens with "…" to fit, so its column can be resized freely."""

    def __init__(self, text):
        super().__init__(text)
        self.full = text
        self.setToolTip(text)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setMinimumWidth(60)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        room = max(20, self.width() - 24)        # minus the tick box itself
        self.setText(self.fontMetrics().elidedText(self.full, Qt.ElideRight, room))


def hbox():
    """A horizontal row packed into its own widget: returns (widget, layout)."""
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    return w, lay


class FlowLayout(QLayout):
    """Lays widgets out left to right and wraps onto a new line when out of room."""

    def __init__(self, parent=None, hspace=14, vspace=6):
        super().__init__(parent)
        self.setContentsMargins(0, 4, 0, 0)
        self._items, self._h, self._v = [], hspace, vspace

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._place(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _place(self, rect, apply):
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line = r.x(), r.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            if x > r.x() and x + hint.width() > r.right() + 1:
                x, y, line = r.x(), y + line + self._v, 0
            if apply:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._h
            line = max(line, hint.height())
        return y + line - rect.y() + m.bottom()


def split(orientation, *widgets, sizes=None):
    s = QSplitter(orientation)
    s.setChildrenCollapsible(False)
    for w in widgets:
        s.addWidget(w)
    if sizes:
        s.setSizes(sizes)
    return s


class SpeakerRow(QWidget):
    """One speaker in the device list: tick box, battery, connection state and
    a Reconnect or Disconnect button for Bluetooth speakers."""

    def __init__(self, name, checked, on_toggle, on_reconnect, on_disconnect):
        super().__init__()
        self.name = name
        g = QGridLayout(self)
        g.setContentsMargins(6, 4, 6, 4)
        g.setHorizontalSpacing(6)
        g.setVerticalSpacing(1)
        self.check = ElidedCheckBox(name)
        self.check.setChecked(checked)
        self.check.toggled.connect(lambda on: on_toggle(name, on))
        g.addWidget(self.check, 0, 0)
        self.battery = QLabel()
        g.addWidget(self.battery, 0, 1, Qt.AlignRight)
        self.state = QLabel()
        self.state.setWordWrap(True)
        self.state.setContentsMargins(21, 0, 0, 0)
        f = self.state.font()
        f.setPointSizeF(f.pointSizeF() * 0.9)
        self.state.setFont(f)
        g.addWidget(self.state, 1, 0)
        self.btn = QPushButton("Reconnect")
        self.btn.setToolTip("Ask Windows to connect this speaker again.")
        self.btn.clicked.connect(lambda: on_reconnect(name))
        self.btn.hide()
        g.addWidget(self.btn, 1, 1, Qt.AlignRight)
        self.dis_btn = QPushButton("Disconnect")
        self.dis_btn.setToolTip("Disconnect this speaker from the PC (it stays paired). "
                                "RoomCast won't reconnect it until you press Reconnect.")
        self.dis_btn.clicked.connect(lambda: on_disconnect(name))
        self.dis_btn.hide()
        g.addWidget(self.dis_btn, 1, 1, Qt.AlignRight)
        g.setColumnStretch(0, 1)

    def set_state(self, text, color, reconnect=False, reconnect_enabled=True, disconnect=False):
        self.state.setText("<span style='color:%s'>●</span> %s" % (color, text))
        self.btn.setVisible(reconnect)
        self.btn.setEnabled(reconnect_enabled)
        self.dis_btn.setVisible(disconnect and not reconnect)

    def set_battery(self, pct):
        if pct is None:
            self.battery.clear()
            self.battery.setToolTip("")
            return
        color = RED if pct <= 20 else (AMBER if pct <= 40 else None)
        text = "🔋 %d%%" % pct
        self.battery.setText("<span style='color:%s'>%s</span>" % (color, text) if color else text)
        self.battery.setToolTip("Battery level reported by the speaker")


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
        self.speakers: dict[str, SpeakerCfg] = {}
        self.active: list[SpeakerCfg] = []
        self.checked: list[str] = []
        self.room = [5.0, 4.0, 2.5]
        self.listener = [0.0, 0.0, 1.2]
        self.mode = "sync"
        self.opts = {"master": 0.8, "base_ms": 120, "focus": 2.0, "delay_comp": True,
                     "level_comp": True, "upmix": True, "source": "", "auto_reconnect": True}
        self.outputs, self.sources = [], []
        self._loading = False
        self._load()

        # connection tracking
        self.endpoints = {}          # endpoint name -> info from bluetooth.list_endpoints()
        self.battery = {}            # container id -> battery %
        self.rows = {}               # speaker name -> SpeakerRow
        self.dropped = {}            # speaker name -> {"t", "gone", "hold", "rejoining"}: left out until it returns
        self.drop_history = {}       # speaker name -> recent drop times (for back-off)
        self.rejoin_fails = {}       # speaker name -> failed in-place rejoins in a row
        self.last_rescan = {}        # speaker name -> last full restart made to bring it back
        self.reconnecting = {}       # speaker name -> time a manual Reconnect was pressed
        self.reconnect_failed = {}   # speaker name -> time it failed
        self.disconnecting = {}      # speaker name -> time Disconnect was pressed
        self.user_disconnected = set()   # disconnected on purpose: don't auto-reconnect them
        self.last_auto = {}          # speaker name -> time of last automatic reconnect attempt
        self._tone_streams = []      # identify tones playing while stopped
        self._notice = ("", 0.0)     # short message kept in the status line until a time

        self.backend, self.backend_error = backend, ""
        if backend is None:
            try:
                from backend import WasapiBackend
                self.backend = WasapiBackend()
            except Exception as e:      # not on Windows / package missing
                self.backend_error = str(e)
        self.engine = Engine(self.backend)

        self._build()
        self._restart_timer = QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.timeout.connect(self._auto_restart)
        self.refresh_devices()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(60)
        self.monitor = DeviceMonitor()
        self.monitor.updated.connect(self._on_monitor)
        self.monitor.reconnect_done.connect(self._on_reconnect_done)
        self.monitor.disconnect_done.connect(self._on_disconnect_done)
        self.monitor.start()
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
        self.monitor.stop()
        self._close_tones()
        self.engine.stop()
        self._save_layout()
        self._save()
        e.accept()

    def _save_layout(self):
        b64 = lambda ba: bytes(ba.toBase64()).decode("ascii")
        self.opts["layout"] = {"window": b64(self.saveGeometry()),
                               **{k: b64(s.saveState()) for k, s in self.splitters.items()}}

    def _restore_layout(self):
        saved = self.opts.get("layout") or {}
        restore = lambda s: QByteArray.fromBase64(s.encode("ascii"))
        try:
            if not self.restoreGeometry(restore(saved["window"])):
                raise ValueError
        except Exception:
            self.resize(1240, 800)
        for k, s in self.splitters.items():
            if k in saved:
                try:
                    s.restoreState(restore(saved[k]))
                except Exception:
                    pass

    def eventFilter(self, obj, e):
        if e.type() == QEvent.Resize:
            try:
                if obj is self.dev_list.viewport():
                    self._fit_rows()
            except RuntimeError:        # window is being torn down
                pass
        return super().eventFilter(obj, e)

    def _fit_rows(self):
        """Give each speaker row the height it needs at the list's current width
        (status text wraps onto more lines when the column is narrow)."""
        w = self.dev_list.viewport().width()
        for i in range(self.dev_list.count()):
            it = self.dev_list.item(i)
            row = self.dev_list.itemWidget(it)
            if row is not None:
                h = row.layout().totalHeightForWidth(w)
                it.setSizeHint(QSize(w, h if h > 0 else row.sizeHint().height()))

    # ---- UI construction ---------------------------------------------
    def _build(self):
        # left column: speaker list | (source, mode, output) in a draggable split,
        # with Start and the status line always at the bottom
        settings = QVBoxLayout()
        settings.setContentsMargins(0, 0, 0, 0)
        g = QGroupBox("1 · Speakers")
        v = QVBoxLayout(g)
        v.addWidget(hint("Tick the speakers to use. Paired Bluetooth speakers that are switched off "
                         "are listed too, and join in automatically when they connect."))
        self.dev_list = QListWidget()
        self.dev_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.dev_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.dev_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.dev_list.setMinimumHeight(70)
        self.dev_list.viewport().installEventFilter(self)
        v.addWidget(self.dev_list, 1)
        b = QPushButton("Add a Bluetooth speaker…")
        b.setToolTip("Find and pair a new speaker without leaving RoomCast.")
        b.clicked.connect(self._add_speaker)
        v.addWidget(b)
        row = QHBoxLayout()
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh_devices)
        row.addWidget(b)
        b = QPushButton("Bluetooth settings…")
        b.clicked.connect(lambda: self._open_settings("ms-settings:bluetooth"))
        row.addWidget(b)
        v.addLayout(row)
        cb = QCheckBox("Auto-reconnect ticked speakers")
        cb.setToolTip("While RoomCast is playing, it asks Windows to reconnect any ticked Bluetooth "
                      "speaker that has disconnected, every 30 seconds.")
        cb.setChecked(bool(self.opts["auto_reconnect"]))
        cb.toggled.connect(lambda on: self.opts.__setitem__("auto_reconnect", on))
        v.addWidget(cb)
        speakers_box = g
        left = settings

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
        left.addStretch()
        settings_w = QWidget()
        settings_w.setLayout(settings)
        settings_scroll = QScrollArea()       # scrolls when the window is short
        settings_scroll.setWidget(settings_w)
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.NoFrame)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        settings_scroll.setMinimumHeight(80)
        self.left_split = split(Qt.Vertical, speakers_box, settings_scroll, sizes=[400, 340])
        self.left_split.setStretchFactor(0, 1)

        bottom = QVBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addWidget(self.left_split, 1)
        self.start_btn = QPushButton("Start")
        self.start_btn.setObjectName("start")
        self.start_btn.setCheckable(True)
        self.start_btn.clicked.connect(self._toggle)
        bottom.addWidget(self.start_btn)
        self.status = hint("Stopped")
        bottom.addWidget(self.status)
        lw = QWidget()
        lw.setLayout(bottom)
        # never narrower than the settings need, so no label gets cut off
        sb = settings_scroll.verticalScrollBar().sizeHint().width()
        lw.setMinimumWidth(max(260, settings_w.minimumSizeHint().width() + sb + 4))

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
        self.click = QCheckBox("Calibration click (replaces the music)")
        self.click.toggled.connect(lambda on: setattr(self.engine, "click_on", on))
        v.addWidget(self.click)
        v.addWidget(hint("Bluetooth speakers each lag by a different amount. With the click on, "
                         "add Delay to the speakers that click first until you hear one click."))

        self.right_split = split(Qt.Vertical, self.stack, g, sizes=[520, 240])
        self.right_split.setStretchFactor(0, 1)
        self.main_split = split(Qt.Horizontal, lw, self.right_split, sizes=[340, 900])
        self.main_split.setStretchFactor(1, 1)
        central = QWidget()
        lay = QHBoxLayout(central)
        lay.addWidget(self.main_split)
        self.setCentralWidget(central)
        self.splitters = {"main": self.main_split, "left": self.left_split, "right": self.right_split}
        self._restore_layout()

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

        # Controls wrap onto more lines when the window is narrow.
        controls = QWidget()
        flow = FlowLayout(controls)
        row_w, row = hbox()
        row.addWidget(QLabel("Room W × D × H"))
        self.room_spins = []
        for i in range(3):
            s = self._dspin(1.0, 30.0, self.room[i], 0.5)
            s.valueChanged.connect(lambda val, i=i: self._room_changed(i, val))
            self.room_spins.append(s)
            row.addWidget(s)
        flow.addWidget(row_w)
        row_w, row = hbox()
        self.sel_label = QLabel("Listener  X / Y / Z")
        row.addWidget(self.sel_label)
        self.pos_spins = []
        for i in range(3):
            s = self._dspin(-15.0, 15.0, 0.0, 0.1)
            s.valueChanged.connect(lambda val, i=i: self._pos_spin(i, val))
            self.pos_spins.append(s)
            row.addWidget(s)
        flow.addWidget(row_w)
        b = QPushButton("Arrange in a circle")
        b.clicked.connect(lambda: self._arrange())
        flow.addWidget(b)

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
            flow.addWidget(cb)
        row_w, row = hbox()
        row.addWidget(QLabel("Focus"))
        fs = QSlider(Qt.Horizontal)
        fs.setRange(5, 80)
        fs.setValue(int(self.opts["focus"] * 10))
        fs.setFixedWidth(160)
        fs.setToolTip("Low = each channel spreads over many speakers; high = each channel "
                      "sticks to the speakers nearest its direction.")
        fs.valueChanged.connect(lambda x: self._opt("focus", x / 10))
        row.addWidget(fs)
        flow.addWidget(row_w)
        v.addWidget(controls)
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
        b = QPushButton("Split evenly")
        b.setToolTip("Give each speaker an equal slice, lows → highs, in list order.")
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

    # Speakers are matched by their Windows name ("Speakers (JBL Flip 5)"), which is the same
    # in the audio library's device list and in bluetooth.list_endpoints().

    def _sync_endpoints(self):
        if bt is None:
            return
        try:
            bt.com_init(single_threaded=True)
            self.endpoints = self._index(bt.list_endpoints())
            if not self.battery:
                self.battery = bt.battery_levels()
        except OSError:
            pass

    @staticmethod
    def _index(endpoints):
        res = {}
        for e in endpoints:            # if two share a name, prefer the connected one
            if e["name"] not in res or e["connected"]:
                res[e["name"]] = e
        return res

    def _is_hands_free(self, name):
        e = self.endpoints.get(name)
        return e["hands_free"] if e else "hands-free" in name.lower()

    def _playable(self):
        """Names the audio library can open right now (hands-free outputs excluded)."""
        return [d["name"] for d in self.outputs if not self._is_hands_free(d["name"])]

    def _list_names(self):
        names = self._playable()
        for e in self.endpoints.values():     # connected since the last refresh, or paired but off
            if not e["hands_free"] and e["name"] not in names:
                names.append(e["name"])
        return names

    def _hands_free_only(self, e):
        """True if the speaker's normal output is down but its call-mode output is up."""
        c = e.get("container")
        return bool(c) and not c.startswith("{00000000-") and any(
            x["hands_free"] and x["connected"] and x["container"] == c for x in self.endpoints.values())

    def refresh_devices(self):
        """Re-scan devices. If playback is on, it continues with the new list."""
        resume = self.start_btn.isChecked()
        self._close_tones()             # the device re-scan below needs every stream closed
        self.engine.stop()
        self._sync_endpoints()
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
        self.rows = {}
        self._add_new_rows()
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
        if resume:
            self._start(quiet=True)
        self._update_rows()

    def _add_new_rows(self):
        """Add a row for every listable speaker that doesn't have one yet."""
        for name in self._list_names():
            if name in self.rows:
                continue
            row = SpeakerRow(name, name in self.checked, self._on_toggle, self._reconnect,
                             self._disconnect)
            it = QListWidgetItem()
            it.setFlags(Qt.ItemIsEnabled)
            it.setSizeHint(row.sizeHint())
            self.dev_list.addItem(it)
            self.dev_list.setItemWidget(it, row)
            self.rows[name] = row

    def _on_source(self, i):
        if not self._loading and 0 <= i < len(self.sources):
            self.opts["source"] = self.sources[i]["name"]
            self._restart_if_running()

    def _on_toggle(self, name, on):
        if self._loading:
            return
        if on and name not in self.checked:
            self.checked.append(name)
        elif not on and name in self.checked:
            self.checked.remove(name)
        self.dropped.pop(name, None)
        if self.start_btn.isChecked():
            self._schedule_restart(150)     # also picks up speakers connected since the last refresh
        else:
            self._rebuild_active()
        self._update_rows()

    def _rebuild_active(self):
        present = [n for n in self._playable() if n not in self.dropped]
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
            b.setToolTip("Plays a short tone on this speaker only, to check which one it is.")
            b.clicked.connect(lambda _, name=s.name: self._beep(name))
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

    def _fail(self, msg, quiet=False):
        self.start_btn.setChecked(False)
        self.start_btn.setText("Start")
        if quiet:
            self.status.setText("Stopped: " + msg.split("\n")[0])
        else:
            QMessageBox.warning(self, "RoomCast", msg)

    def _set_running_ui(self):
        self.start_btn.setChecked(True)
        self.start_btn.setText("Stop")

    def _start(self, quiet=False):
        """quiet=True for automatic restarts: report problems in the status line, no pop-ups."""
        if self.backend is None:
            return self._fail("The Windows audio system could not be loaded:\n" + self.backend_error, quiet)
        if not self.active:
            if not self.checked:
                return self._fail("Tick at least one speaker first.", quiet)
            self._set_running_ui()      # keep "on" and wait for a ticked speaker to connect
            self.status.setText("Waiting for a ticked speaker to connect…")
            self._update_rows()
            return
        i = self.src_combo.currentIndex()
        if not 0 <= i < len(self.sources):
            return self._fail("No audio source found. Press Refresh.", quiet)
        src = self.sources[i]
        if any(src["name"].startswith(s.name) for s in self.active):
            return self._fail(
                "The audio source is also ticked as a speaker, which would create a feedback loop.\n\n"
                "Set Windows to play through a device you are NOT using as a speaker (for example the "
                "PC's built-in output turned all the way down, or a virtual cable), choose that device "
                "as the source, and try again.", quiet)
        by_name = {d["name"]: d for d in self.outputs}
        try:
            failed = self.engine.start(src, [by_name[s.name] for s in self.active], self.mix())
        except Exception as e:
            if quiet:                   # e.g. every speaker vanished mid-restart: retry shortly
                for s in self.active:
                    self._mark_dropped(s.name)
                self._set_running_ui()
                self.status.setText("Could not open the speakers, retrying…")
                self._schedule_restart(3000)
                return
            return self._fail("Could not open the audio devices:\n%s\n\nPress Refresh and check that "
                              "every ticked speaker is still connected." % e)
        self.engine.click_on = self.click.isChecked()
        self._set_running_ui()
        if failed:                      # play on the others; these rejoin by themselves later
            for k in failed:
                self._mark_dropped(self.active[k].name)
        self._update_rows()
        self._save()
        self._keep_source_default()

    def _keep_source_default(self):
        """When a Bluetooth speaker connects (especially right after pairing),
        Windows makes it the default output. Apps then play straight to that
        speaker and the source RoomCast records from goes silent: music stops
        while the calibration click (made inside RoomCast) still works. If the
        default is one of RoomCast's own speakers, point it back at the source."""
        if bt is None or not self.engine.running:
            return
        i = self.src_combo.currentIndex()
        if not 0 <= i < len(self.sources):
            return
        src_name = self.sources[i]["name"].replace(" [Loopback]", "")
        try:
            default = bt.default_output()
        except OSError:
            return
        if (default is None or default["name"] == src_name
                or default["name"] not in [s.name for s in self.active]):
            return
        src = self.endpoints.get(src_name)
        if src is None or not src["connected"]:
            return
        try:
            bt.set_default_output(src["id"])
        except OSError:
            self._flash("Windows is sending sound straight to %s, so RoomCast hears nothing. In Windows "
                        "sound settings, set the output back to %s." % (default["name"], src_name), 15)
            return
        self._flash("Windows had switched its sound output to %s (it does that when a speaker "
                    "connects). Switched it back to %s so your music plays through RoomCast."
                    % (default["name"], src_name), 10)

    def _stop(self):
        self._restart_timer.stop()
        self.engine.stop()
        self.dropped.clear()
        self.rejoin_fails.clear()
        self.start_btn.setChecked(False)
        self.start_btn.setText("Start")
        self.status.setText("Stopped")
        for m in getattr(self, "meters", []):
            m.setValue(0)
        self._update_rows()

    def _restart_if_running(self):
        if self.start_btn.isChecked():
            self._schedule_restart(150)

    def _schedule_restart(self, ms):
        """Restart playback (re-scanning devices) shortly. Repeated calls are merged."""
        self._restart_timer.start(ms)

    def _auto_restart(self):
        if self.start_btn.isChecked():
            self.refresh_devices()

    # ---- dropouts and reconnecting --------------------------------------
    def _mark_dropped(self, name):
        """Leave a speaker out of playback until it comes back. A speaker that keeps
        dropping out is left out for longer each time (10 s, 20 s, … up to 2 min)."""
        now = time.monotonic()
        hist = [t for t in self.drop_history.get(name, []) if now - t < 300] + [now]
        self.drop_history[name] = hist
        self.dropped[name] = {"t": now, "gone": False, "hold": min(10 * 2 ** (len(hist) - 1), 120)}

    def _drop_now(self, name, gone=False):
        """Take one speaker out of playback; the others keep playing."""
        i = self.engine.index_of(name)
        if i is not None:
            self.engine.drop(i)                 # surround re-pans onto the others by itself
        self._mark_dropped(name)
        self.dropped[name]["gone"] = gone

    def _schedule_rejoin(self, name, ms):
        d = self.dropped.setdefault(name, {"t": time.monotonic(), "gone": False, "hold": 10})
        d["gone"], d["rejoining"] = False, True
        QTimer.singleShot(ms, lambda: self._try_rejoin(name))

    def _try_rejoin(self, name):
        """Put a returning speaker back into playback without interrupting the
        others. Falls back to restarting playback when that isn't possible
        (e.g. it wasn't part of this playback session)."""
        if not self.start_btn.isChecked():
            self.dropped.pop(name, None)
            return
        e = self.endpoints.get(name)
        if e is not None and not e["connected"]:    # still off: wait until Windows sees it again
            d = self.dropped.setdefault(name, {"t": time.monotonic(), "hold": 10})
            d["gone"], d["rejoining"] = True, False
            self._update_rows()
            return
        i = self.engine.index_of(name) if self.engine.running else None
        if i is not None:
            self.engine.drop(i)                 # a stream left over from before it disconnected
            if self.engine.rejoin(i):
                self.dropped.setdefault(name, {"t": time.monotonic(), "gone": False, "hold": 10})
                self.dropped[name]["rejoining"] = True
                self._update_rows()
                return
        self.dropped.pop(name, None)
        self._schedule_restart(500)

    def _check_dropouts(self):
        now = time.monotonic()
        if self.engine.running:
            stalled = self.engine.stalled()
            for i in stalled:
                self._drop_now(self.engine.outs[i].name)
            changed = bool(stalled)
            for i, ok in self.engine.poll_rejoined():
                changed = True
                name = self.engine.outs[i].name
                if ok:
                    self.dropped.pop(name, None)
                    self.rejoin_fails.pop(name, None)
                    continue
                fails = self.rejoin_fails[name] = self.rejoin_fails.get(name, 0) + 1
                gone = self.dropped.get(name, {}).get("gone", False)
                if fails == 1:                  # try again shortly
                    self.dropped[name] = {"t": now, "gone": gone, "hold": 3}
                elif now - self.last_rescan.get(name, -1e9) > 300:
                    # Its device entry may be out of date. Re-scanning needs a short
                    # restart of every speaker, so allow it only now and then.
                    self.last_rescan[name] = now
                    del self.rejoin_fails[name]
                    self.dropped.pop(name, None)
                    self._schedule_restart(500)
                else:                           # keep trying now and then, without a restart
                    self._mark_dropped(name)
                    self.dropped[name]["gone"] = gone
            if changed:
                self._update_rows()
        for name, d in list(self.dropped.items()):
            if not d["gone"] and not d.get("rejoining") and now - d["t"] > d["hold"]:
                self._try_rejoin(name)          # never actually disconnected: try it again

    def _on_monitor(self, endpoints, battery):
        old = self.endpoints
        self.endpoints, self.battery = self._index(endpoints), battery
        now = time.monotonic()

        def shape(d):
            return {(n, e["connected"], e["hands_free"]) for n, e in d.items()}

        def up(d):
            return {n for n, e in d.items() if e["connected"] and not e["hands_free"]}

        returned = set()
        for name, d in list(self.dropped.items()):
            e = self.endpoints.get(name)
            if e is None:
                continue                        # no connection info: the time-out retries it
            if not e["connected"]:
                d["gone"] = True
            elif d["gone"]:
                returned.add(name)              # back again
        for name, t in list(self.disconnecting.items()):
            e = self.endpoints.get(name)
            if e is None or not e["connected"] or now - t > 15:
                del self.disconnecting[name]
        for name in list(self.user_disconnected):
            e = self.endpoints.get(name)
            if e is not None and e["connected"] and name not in self.disconnecting:
                self.user_disconnected.discard(name)    # connected again some other way
        for name, t in list(self.reconnecting.items()):
            e = self.endpoints.get(name)
            if e is not None and e["connected"]:
                del self.reconnecting[name]
            elif now - t > 20:
                del self.reconnecting[name]
                self.reconnect_failed[name] = now

        if self.start_btn.isChecked():
            # Speakers leaving or returning are handled one at a time so the
            # others keep playing. Anything else (another speaker appearing, a
            # hands-free endpoint coming and going because a phone connected to
            # a speaker) doesn't touch playback at all.
            checked = set(self.checked)
            came_up = ((up(self.endpoints) - up(old)) & checked) | returned
            for name in (up(old) - up(self.endpoints)) & checked:
                self._drop_now(name, gone=True)
            for name in came_up:
                i = self.engine.index_of(name)
                if name not in returned and i is not None and self.engine.is_open(i):
                    continue                        # already playing (e.g. first poll after launch)
                self._schedule_rejoin(name, 2000)   # let a fresh Bluetooth link settle first
            if shape(old) != shape(self.endpoints):
                self._add_new_rows()                # ticking one restarts playback to open it
        elif shape(old) != shape(self.endpoints):
            self.refresh_devices()

        if self.start_btn.isChecked() and self.opts["auto_reconnect"]:
            for name in self.checked:
                e = self.endpoints.get(name)
                if (e and e["bluetooth"] and not e["connected"] and name not in self.reconnecting
                        and name not in self.user_disconnected
                        and now - self.last_auto.get(name, -1e9) > 30):
                    self.last_auto[name] = now
                    self.monitor.request_reconnect(e["id"], name)
        self._keep_source_default()
        self._update_rows()

    def _reconnect(self, name):
        e = self.endpoints.get(name)
        if e is None:
            return
        self.reconnecting[name] = time.monotonic()
        self.reconnect_failed.pop(name, None)
        self.user_disconnected.discard(name)
        self.monitor.request_reconnect(e["id"], name)
        self._update_rows()

    def _disconnect(self, name):
        """Disconnect a Bluetooth speaker from the PC (it stays paired). If it is
        playing, the other speakers carry on without it."""
        e = self.endpoints.get(name)
        if e is None or not e["connected"]:
            return
        self.disconnecting[name] = time.monotonic()
        self.user_disconnected.add(name)
        self.monitor.request_disconnect(e["id"], name)
        self._update_rows()

    def _on_disconnect_done(self, name, ok, message):
        if not ok:
            self.disconnecting.pop(name, None)
            self.user_disconnected.discard(name)
            self._flash("Couldn't disconnect %s%s. You can disconnect it in Windows Bluetooth "
                        "settings." % (name, ": " + message if message else ""), 10)
            self._update_rows()

    def _on_reconnect_done(self, name, ok, message):
        if not ok and name in self.reconnecting:
            del self.reconnecting[name]
            self.reconnect_failed[name] = time.monotonic()
            self._update_rows()
        if ok:
            self.monitor.poll_now()

    def _beep(self, name):
        """Identify tone. While playing it is mixed into that speaker's audio;
        while stopped the speaker is opened on its own just for the tone."""
        if self.engine.running:
            if not self.engine.beep(name):
                self._flash("%s isn't playing right now (it may have dropped out), "
                            "so it can't beep." % name)
            return
        dev = next((d for d in self.outputs if d["name"] == name), None)
        if dev is None or self.backend is None:
            self._flash("%s isn't connected, so it can't beep." % name)
            return
        try:
            stream = open_tone_stream(self.backend, dev)
        except Exception as e:
            self._flash("Couldn't play a tone on %s: %s" % (name, e))
            return
        self._tone_streams.append(stream)
        QTimer.singleShot(int(TONE_TOTAL_S * 1000), lambda: self._close_tone(stream))

    def _flash(self, text, seconds=6.0):
        """Show a message in the status line for a few seconds, even while playing."""
        self._notice = (text, time.monotonic() + seconds)
        self.status.setText(text)

    def _close_tone(self, stream):
        if stream in self._tone_streams:
            self._tone_streams.remove(stream)
            try:
                self.backend.close(stream)
            except Exception:
                pass

    def _close_tones(self):
        for s in list(self._tone_streams):
            self._close_tone(s)

    def _add_speaker(self):
        dlg = PairDialog(self)
        dlg.paired.connect(self.monitor.poll_now)
        dlg.exec()
        self.monitor.poll_now()

    def _row_state(self, name):
        """(text, colour, show Reconnect, Reconnect enabled) for one speaker row."""
        e = self.endpoints.get(name)
        is_bt = bool(e and e["bluetooth"])
        now = time.monotonic()
        if e is not None and e["connected"] and name in self.disconnecting:
            return "Disconnecting…", AMBER, False, True
        if e is not None and not e["connected"]:
            if name in self.reconnecting:
                return "Reconnecting…", AMBER, True, False
            if name in self.user_disconnected:
                return "Disconnected. Press Reconnect to use it again.", GREY, True, True
            if now - self.reconnect_failed.get(name, -1e9) < 30:
                return "Couldn't reconnect. Is it switched on and nearby?", RED, True, True
            if self._hands_free_only(e):
                return ("Connected in hands-free (call) mode only, which sounds poor. "
                        "Press Reconnect."), AMBER, True, True
            if self.start_btn.isChecked() and name in self.checked and self.opts["auto_reconnect"]:
                return "Disconnected. RoomCast keeps trying to reconnect it.", RED, True, True
            return "Disconnected (paired). Switch it on or press Reconnect.", GREY, True, True
        if name in self.dropped:
            if self.dropped[name].get("rejoining"):
                return "Back. Rejoining playback…", AMBER, False, True
            return "Dropped out. It rejoins automatically when it's back.", AMBER, is_bt, True
        if self.engine.running and any(o.name == name for o in self.engine.outs):
            return "Playing", GREEN, False, True
        if name in self.checked and self.start_btn.isChecked():
            return "Connected. Joining…", AMBER, False, True
        return "Connected" + (" · Bluetooth" if is_bt else ""), GREEN if is_bt else GREY, False, True

    def _update_rows(self):
        for name, row in self.rows.items():
            text, color, show, enabled = self._row_state(name)
            e = self.endpoints.get(name)
            can_disconnect = bool(e and e["bluetooth"] and e["connected"] and not e["hands_free"]
                                  and name not in self.disconnecting)
            row.set_state(text, color, show, enabled, can_disconnect)
            row.set_battery(self.battery.get(e["container"]) if e and e["bluetooth"] else None)
        self._fit_rows()

    def _tick(self):
        self._tick_n = getattr(self, "_tick_n", 0) + 1
        if self._tick_n % 8 == 0:               # about twice a second
            self._check_dropouts()
        if not self.engine.running:
            return
        for m, lv in zip(self.meters, self.engine.levels()):
            db = 20 * np.log10(max(lv, 1e-4))
            m.setValue(int(np.clip((db + 60) / 60 * 100, 0, 100)))
        if time.monotonic() < self._notice[1]:
            return self.status.setText(self._notice[0])
        playing = len(self.engine.outs) - len(self.engine.stalled())
        waiting = [n for n in self.checked if n not in [o.name for o in self.engine.outs]]
        self.status.setText("Running · %d speaker(s) playing%s · source %d ch @ %d Hz · dropouts: %d" % (
            playing, " · %d waiting to reconnect" % len(waiting) if waiting else "",
            self.engine.n_in, self.engine.src_rate, self.engine.underruns()))
