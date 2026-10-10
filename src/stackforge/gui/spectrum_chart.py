"""Spectrum chart and swatch strip: hand-painted (QPainter), like the rest of the GUI.

`SpectrumChart` draws one or more series of curves over 380-730 nm: each series is a list of
curves indexed by layer count; the current layer is drawn bold, the others as faint ghosts so the
build-up reads at a glance. Measured points, a hover read-out and a "synthetic" watermark are
optional. `SwatchStrip` shows the colour of every layer count, one row per series.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from stackforge.core import colormath
from stackforge.core import spectral as sp
from stackforge.gui import theme


def visible(hexcol: str, min_l=38.0) -> QColor:
    """A curve colour that shows on the dark background: very dark colours are lifted."""
    rgb = np.array(colormath.parse_hex(hexcol), float)
    lab = colormath.srgb_to_lab(rgb)
    if lab[0] >= min_l:
        return QColor(hexcol)
    t = (min_l - lab[0]) / min_l
    mix = rgb * (1 - t) + np.array([170, 170, 182]) * t
    return QColor(colormath.to_hex(mix))


def _paint_error(widget, p, exc):
    """Draw a painting failure in the widget instead of letting it escape to Qt."""
    p.fillRect(widget.rect(), QColor(theme.BG))
    p.setPen(QColor(theme.ERR))
    p.drawText(widget.rect().adjusted(12, 12, -12, -12), Qt.AlignCenter | Qt.TextWordWrap,
               f"could not draw this: {type(exc).__name__}: {exc}")


@dataclass
class Series:
    label: str
    colour: QColor
    curves: list                      # [np.ndarray (36,)] indexed by layer count
    points: np.ndarray | None = None  # measured spectrum to dot at the current layer
    dashed: bool = False
    ghosts: bool = True
    extra: dict = field(default_factory=dict)


@dataclass
class Axis:
    label: str = "reflectance"
    lo: float = 0.0
    hi: float = 1.0
    log: bool = False


class SpectrumChart(QWidget):
    hovered = Signal(float)           # wavelength under the cursor, or -1

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.series: list[Series] = []
        self.axis = Axis()
        self.layer = 0
        self.watermark = ""
        self.empty = "nothing to show"
        self.title = ""
        self._hover = None             # band index

    # ---- data ---------------------------------------------------------------------------
    def set_data(self, series, layer, axis=None, title="", watermark=""):
        self.series, self.layer = list(series), int(layer)
        self.axis = axis or Axis()
        self.title, self.watermark = title, watermark
        self.update()

    def set_layer(self, layer):
        self.layer = int(layer)
        self.update()

    def _curve(self, s: Series, i: int):
        return s.curves[min(max(i, 0), len(s.curves) - 1)] if s.curves else None

    # ---- geometry -----------------------------------------------------------------------
    def _plot_rect(self) -> QRectF:
        return QRectF(58, 34, max(10, self.width() - 58 - 18), max(10, self.height() - 34 - 58))

    def _x(self, r: QRectF, nm):
        return r.left() + (nm - sp.NM_FROM) / (sp.NM_TO - sp.NM_FROM) * r.width()

    def _y(self, r: QRectF, v):
        a = self.axis
        if a.log:
            v = np.log10(np.clip(v, 10.0 ** a.lo, 10.0 ** a.hi))
        t = (np.clip(v, a.lo, a.hi) - a.lo) / (a.hi - a.lo)
        return r.bottom() - t * r.height()

    # ---- events -------------------------------------------------------------------------
    def mouseMoveEvent(self, ev):
        r = self._plot_rect()
        x = ev.position().x()
        if r.left() <= x <= r.right():
            nm = sp.NM_FROM + (x - r.left()) / r.width() * (sp.NM_TO - sp.NM_FROM)
            self._hover = int(np.clip(round((nm - sp.NM_FROM) / sp.NM_STEP), 0, sp.NB - 1))
            self.hovered.emit(float(sp.GRID[self._hover]))
        else:
            self._hover = None
            self.hovered.emit(-1.0)
        self.update()

    def leaveEvent(self, _ev):
        self._hover = None
        self.hovered.emit(-1.0)
        self.update()

    # ---- painting -----------------------------------------------------------------------
    def paintEvent(self, _ev):
        p = QPainter(self)
        try:
            self._paint(p)
        except Exception as exc:  # noqa: BLE001 -- an exception escaping paintEvent kills PySide
            _paint_error(self, p, exc)

    def _paint(self, p):
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.BG))
        r = self._plot_rect()
        small = QFont(p.font())
        small.setPointSize(8)
        p.setFont(small)

        # grid and axes
        a = self.axis
        # floats throughout: numpy 2 keeps ceil/floor of ints integral, and an int to a
        # negative power raises -- inside paintEvent that takes the whole process down.
        ticks = (np.arange(np.ceil(float(a.lo)), np.floor(float(a.hi)) + 1.0) if a.log
                 else np.linspace(a.lo, a.hi, 6))
        for v in ticks:
            y = self._y(r, 10.0 ** v if a.log else v)
            p.setPen(QPen(QColor(theme.LINE), 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor(theme.FG_DIM))
            text = (f"{10.0 ** v:g}" if a.log else f"{v:.1f}")
            p.drawText(QRectF(0, y - 8, r.left() - 6, 16), Qt.AlignRight | Qt.AlignVCenter, text)
        for nm in range(400, 731, 50):
            x = self._x(r, nm)
            p.setPen(QPen(QColor(theme.LINE), 1, Qt.DotLine))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(QColor(theme.FG_DIM))
            p.drawText(QRectF(x - 20, r.bottom() + 22, 40, 14), Qt.AlignCenter, str(nm))
        # the visible spectrum under the axis
        n = max(2, int(r.width()))
        band = sp.wavelength_srgb(np.linspace(sp.NM_FROM, sp.NM_TO, n))
        for i, c in enumerate(band):
            p.setPen(QColor(int(c[0]), int(c[1]), int(c[2])))
            x = r.left() + i * r.width() / n
            p.drawLine(QPointF(x, r.bottom() + 6), QPointF(x, r.bottom() + 16))
        p.setPen(QColor(theme.FG_DIM))
        p.drawText(QRectF(r.left(), r.bottom() + 36, r.width(), 16), Qt.AlignCenter, "wavelength, nm")
        p.save()
        p.translate(14, r.center().y())
        p.rotate(-90)
        p.drawText(QRectF(-80, -8, 160, 16), Qt.AlignCenter, a.label)
        p.restore()
        title = QFont(p.font())
        title.setPointSize(10)
        p.setFont(title)
        p.setPen(QColor(theme.FG))
        p.drawText(QRectF(r.left(), 6, r.width(), 22), Qt.AlignLeft | Qt.AlignVCenter, self.title)
        p.setFont(small)

        if not self.series:
            p.setPen(QColor(theme.FG_DIM))
            p.drawText(r.adjusted(30, 30, -30, -30), Qt.AlignCenter | Qt.TextWordWrap, self.empty)
        else:
            p.setClipRect(r.adjusted(-2, -2, 2, 2))
            for s in self.series:                      # ghosts first, under everything
                if not s.ghosts:
                    continue
                for i, c in enumerate(s.curves):
                    if i == self.layer:
                        continue
                    col = QColor(s.colour)
                    col.setAlpha(55)
                    self._draw_curve(p, r, c, QPen(col, 1))
            for s in self.series:
                c = self._curve(s, self.layer)
                if c is not None:
                    pen = QPen(s.colour, 2.4)
                    if s.dashed:
                        pen.setStyle(Qt.DashLine)
                    self._draw_curve(p, r, c, pen)
                if s.points is not None:
                    p.setPen(QPen(s.colour, 1.4))
                    p.setBrush(QColor(theme.BG))
                    for nm, v in zip(sp.GRID, s.points):
                        p.drawEllipse(QPointF(self._x(r, nm), self._y(r, v)), 3.2, 3.2)
                    p.setBrush(Qt.NoBrush)
            p.setClipping(False)
            self._draw_hover(p, r)

        if self.watermark:
            wm = QFont(p.font())
            wm.setPointSize(22)
            wm.setBold(True)
            p.setFont(wm)
            col = QColor(theme.ERR)
            col.setAlpha(70)
            p.setPen(col)
            p.drawText(r, Qt.AlignCenter, self.watermark)

    def _draw_curve(self, p, r, c, pen):
        path = QPainterPath()
        for k, (nm, v) in enumerate(zip(sp.GRID, c)):
            pt = QPointF(self._x(r, nm), self._y(r, v))
            if k == 0:
                path.moveTo(pt)
            else:
                path.lineTo(pt)
        p.setPen(pen)
        p.drawPath(path)

    def _draw_hover(self, p, r):
        if self._hover is None:
            return
        b = self._hover
        x = self._x(r, sp.GRID[b])
        p.setPen(QPen(QColor(theme.FG_DIM), 1, Qt.DashLine))
        p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        lines = [f"{sp.GRID[b]:.0f} nm"]
        for s in self.series:
            c = self._curve(s, self.layer)
            if c is not None:
                lines.append((s.label, s.colour, float(c[b])))
        fm = p.fontMetrics()
        w = max([fm.horizontalAdvance(lines[0])] +
                [fm.horizontalAdvance(f"{lbl}  {v:.3g}") for lbl, _, v in lines[1:]]) + 26
        h = 16 * len(lines) + 8
        bx = x + 12 if x + 12 + w < r.right() else x - 12 - w
        box = QRectF(bx, r.top() + 6, w, h)
        p.setPen(QPen(QColor(theme.LINE), 1))
        p.setBrush(QColor(theme.BG3))
        p.drawRoundedRect(box, 4, 4)
        p.setBrush(Qt.NoBrush)
        p.setPen(QColor(theme.FG))
        p.drawText(QRectF(bx + 8, box.top() + 4, w, 16), Qt.AlignLeft | Qt.AlignVCenter, lines[0])
        for k, (lbl, col, v) in enumerate(lines[1:], 1):
            y = box.top() + 4 + 16 * k
            p.fillRect(QRectF(bx + 8, y + 5, 8, 8), col)
            p.setPen(QColor(theme.FG))
            p.drawText(QRectF(bx + 20, y, w, 16), Qt.AlignLeft | Qt.AlignVCenter, f"{lbl}  {v:.3g}")


class SwatchStrip(QWidget):
    """Rows of colour cells, one per layer count; click a cell to pick that layer."""
    picked = Signal(int)

    def __init__(self):
        super().__init__()
        self.rows: list[tuple[str, list[str]]] = []
        self.current = 0
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._resize()

    def set_rows(self, rows, current):
        self.rows, self.current = list(rows), int(current)
        self._resize()
        self.update()

    def _resize(self):
        self.setFixedHeight(max(30, 24 * len(self.rows) + 20))

    def _geom(self):
        left = 110
        n = max((len(h) for _, h in self.rows), default=1)
        return left, (self.width() - left - 8) / max(n, 1)

    def mousePressEvent(self, ev):
        left, cw = self._geom()
        x = ev.position().x() - left
        if x >= 0 and self.rows:
            self.picked.emit(int(x // cw))

    def paintEvent(self, _ev):
        p = QPainter(self)
        try:
            self._paint(p)
        except Exception as exc:  # noqa: BLE001 -- see SpectrumChart.paintEvent
            _paint_error(self, p, exc)

    def _paint(self, p):
        p.fillRect(self.rect(), QColor(theme.BG))
        small = QFont(p.font())
        small.setPointSize(8)
        p.setFont(small)
        left, cw = self._geom()
        for k, (label, hexes) in enumerate(self.rows):
            y = 4 + 24 * k
            p.setPen(QColor(theme.FG_DIM))
            p.drawText(QRectF(4, y, left - 10, 20), Qt.AlignRight | Qt.AlignVCenter,
                       p.fontMetrics().elidedText(label, Qt.ElideRight, left - 12))
            for i, hx in enumerate(hexes):
                cell = QRectF(left + i * cw, y, max(cw - 1, 1), 20)
                p.fillRect(cell, QColor(hx))
                if i == self.current:
                    p.setPen(QPen(QColor(theme.FG), 2))
                    p.drawRect(cell.adjusted(1, 1, -1, -1))
        if self.rows:
            n = max(len(h) for _, h in self.rows)
            p.setPen(QColor(theme.FG_DIM))
            step = max(1, round(n / 16))
            y = 4 + 24 * len(self.rows)
            for i in range(0, n, step):
                p.drawText(QRectF(left + i * cw, y, cw, 14), Qt.AlignHCenter | Qt.AlignTop, str(i))
