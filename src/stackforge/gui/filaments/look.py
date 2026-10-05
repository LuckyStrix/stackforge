"""Look page: the selected filament stacked over white and over black, plus its optics table."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from stackforge.core import colormath
from stackforge.core.optics import PREVIEW_BASES, opaque_at, optics_lines, stack_colors
from stackforge.gui import theme
from stackforge.gui.filaments.common import report_box, show_lines, spin


class StackPreview(QWidget):
    """Two ramps of the selected filament, over white and over black."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(190)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._fil, self._layers, self._lh = None, 12, 0.08

    def show_stack(self, fil, layers, layer_h):
        self._fil, self._layers, self._lh = fil, layers, layer_h
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.BG))
        w, h = self.width(), self.height()
        p.setPen(QColor(theme.FG_DIM))
        if self._fil is None:
            p.drawText(self.rect(), Qt.AlignCenter, "select a filament")
            return
        try:
            ramps = [(lbl, stack_colors(self._fil, hexcol, self._layers, self._lh))
                     for lbl, hexcol in PREVIEW_BASES]
        except SystemExit as exc:              # bad td / td_rgb
            p.setPen(QColor(theme.ERR))
            p.drawText(self.rect().adjusted(10, 10, -10, -10), Qt.AlignCenter | Qt.TextWordWrap, str(exc))
            return
        n = self._layers + 1
        left, top, gap = 8, 22, 24
        cw = max(6.0, (w - 2 * left) / n)
        rh = max(18.0, (h - top - 34 - gap) / 2)
        opq = opaque_at(self._fil, self._lh)
        small = QFont(p.font())
        small.setPointSize(8)
        p.setFont(small)
        for r, (label, ramp) in enumerate(ramps):
            y0 = top + r * (rh + gap)
            p.setPen(QColor(theme.FG_DIM))
            p.drawText(left, int(y0 - 7), label)
            for i, rgb in enumerate(ramp):
                x0 = left + i * cw
                p.fillRect(int(x0), int(y0), int(cw - 1) + 1, int(rh), QColor(colormath.to_hex(rgb)))
                if opq is not None and i == opq:
                    p.setPen(QPen(QColor(theme.ACCENT), 2))
                    p.drawLine(int(x0), int(y0 - 3), int(x0), int(y0 + rh + 3))
        y = top + 2 * (rh + gap) - gap + 6
        p.setPen(QColor(theme.FG_DIM))
        step = max(1, round(n / 12))
        for i in range(0, n, step):
            p.drawText(int(left + i * cw), int(y), int(cw), 14, Qt.AlignHCenter | Qt.AlignTop, str(i))
        note = f"{self._layers} layers = {self._layers * self._lh:.2f} mm"
        if opq:
            note += f"   ·   opaque (T<1%) at {opq} layers = {opq * self._lh:.2f} mm"
            colour = theme.OK if 4 <= opq <= 14 else theme.WARN
        else:
            note += f"   ·   still translucent at {self._layers} layers"
            colour = theme.WARN
        p.setPen(QColor(colour))
        p.drawText(left, int(y + 16), w - 2 * left, 16, Qt.AlignLeft | Qt.AlignTop, note)


class LookPage(QWidget):
    def __init__(self, editor):
        super().__init__()
        self.ed = editor
        col = QVBoxLayout(self)
        col.setContentsMargins(12, 12, 12, 12)
        head = QLabel("Stacked over white and over black")
        head.setStyleSheet("font-weight: bold")
        col.addWidget(head)
        col.addWidget(theme.hint(
            "Composited with the same model the plaque designer uses. This is the quickest check that a td "
            "is sane: a filament that goes opaque in two layers, or is still see-through at twenty, "
            "will not behave in a plaque the way the numbers claim."))
        ctl = QHBoxLayout()
        ctl.addWidget(QLabel("Layer height"))
        ctl.addWidget(editor.layer.spin())
        ctl.addSpacing(16)
        ctl.addWidget(QLabel("Layers"))
        self.layers = spin(2, 40, 1, 14)
        self.layers.valueChanged.connect(lambda *_: self.refresh())
        ctl.addWidget(self.layers)
        ctl.addStretch(1)
        col.addLayout(ctl)
        self.preview = StackPreview()
        col.addWidget(self.preview, 1)
        self.report = report_box(200)
        col.addWidget(self.report)
        editor.layer.changed.connect(lambda *_: self.refresh())
        editor.edited.connect(self.refresh)

    def refresh(self):
        fil = self.ed.fil()
        if fil is None:
            return
        self.preview.show_stack(fil, self.layers.value(), self.ed.layer.value)
        show_lines(self.report, optics_lines(fil, self.ed.layer.value))
