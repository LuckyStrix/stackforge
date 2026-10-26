"""Tool changes against colour error: paint's --toolchange-curve as a chart and a slider.

paint writes the curve (one point per plan, fewest changes first, the last unrestricted);
the slider walks its points and "Run with this limit" reruns paint with --max-toolchanges
set to the point's count. The counts are paint's upper bound; the slicer comes in under it.
"""
from __future__ import annotations

import json

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider,
                               QVBoxLayout, QWidget)

from stackforge.gui import theme
from stackforge.gui.spectrum_chart import _paint_error


def load_curve(path) -> dict:
    with open(path) as f:
        data = json.load(f)
    if not data.get("points"):
        raise ValueError(f"{path}: no points")
    return data


def describe(pt: dict, secs, last: bool) -> str:
    """One line for a curve point."""
    t = f" (~{pt['changes'] * secs / 3600:.1f} h at {secs:g} s each)" if secs else ""
    layers = " / ".join(str(n) for n in pt.get("layers_by_tools", []))
    lim = "no limit: " if last else ""
    return (f"{lim}at most {pt['changes']} tool changes{t}; mean {pt['mean']:.1f} dE, "
            f"p95 {pt['p95']:.1f}\nlayers printing 1, 2, ... filaments: {layers}")


class ToolchangeChart(QWidget):
    """Mean and p95 dE against tool changes, the selected point marked."""

    def __init__(self):
        super().__init__()
        self.points, self.sel = [], -1
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_points(self, points, sel):
        self.points, self.sel = list(points), sel
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        try:
            self._paint(p)
        except Exception as exc:  # noqa: BLE001 -- an exception escaping paintEvent kills PySide
            _paint_error(self, p, exc)

    def _paint(self, p):
        p.fillRect(self.rect(), QColor(theme.BG))
        p.setRenderHint(QPainter.Antialiasing)
        if not self.points:
            p.setPen(QColor(theme.FG_DIM))
            p.drawText(self.rect(), Qt.AlignCenter, "no curve yet")
            return
        r = QRectF(self.rect()).adjusted(44, 10, -12, -28)
        xmax = max(1, max(pt["changes"] for pt in self.points))
        ymax = max(1.0, max(pt["p95"] for pt in self.points)) * 1.08

        def at(x, y):
            return QPointF(r.left() + r.width() * x / xmax, r.bottom() - r.height() * y / ymax)

        p.setPen(QPen(QColor(theme.LINE), 1))
        p.drawRect(r)
        p.setPen(QColor(theme.FG_DIM))
        for i in range(5):
            y = ymax * i / 4
            q = at(0, y)
            p.drawText(QRectF(0, q.y() - 8, 40, 16), Qt.AlignRight | Qt.AlignVCenter, f"{y:.0f}")
        for i in range(5):
            x = xmax * i / 4
            q = at(x, 0)
            p.drawText(QRectF(q.x() - 30, r.bottom() + 2, 60, 14), Qt.AlignCenter, f"{x:.0f}")
        p.drawText(QRectF(r.left(), r.bottom() + 14, r.width(), 14), Qt.AlignCenter,
                   "tool changes (at most)")
        for key, col, name in (("p95", theme.WARN, "p95 dE"), ("mean", theme.ACCENT, "mean dE")):
            pen = QPen(QColor(col), 2)
            p.setPen(pen)
            pts = [at(pt["changes"], pt[key]) for pt in self.points]
            for a, b in zip(pts, pts[1:]):
                p.drawLine(a, b)
            p.setBrush(QColor(col))
            for q in pts:
                p.drawEllipse(q, 2.5, 2.5)
        if 0 <= self.sel < len(self.points):
            pt = self.points[self.sel]
            p.setPen(QPen(QColor(theme.FG), 1, Qt.DashLine))
            x = at(pt["changes"], 0).x()
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        p.setPen(QColor(theme.WARN))
        p.drawText(QRectF(r.right() - 150, r.top() + 2, 146, 14), Qt.AlignRight, "p95 dE")
        p.setPen(QColor(theme.ACCENT))
        p.drawText(QRectF(r.right() - 150, r.top() + 16, 146, 14), Qt.AlignRight, "mean dE")


class ToolchangeBox(QWidget):
    """The chart, a slider over its points and the button that reruns with that limit.

    Hidden until a curve arrives. `on_use(limit)` gets the point's change count, or None
    for the last point (no limit).
    """

    def __init__(self, on_use):
        super().__init__()
        self.on_use = on_use
        self.data = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        title = QLabel("Tool changes against colour error")
        title.setStyleSheet(f"color: {theme.ACCENT}; font-weight: bold")
        lay.addWidget(title)
        self.chart = ToolchangeChart()
        lay.addWidget(self.chart, 1)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.valueChanged.connect(self._moved)
        lay.addWidget(self.slider)
        self.info = QLabel()
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        row = QHBoxLayout()
        self.use = QPushButton("Run with this limit")
        self.use.clicked.connect(self._use)
        row.addWidget(self.use)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(theme.hint("Each layer prints only some filaments (always the base); "
                                 "dE is simulated. The count is an upper bound: the slice "
                                 "comes in under it."))
        self.setVisible(False)

    def set_curve(self, data: dict):
        self.data = data
        pts = data["points"]
        sel = len(pts) - 1
        if data.get("chosen"):
            sel = min(range(len(pts)), key=lambda i: abs(pts[i]["changes"] - data["chosen"]["changes"]))
        self.slider.blockSignals(True)
        self.slider.setRange(0, len(pts) - 1)
        self.slider.setValue(sel)
        self.slider.blockSignals(False)
        self._moved(sel)
        self.setVisible(True)

    def show_error(self, msg: str):
        self.data = None
        self.chart.set_points([], -1)
        self.slider.setRange(0, 0)
        self.info.setText(msg)
        self.setVisible(True)

    def limit(self):
        """The selected point's limit, None for the last (unrestricted) one."""
        pts = self.data["points"]
        i = self.slider.value()
        return None if i == len(pts) - 1 else pts[i]["changes"]

    def _moved(self, i):
        if self.data is None:
            return
        pts = self.data["points"]
        self.chart.set_points(pts, i)
        self.info.setText(describe(pts[i], self.data.get("tool_change_time_s"), i == len(pts) - 1))

    def _use(self):
        if self.data is not None:
            self.on_use(self.limit())
