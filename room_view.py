"""Orbitable 3D room view for placing speakers and the listener."""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

LISTENER = -1


class RoomView(QWidget):
    changed = Signal()          # a position was edited
    selected = Signal(int)      # index into state.active, or LISTENER

    def __init__(self, state):
        super().__init__()
        self.state = state
        self.az, self.el, self.zoom = math.radians(-32), math.radians(30), 1.0
        self.sel = LISTENER
        self._drag = None
        self._last = None
        self.setMinimumSize(280, 200)
        self.setMouseTracking(True)

    # ---- camera ------------------------------------------------------
    def _camera(self):
        W, D, H = self.state.room
        target = np.array([0.0, 0.0, H * 0.35])
        dist = 1.7 * max(W, D, H) * self.zoom
        cam = target + dist * np.array([math.cos(self.el) * math.sin(self.az),
                                        -math.cos(self.el) * math.cos(self.az),
                                        math.sin(self.el)])
        f = (target - cam) / dist
        r = np.cross(f, [0, 0, 1.0])
        r /= np.linalg.norm(r)
        u = np.cross(r, f)
        F = 0.95 * min(self.width(), self.height())
        return cam, f, r, u, F

    def _project(self, p, c=None):
        cam, f, r, u, F = c or self._camera()
        v = np.asarray(p, float) - cam
        z = max(float(v @ f), 1e-3)
        return QPointF(self.width() / 2 + F * float(v @ r) / z,
                       self.height() / 2 - F * float(v @ u) / z), z

    def _ray_to_plane(self, x, y, h):
        cam, f, r, u, F = self._camera()
        d = f + r * (x - self.width() / 2) / F + u * (self.height() / 2 - y) / F
        if abs(d[2]) < 1e-6:
            return None
        t = (h - cam[2]) / d[2]
        return cam + t * d if t > 0 else None

    def _clamp(self, p):
        W, D, H = self.state.room
        return [float(np.clip(p[0], -W / 2, W / 2)), float(np.clip(p[1], -D / 2, D / 2)),
                float(np.clip(p[2], 0, H))]

    def _items(self):
        return [(LISTENER, self.state.listener)] + [(i, s.pos) for i, s in enumerate(self.state.active)]

    def _pick(self, x, y):
        best, bd = None, 18.0
        for idx, pos in self._items():
            pt, _ = self._project(pos)
            d = math.hypot(pt.x() - x, pt.y() - y)
            if d < bd:
                best, bd = idx, d
        return best

    # ---- mouse -------------------------------------------------------
    def mousePressEvent(self, e):
        self._last = e.position()
        hit = self._pick(e.position().x(), e.position().y()) if e.button() == Qt.LeftButton else None
        self._drag = hit
        if hit is not None:
            self.sel = hit
            self.selected.emit(hit)
            self.update()

    def mouseMoveEvent(self, e):
        if self._last is None or not e.buttons():
            return
        dx, dy = e.position().x() - self._last.x(), e.position().y() - self._last.y()
        self._last = e.position()
        if self._drag is None:
            self.az -= dx * 0.008
            self.el = float(np.clip(self.el + dy * 0.008, 0.05, 1.5))
        else:
            pos = self.state.listener if self._drag == LISTENER else self.state.active[self._drag].pos
            if e.modifiers() & Qt.ShiftModifier:
                _, z = self._project(pos)
                new = [pos[0], pos[1], pos[2] - dy * z / self._camera()[4]]
            else:
                hit = self._ray_to_plane(e.position().x(), e.position().y(), pos[2])
                if hit is None:
                    return
                new = [hit[0], hit[1], pos[2]]
            pos[:] = self._clamp(new)
            self.changed.emit()
        self.update()

    def mouseReleaseEvent(self, e):
        self._drag = None
        self._last = None

    def wheelEvent(self, e):
        self.zoom = float(np.clip(self.zoom * (0.9 if e.angleDelta().y() > 0 else 1.1), 0.4, 3.0))
        self.update()

    # ---- painting ----------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#14171c"))
        c = self._camera()
        W, D, H = self.state.room
        P = lambda q: self._project(q, c)[0]

        p.setPen(QPen(QColor("#262b33"), 1))
        for x in np.arange(-W / 2, W / 2 + 1e-6, 0.5):
            p.drawLine(P([x, -D / 2, 0]), P([x, D / 2, 0]))
        for y in np.arange(-D / 2, D / 2 + 1e-6, 0.5):
            p.drawLine(P([-W / 2, y, 0]), P([W / 2, y, 0]))

        corners = [(-W / 2, -D / 2), (W / 2, -D / 2), (W / 2, D / 2), (-W / 2, D / 2)]
        p.setPen(QPen(QColor("#4a5464"), 1.4))
        for i in range(4):
            a, b = corners[i], corners[(i + 1) % 4]
            p.drawLine(P([*a, 0]), P([*b, 0]))
            p.drawLine(P([*a, H]), P([*b, H]))
            p.drawLine(P([*a, 0]), P([*a, H]))
        p.setPen(QPen(QColor("#7fb2ff"), 3))
        p.drawLine(P([-W / 2, D / 2, 0]), P([W / 2, D / 2, 0]))
        p.setFont(QFont(self.font().family(), 9))
        p.drawText(P([0, D / 2, 0]) + QPointF(-18, 16), "FRONT")

        items = sorted(self._items(), key=lambda it: -self._project(it[1], c)[1])
        L = self.state.listener
        for idx, pos in items:
            pt, z = self._project(pos, c)
            floor = P([pos[0], pos[1], 0])
            p.setPen(QPen(QColor("#5a6474"), 1, Qt.DashLine))
            p.drawLine(pt, floor)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 90))
            p.drawEllipse(floor, 7, 3)
            rad = float(np.clip(0.16 * c[4] / z, 8, 22))
            if idx == LISTENER:
                col, label, name = QColor("#f2f2f2"), "", "Listener"
                nose = P([pos[0], pos[1] + 0.35, pos[2]])
                p.setPen(QPen(col, 2))
                p.drawLine(pt, nose)
            else:
                s = self.state.active[idx]
                col, label, name = QColor(s.color), str(idx + 1), s.name
            if idx == self.sel:
                p.setPen(QPen(QColor("#ffffff"), 1, Qt.DotLine))
                if idx != LISTENER:
                    p.drawLine(pt, P(L))
                p.setPen(QPen(QColor("#ffffff"), 2.5))
            else:
                p.setPen(QPen(QColor("#14171c"), 1.5))
            p.setBrush(col)
            p.drawEllipse(pt, rad, rad)
            p.setPen(QColor("#14171c"))
            f = QFont(self.font().family(), 9)
            f.setBold(True)
            p.setFont(f)
            p.drawText(int(pt.x() - rad), int(pt.y() - rad), int(2 * rad), int(2 * rad), Qt.AlignCenter, label)
            p.setPen(QColor("#d6dbe3"))
            p.setFont(QFont(self.font().family(), 9))
            extra = ""
            if idx != LISTENER:
                extra = "  %.2f m" % math.dist(pos, L)
            p.drawText(pt + QPointF(rad + 5, 4), name[:28] + extra)

        p.setPen(QColor("#77808e"))
        p.drawText(10, self.height() - 10,
                   "Drag a speaker or the listener to move it  ·  Shift+drag = height  ·  "
                   "drag empty space = orbit  ·  wheel = zoom")
        p.end()
