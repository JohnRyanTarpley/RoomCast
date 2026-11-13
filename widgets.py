"""Reusable widgets and the dark theme for the RoomCast window."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout, QLabel, QLayout, QPushButton,
                               QSizePolicy, QSplitter, QWidget)


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
    # Check box whose label shortens with "…" to fit, so its column can be resized freely.

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
    # A horizontal row packed into its own widget: returns (widget, layout).
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    return w, lay


class FlowLayout(QLayout):
    # Lays widgets out left to right and wraps onto a new line when out of room.

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

    def set_battery(self, pct, unknown=False):
        """Show the battery percentage. With pct None, `unknown` marks a
        connected Bluetooth speaker that doesn't report its battery."""
        if pct is None:
            if unknown:
                self.battery.setText("🔋 <span style='color:%s'>–</span>" % GREY)
                self.battery.setToolTip("RoomCast can't get this speaker's battery level: "
                                        "it doesn't report it to Windows.")
            else:
                self.battery.clear()
                self.battery.setToolTip("")
            return
        color = RED if pct <= 20 else (AMBER if pct <= 40 else None)
        text = "🔋 %d%%" % pct
        self.battery.setText("<span style='color:%s'>%s</span>" % (color, text) if color else text)
        self.battery.setToolTip("Battery level reported by the speaker")


class BandView(QWidget):
    # Shows which slice of the spectrum each speaker plays.

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
