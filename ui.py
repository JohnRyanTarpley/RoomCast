# RoomCast main window.
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import numpy as np
from PySide6.QtCore import QByteArray, QEvent, QSize, Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox,
                               QDoubleSpinBox, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                               QFrame, QProgressBar, QPushButton, QRadioButton, QScrollArea,
                               QSlider, QSpinBox, QStackedWidget, QTableWidget, QVBoxLayout,
                               QWidget)

import dsp
from connection import DeviceMonitor, PairDialog, bt
from diag import log
from dropouts import DropoutHandling, Link
from pauseguard import PauseGuard
from engine import TONE_TOTAL_S, Engine, MixSettings, SpeakerSettings, open_tone_stream
from room_view import LISTENER, RoomView
from widgets import AMBER, GREEN, GREY, RED, BandView, FlowLayout, SpeakerRow, hbox, hint, split

COLORS = ["#ff6b6b", "#4dabf7", "#51cf66", "#fcc419", "#cc5de8", "#ff922b", "#22b8cf", "#f783ac"]
MODES = ["surround", "frequency", "sync"]
CONFIG = Path(os.environ.get("APPDATA", Path.home())) / "RoomCast" / "settings.json"
try:                                    # same file the launcher's build stamps into RoomCast.exe
    VERSION = (Path(__file__).parent / "VERSION").read_text("utf-8").strip()
except OSError:
    VERSION = ""
STOPPED = "Stopped" + (" · RoomCast %s" % VERSION if VERSION else "")


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

    @classmethod
    def from_dict(cls, name, d):
        """From the settings file: unknown keys are ignored and invalid values
        keep their defaults. None if `d` isn't a speaker entry at all."""
        if not isinstance(d, dict):
            return None
        cfg = cls(name=name)
        for f in fields(cls):
            if f.name != "name" and f.name in d and same_kind(d[f.name], getattr(cfg, f.name)):
                setattr(cfg, f.name, d[f.name])
        return cfg


def same_kind(value, default):
    """True if a value read from the settings file has the same type as the
    default it replaces (any number for a number; a list of fixed length, such
    as a position, element by element; an empty default list takes any list)."""
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, list) and default:
        return (isinstance(value, list) and len(value) == len(default)
                and all(same_kind(v, x) for v, x in zip(value, default)))
    return isinstance(value, type(default))


class MainWindow(DropoutHandling, QMainWindow):
    def __init__(self, backend=None):
        super().__init__()
        self.setWindowTitle("RoomCast – multi-speaker Bluetooth audio")
        self.speakers: dict[str, SpeakerCfg] = {}
        self.active: list[SpeakerCfg] = []
        self.checked: list[str] = []
        self.room = [5.0, 4.0, 2.5]
        self.listener = [0.0, 0.0, 1.2]
        self.mode = "sync"
        self.opts = {"master": 0.8, "base_ms": 40, "focus": 2.0, "delay_comp": True,
                     "level_comp": True, "upmix": True, "source": "", "auto_reconnect": True,
                     "keep_playing": True,
                     "resync_on_pause": True}
        self.outputs, self.sources = [], []
        self._loading = False
        self._load()

        # connection tracking
        self.endpoints = {}          # endpoint name -> info from bluetooth.list_endpoints()
        self.battery = {}            # container id -> battery %
        self.rows = {}               # speaker name -> SpeakerRow
        self.links: dict[str, Link] = {}     # speaker name -> its connection record
        self.dropout_count = 0       # speakers taken out of playback since Start was pressed
        self._resyncing = False      # all speakers closed for a moment by _resync()
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
        self.guard = PauseGuard()       # before refresh_devices(), which tells it the speakers
        self.guard.enabled = bool(self.opts["keep_playing"])
        self.guard.switched_off.connect(self._speaker_switched_off)
        self.guard.paused.connect(self._on_music_paused)
        self.guard.checking.connect(self._on_guard_checking)
        self._checking = False          # the pause guard is checking the speakers
        self.refresh_devices()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(60)
        self.monitor = DeviceMonitor()
        self.monitor.updated.connect(self._on_monitor)
        self.monitor.reconnect_done.connect(self._on_reconnect_done)
        self.monitor.disconnect_done.connect(self._on_disconnect_done)
        self.monitor.start()
        self.guard.start()
        if self.backend is None:
            self.status.setText("Audio system unavailable: " + self.backend_error)

    # ---- persistence -------------------------------------------------
    def _load(self):
        """Load the settings file. Anything missing or invalid keeps its default
        and the rest still loads; a file that can't be read at all is kept
        aside as settings.broken.json rather than overwritten."""
        try:
            d = json.loads(CONFIG.read_text("utf-8"))
            if not isinstance(d, dict):
                raise ValueError("not a settings object")
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            broken = CONFIG.with_name("settings.broken.json")
            try:
                os.replace(CONFIG, broken)
            except OSError:
                pass
            log.info("settings: couldn't read %s (%s), kept it as %s and started with defaults",
                     CONFIG.name, e, broken.name)
            return
        skipped = []

        def take(key, default, valid=lambda v: True):
            v = d.get(key, default)
            if same_kind(v, default) and valid(v):
                return v
            skipped.append(key)
            return default

        for name, v in take("speakers", {}).items():
            cfg = SpeakerCfg.from_dict(name, v)
            if cfg is None:
                skipped.append("speaker %s" % name)
            else:
                self.speakers[name] = cfg
        self.checked = [n for n in take("checked", []) if isinstance(n, str)]
        self.room = take("room", self.room, lambda r: all(x > 0 for x in r))
        self.listener = take("listener", self.listener)
        self.mode = take("mode", self.mode, lambda m: m in MODES)
        for key, v in take("opts", {}).items():
            if key == "layout" and isinstance(v, dict):
                self.opts[key] = v
            elif key in self.opts and same_kind(v, self.opts[key]):
                self.opts[key] = v
            elif key != "auto_resync":      # (no longer an option: a returned speaker is always reset)
                skipped.append("option %s" % key)
        if skipped:
            log.info("settings: ignored invalid %s", ", ".join(skipped))

    def _save(self):
        # Write a new file and swap it in, so a crash mid-save can't leave a half-written one.
        tmp = CONFIG.with_name(CONFIG.name + ".tmp")
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({
                "speakers": {k: asdict(v) for k, v in self.speakers.items()},
                "checked": self.checked, "room": self.room, "listener": self.listener,
                "mode": self.mode, "opts": self.opts}, indent=1), "utf-8")
            os.replace(tmp, CONFIG)
        except (OSError, TypeError, ValueError) as e:
            log.info("settings: couldn't save: %s", e)

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
        self.src_hint = hint("")              # says whether the chosen source works (see _update_source_hint)
        self.src_hint.setTextFormat(Qt.RichText)
        v.addWidget(self.src_hint)
        row = QHBoxLayout()
        self.src_fix = QPushButton("Play Windows sound through this")
        self.src_fix.setToolTip("Make this device the Windows default output, so the music RoomCast "
                                "records is the music your apps are playing.")
        self.src_fix.clicked.connect(self._make_source_default)
        self.src_fix.hide()
        row.addWidget(self.src_fix)
        b = QPushButton("Windows sound settings…")
        b.clicked.connect(lambda: self._open_settings("ms-settings:sound"))
        row.addWidget(b)
        v.addLayout(row)
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
        self.base.setRange(0, 500)
        self.base.setSuffix(" ms")
        self.base.setValue(int(self.opts["base_ms"]))
        self.base.setToolTip("Extra buffering before audio is sent to the speakers. "
                             "Raise it if you hear dropouts; lower it for less lag (at 0 ms "
                             "only a minimal buffer of about 15 ms is kept).")
        self.base.valueChanged.connect(lambda x: self._opt("base_ms", x))
        row.addWidget(self.base)
        row.addStretch()
        v.addLayout(row)
        cb = QCheckBox("Keep the music playing when a speaker is switched off")
        cb.setToolTip("Many Bluetooth speakers tell the PC to pause as they switch off, which pauses "
                      "your music on every speaker. With this on, when your music pauses RoomCast "
                      "checks whether a speaker has just gone. If one has, the music plays on after "
                      "a few seconds, and that speaker's sound moves to the others once Windows "
                      "notices it has gone. Pausing on purpose, from a speaker's button or the PC, "
                      "still works.")
        cb.setChecked(bool(self.opts["keep_playing"]))
        cb.toggled.connect(self._set_keep_playing)
        v.addWidget(cb)
        cb = QCheckBox("Resync the speakers whenever the music pauses")
        cb.setToolTip("While nothing is playing, a resync can't be heard, so RoomCast uses every "
                      "pause to line all the speakers up again.")
        cb.setChecked(bool(self.opts["resync_on_pause"]))
        cb.toggled.connect(lambda on: self.opts.__setitem__("resync_on_pause", on))
        v.addWidget(cb)
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
        self.resync_btn = QPushButton("Resync")
        self.resync_btn.setToolTip("Restart every speaker for a moment so they play in time again "
                                   "(the same as pressing Stop and Start).")
        self.resync_btn.clicked.connect(lambda: self._resync())
        self.resync_btn.setEnabled(False)
        self.resync_btn.setMinimumWidth(90)
        self.resync_btn.setFixedHeight(self.start_btn.sizeHint().height())
        row = QHBoxLayout()
        row.addWidget(self.start_btn, 1)
        row.addWidget(self.resync_btn)
        bottom.addLayout(row)
        self.status = hint(STOPPED)
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
        # Names the audio library can open right now (hands-free outputs excluded).
        return [d["name"] for d in self.outputs if not self._is_hands_free(d["name"])]

    def _list_names(self):
        names = self._playable()
        for e in self.endpoints.values():     # connected since the last refresh, or paired but off
            if not e["hands_free"] and e["name"] not in names:
                names.append(e["name"])
        return names

    def _hands_free_only(self, e):
        # True if the speaker's normal output is down but its call-mode output is up.
        c = e.get("container")
        return bool(c) and not c.startswith("{00000000-") and any(
            x["hands_free"] and x["connected"] and x["container"] == c for x in self.endpoints.values())

    def refresh_devices(self):
        # Re-scan devices. If playback is on, it continues with the new list.
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
        self._choose_source()
        self._loading = False
        self._rebuild_active()
        if resume:
            self._start(quiet=True)
        self._update_rows()
        self._update_source_hint()

    def _add_new_rows(self):
        # Add a row for every listable speaker that doesn't have one yet.
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

    # ---- audio source ------------------------------------------------
    # The source is the loopback of an output device: whatever Windows plays
    # through that device is what RoomCast records and sends to the speakers.
    VIRTUAL_CABLE = ("vb-audio", "cable input")     # name fragments of virtual audio cables

    def _source_name(self, s=None):
        # The output device behind a source (the chosen one by default), as Windows names it.
        if s is None:
            i = self.src_combo.currentIndex()
            s = self.sources[i] if 0 <= i < len(self.sources) else None
        return s["name"].replace(" [Loopback]", "") if s else None

    def _is_speaker(self, name):
        # A ticked speaker or any Bluetooth speaker: the devices RoomCast plays to.
        e = self.endpoints.get(name)
        return name in self.checked or bool(e and e["bluetooth"])

    def _windows_output(self):
        # The device Windows currently plays through (its name), or None if unknown.
        if bt is not None:
            try:
                d = bt.default_output()
                return d["name"] if d else None
            except OSError:
                pass
        return self.backend.default_output_name() if self.backend is not None else None

    def _auto_source(self):
        """A sensible source when the user hasn't chosen one: the device Windows
        plays through, unless that is a speaker; then a virtual cable; then a
        wired output. Never a Bluetooth or ticked speaker. None if no such device."""
        usable = [s for s in self.sources if not self._is_speaker(self._source_name(s))]
        default = self._windows_output()
        for s in usable:
            if default and self._source_name(s) == default:
                return s
        for s in usable:
            if any(v in s["name"].lower() for v in self.VIRTUAL_CABLE):
                return s
        return usable[0] if usable else None

    def _choose_source(self):
        # Select the saved source, or pick one automatically if none was chosen or it's gone.
        names = [s["name"] for s in self.sources]
        want = self.opts["source"]
        if want not in names:
            auto = self._auto_source()
            want = auto["name"] if auto else ""
        if want in names:
            self.src_combo.setCurrentIndex(names.index(want))

    def _update_source_hint(self):
        """Say whether the chosen source works: it must not be a speaker, and
        Windows must be playing through it."""
        src = self._source_name()
        default = self._windows_output()
        fix = False
        if src is None:
            color, text = RED, "No source devices found. Press Refresh."
        elif src in self.checked:
            color, text = RED, ("This is one of your ticked speakers, so the sound would feed back on "
                                "itself. Choose a device you aren't using as a speaker.")
        elif self._is_speaker(src):
            color, text = AMBER, ("This is a Bluetooth speaker, so your music would also play on it "
                                  "directly. A wired output or a virtual cable works better.")
        elif default and default != src:
            e = self.endpoints.get(src)
            fix = bool(e and e["connected"])
            color, text = AMBER, ("Windows is playing through %s, not this device, so RoomCast won't "
                                  "hear your music." % default)
        elif any(v in src.lower() for v in self.VIRTUAL_CABLE):
            color, text = GREEN, ("Windows plays through this virtual cable and RoomCast records it. "
                                  "Nothing else plays out loud.")
        else:
            color, text = GREEN, ("Windows plays through this device and RoomCast records it. Turn this "
                                  "device's volume all the way down so you only hear the speakers.")
        self.src_hint.setText("<span style='color:%s'>●</span> %s" % (color, text))
        self.src_fix.setVisible(fix)

    def _make_source_default(self):
        src = self._source_name()
        e = self.endpoints.get(src) if src else None
        if bt is None or e is None:
            return
        try:
            bt.set_default_output(e["id"])
        except OSError as err:
            self._flash("Windows didn't switch to %s (%s). Use Windows sound settings instead."
                        % (src, err), 10)
        self._update_source_hint()

    def _on_source(self, i):
        if not self._loading and 0 <= i < len(self.sources):
            self.opts["source"] = self.sources[i]["name"]     # chosen by the user: kept from now on
            self._update_source_hint()
            self._restart_if_running()

    def _on_toggle(self, name, on):
        if self._loading:
            return
        if on and name not in self.checked:
            self.checked.append(name)
        elif not on and name in self.checked:
            self.checked.remove(name)
        if not self.opts["source"] and self._source_name() in self.checked:
            self._loading = True        # the automatic choice just became a speaker: choose again
            self._choose_source()
            self._loading = False
        self._update_source_hint()
        self._link(name).drop = None
        if self.start_btn.isChecked():
            self._schedule_restart(150)     # also picks up speakers connected since the last refresh
        else:
            self._rebuild_active()
        self._update_rows()

    def _rebuild_active(self):
        present = [n for n in self._playable() if not self._dropped(n)]
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
            self.dropout_count = 0      # counts from Start; resyncs and restarts keep it
            self._start()
        else:
            self._stop()
            self.guard.pause_all()      # stopping RoomCast pauses the music on the computer too

    def _fail(self, msg, quiet=False):
        self.start_btn.setChecked(False)
        self.start_btn.setText("Start")
        self.resync_btn.setEnabled(False)
        if quiet:
            self.status.setText("Stopped: " + msg.split("\n")[0])
        else:
            QMessageBox.warning(self, "RoomCast", msg)

    def _set_running_ui(self):
        self.start_btn.setChecked(True)
        self.start_btn.setText("Stop")
        self.resync_btn.setEnabled(True)

    def _start(self, quiet=False, skip_dropped=False):
        """quiet=True for automatic restarts: report problems in the status line, no pop-ups.
        skip_dropped=True leaves speakers that are out of playback closed (they rejoin as usual)."""
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
            skip = {k for k, s in enumerate(self.active) if self._dropped(s.name)} if skip_dropped else ()
            failed = self.engine.start(src, [by_name[s.name] for s in self.active], self.mix(), skip)
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
        log.info("playback started: %s%s", ", ".join(s.name for s in self.active),
                 " (failed to open: %s)" % ", ".join(self.active[k].name for k in failed) if failed else "")
        self._last_stats = time.monotonic()
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
        log.info("playback stopped")
        self._restart_timer.stop()
        self._resyncing = False
        self.engine.stop()
        for link in self.links.values():
            link.settling, link.steady_since, link.resetting = False, None, False
            link.drop, link.rejoin_fails = None, 0
        self.start_btn.setChecked(False)
        self.start_btn.setText("Start")
        self.resync_btn.setEnabled(False)
        self.status.setText(STOPPED)
        for m in getattr(self, "meters", []):
            m.setValue(0)
        self._update_rows()

    def _restart_if_running(self):
        if self.start_btn.isChecked():
            self._schedule_restart(150)

    def _schedule_restart(self, ms):
        # Restart playback (re-scanning devices) shortly. Repeated calls are merged.
        self._restart_timer.start(ms)

    def _auto_restart(self):
        if self.start_btn.isChecked() and not self._resyncing:
            self.refresh_devices()

    RESYNC_GAP_MS = 1500    # long enough for Windows to close the Bluetooth audio links
    RESUME_AFTER_RESYNC_MS = 700    # Bluetooth speakers swallow the first moment after waking
    def _on_guard_checking(self, on):
        self._checking = on
        self._update_rows()

    def _on_music_paused(self):
        if (self.opts["resync_on_pause"] and self.engine.running and self.start_btn.isChecked()
                and not self._resyncing):
            self._resync(on_pause=True)

    def _resync(self, on_pause=False):
        """Line every speaker up again by closing them all for a moment and
        starting afresh, as Stop then Start does. A speaker that comes back
        after a dropout can play slightly late even though RoomCast feeds it
        on time, because audio queued up in its Bluetooth link; only closing
        the link clears that.

        on_pause: done because the music paused. Then there is no device
        re-scan (it can hang while a speaker that was just switched off still
        holds up Bluetooth), and speakers that are out stay out until they
        rejoin as usual."""
        if not self.start_btn.isChecked() or self._resyncing:
            return
        log.info("resync all speakers%s", " (the music paused)" if on_pause else "")
        if not on_pause:
            self.guard.pause_all(remember=True)     # resumed where it left off once restarted
        self._resync_on_pause = on_pause
        self._resyncing = True
        self._restart_timer.stop()
        self._close_tones()
        self.engine.stop(wait=0.5 if on_pause else 5.0)
        for link in self.links.values():
            link.settling, link.steady_since, link.resetting = False, None, False
            if link.drop:
                link.drop.rejoining = False     # a reopen under way is discarded by the stop: try again
        self.resync_btn.setEnabled(False)
        if not on_pause:                # on a pause, the speakers' rows say "Resyncing…" anyway
            self._flash("Resyncing all speakers…", 4)
        self._update_rows()
        QTimer.singleShot(self.RESYNC_GAP_MS, self._finish_resync)

    def _finish_resync(self):
        if not self._resyncing:
            return                      # stopped meanwhile
        self._resyncing = False
        if not self.start_btn.isChecked():
            return
        if getattr(self, "_resync_on_pause", False):
            self._start(quiet=True, skip_dropped=True)
        else:
            self.refresh_devices()      # re-scans (picks up the returned speaker) and starts
            if self.engine.running:     # give the speakers a moment to wake before the music
                QTimer.singleShot(self.RESUME_AFTER_RESYNC_MS, self.guard.resume)

    def _set_keep_playing(self, on):
        self.opts["keep_playing"] = on
        self.guard.enabled = on

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
        # Show a message in the status line for a few seconds, even while playing.
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
        # (text, colour, show Reconnect, Reconnect enabled) for one speaker row.
        e, link = self.endpoints.get(name), self._link(name)
        d = link.drop
        is_bt = bool(e and e["bluetooth"])
        now = time.monotonic()
        if e is not None and e["connected"] and link.disconnecting is not None:
            return "Disconnecting…", AMBER, False, True
        if e is not None and not e["connected"]:
            if link.reconnecting is not None:
                return "Reconnecting…", AMBER, True, False
            if link.user_disconnected:
                return "Disconnected. Press Reconnect to use it again.", GREY, True, True
            if now - link.reconnect_failed < 30:
                return "Couldn't reconnect. Is it switched on and nearby?", RED, True, True
            if self._hands_free_only(e):
                return ("Connected in hands-free (call) mode only, which sounds poor. "
                        "Press Reconnect."), AMBER, True, True
            if self.start_btn.isChecked() and name in self.checked and self.opts["auto_reconnect"]:
                return "Disconnected. RoomCast keeps trying to reconnect it.", RED, True, True
            return "Disconnected (paired). Switch it on or press Reconnect.", GREY, True, True
        if link.resetting and not d:
            return "Resyncing this speaker…", AMBER, False, True
        if self._switched_off(name):
            return "Turned off. Its sound moved to the other speakers.", AMBER, False, True
        playing = self.engine.running and any(o.name == name for o in self.engine.outs)
        if not d and name in self.checked and self.start_btn.isChecked() and (
                self._resyncing or (self._checking and playing and self.opts["resync_on_pause"])):
            # All speakers restarting (or, after a pause that resynced them, still
            # being checked for one that was switched off): don't flash "Joining…" /
            # "Playing" meanwhile. Without the resync, nothing restarts on a pause,
            # so the speakers just keep saying "Playing" during the check.
            return "Resyncing…", AMBER, False, True
        if link.settling and not d:
            return "Back. Stabilizing connection…", AMBER, False, True
        if d:
            if d.rejoining:
                return "Back. Rejoining playback…", AMBER, False, True
            return "Dropped out. It rejoins automatically when it's back.", AMBER, is_bt, True
        if playing:
            return "Playing", GREEN, False, True
        if name in self.checked and self.start_btn.isChecked():
            return "Connected. Joining…", AMBER, False, True
        return "Connected" + (" · Bluetooth" if is_bt else ""), GREEN if is_bt else GREY, False, True

    def _update_rows(self):
        self._guard_speakers()
        for name, row in self.rows.items():
            text, color, show, enabled = self._row_state(name)
            e = self.endpoints.get(name)
            can_disconnect = bool(e and e["bluetooth"] and e["connected"] and not e["hands_free"]
                                  and self._link(name).disconnecting is None
                                  and not self._switched_off(name))   # can't reach it while off
            row.set_state(text, color, show, enabled, can_disconnect)
            is_bt = bool(e and e["bluetooth"])
            row.set_battery(self.battery.get(e["container"]) if is_bt else None,
                            unknown=is_bt and e["connected"] and not e["hands_free"])
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
        self.status.setText("Running · %d speaker(s) playing%s · source %d ch @ %d Hz\n"
                            "Speaker dropouts: %d · buffer gaps: %d%s" % (
            playing, " · %d waiting to reconnect" % len(waiting) if waiting else "",
            self.engine.n_in, self.engine.src_rate, self.dropout_count, self.engine.underruns(),
            " · RoomCast %s" % VERSION if VERSION else ""))
