"""Background device monitoring and the "Add speaker" pairing dialog."""
from __future__ import annotations

import asyncio
import queue
import threading
import time

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QVBoxLayout)

try:
    import bluetooth as bt
except Exception:           # not on Windows
    bt = None


class DeviceMonitor(QObject):
    """Polls Windows for speaker connection state (every couple of seconds)
    and battery levels (every 30 s) on a background thread, and carries out
    reconnect requests there so the window never freezes."""

    updated = Signal(list, dict)                # endpoints, {container: battery %}
    reconnect_done = Signal(str, bool, str)     # speaker name, ok, message

    def __init__(self, interval=2.0, battery_every=30.0):
        super().__init__()
        self.interval, self.battery_every = interval, battery_every
        self._jobs = queue.Queue()
        self._running = False

    @property
    def available(self):
        return bt is not None

    def start(self):
        if self.available and not self._running:
            self._running = True
            threading.Thread(target=self._run, name="device-monitor", daemon=True).start()

    def stop(self):
        self._running = False
        self._jobs.put(None)

    def poll_now(self):
        self._jobs.put(("poll",))

    def request_reconnect(self, endpoint_id, name):
        self._jobs.put(("reconnect", endpoint_id, name))

    def _run(self):
        bt.com_init()
        battery, last_battery, last_ids = {}, 0.0, None
        while self._running:
            try:
                endpoints = bt.list_endpoints()
            except OSError:
                endpoints = []
            ids = sorted((e["id"], e["state"]) for e in endpoints)
            now = time.monotonic()
            if ids != last_ids or now - last_battery > self.battery_every:
                try:
                    battery = bt.battery_levels()
                except OSError:
                    battery = {}
                last_battery, last_ids = now, ids
            self.updated.emit(endpoints, battery)
            try:
                job = self._jobs.get(timeout=self.interval)
            except queue.Empty:
                continue
            if job is None:
                break
            if job[0] == "reconnect":
                _, endpoint_id, name = job
                try:
                    bt.reconnect(endpoint_id)
                    self.reconnect_done.emit(name, True, "")
                except OSError as e:
                    self.reconnect_done.emit(name, False, str(e))


class _PairWorker(QObject):
    found = Signal(str, str)        # device id, name
    scan_done = Signal(str)         # error message, or "" on success
    pair_done = Signal(bool, str)   # ok, message


class PairDialog(QDialog):
    """Finds nearby Bluetooth speakers that are in pairing mode and pairs them."""

    paired = Signal()
    SCAN_S = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add a Bluetooth speaker")
        self.resize(440, 380)
        self.worker = _PairWorker()
        self.worker.found.connect(self._on_found)
        self.worker.scan_done.connect(self._on_scan_done)
        self.worker.pair_done.connect(self._on_pair_done)
        self._stop_scan = False
        self._busy = False

        v = QVBoxLayout(self)
        intro = QLabel("1. Switch the speaker on and put it in <b>pairing mode</b> (usually: hold its "
                       "Bluetooth button until the light flashes).<br>"
                       "2. Wait for it to appear below, select it and press <b>Pair</b>.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self._pair())
        self.list.currentRowChanged.connect(lambda _: self._update_buttons())
        v.addWidget(self.list, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setProperty("hint", True)
        v.addWidget(self.status)
        row = QHBoxLayout()
        self.scan_btn = QPushButton("Search again")
        self.scan_btn.clicked.connect(self._scan)
        row.addWidget(self.scan_btn)
        row.addStretch()
        self.pair_btn = QPushButton("Pair")
        self.pair_btn.setDefault(True)
        self.pair_btn.clicked.connect(self._pair)
        row.addWidget(self.pair_btn)
        b = QPushButton("Close")
        b.clicked.connect(self.reject)
        row.addWidget(b)
        v.addLayout(row)

        if bt is None or not bt.pairing_available():
            self.status.setText("Pairing support is not installed yet. Close RoomCast and start it again "
                                "with RoomCast.exe to finish installing it, or use Windows Bluetooth settings.")
            self.scan_btn.setEnabled(False)
            self.pair_btn.setEnabled(False)
        else:
            self._scan()

    def _update_buttons(self):
        self.scan_btn.setEnabled(not self._busy)
        self.pair_btn.setEnabled(not self._busy and self.list.currentItem() is not None)

    def _run_async(self, coro_fn):
        def target():
            try:
                asyncio.run(coro_fn())
            except Exception:
                pass
        threading.Thread(target=target, daemon=True).start()

    def _scan(self):
        self.list.clear()
        self._busy, self._stop_scan = True, False
        self._update_buttons()
        self.status.setText("Searching for speakers in pairing mode…")
        w = self.worker

        async def job():
            try:
                await bt.scan_for_speakers(self.SCAN_S, w.found.emit, lambda: self._stop_scan)
                w.scan_done.emit("")
            except Exception as e:
                w.scan_done.emit(str(e) or type(e).__name__)
        self._run_async(job)

    def _on_found(self, device_id, name):
        it = QListWidgetItem(name)
        it.setData(Qt.UserRole, device_id)
        self.list.addItem(it)
        if self.list.count() == 1:
            self.list.setCurrentRow(0)

    def _on_scan_done(self, error):
        self._busy = False
        self._update_buttons()
        if error:
            self.status.setText(error)
        elif self.list.count() == 0:
            self.status.setText("No speakers found. Make sure the speaker is in pairing mode, close to the "
                                "PC, and not connected to a phone, then press Search again.")
        else:
            self.status.setText("Select your speaker and press Pair.")

    def _pair(self):
        it = self.list.currentItem()
        if it is None or self._busy:
            return
        self._stop_scan = True
        self._busy = True
        self._update_buttons()
        name, device_id = it.text(), it.data(Qt.UserRole)
        self.status.setText("Pairing with %s…" % name)
        w = self.worker

        async def job():
            try:
                ok, msg = await bt.pair(device_id)
            except Exception as e:
                ok, msg = False, "Pairing failed: %s" % (str(e) or type(e).__name__)
            w.pair_done.emit(ok, "%s: %s" % (name, msg))
        self._run_async(job)

    def _on_pair_done(self, ok, message):
        self._busy = False
        self._update_buttons()
        if ok:
            self.status.setText(message + " It will appear in the speaker list in a few seconds.")
            self.paired.emit()
        else:
            self.status.setText(message + " Put the speaker back in pairing mode and try again.")

    def done(self, r):
        self._stop_scan = True
        super().done(r)
