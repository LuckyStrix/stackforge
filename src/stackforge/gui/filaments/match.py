"""Match-by-eye page: fit a filament's td by comparing a printed wedge against the model.

Nobody without an instrument can name a colour accurately, but anyone can say which of two pairs
has the bigger step between them. So every candidate is drawn as patch-against-base, and the
judgement asked for is a comparison of contrast rather than of absolute colour -- which is also
what makes the screen's white point and the room's lighting mostly cancel out.
"""
from __future__ import annotations

from datetime import date

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)

from stackforge.core import colormath
from stackforge.core.optics import best_layers, patch_rgb
from stackforge.gui import theme
from stackforge.gui.filaments.common import spin

N_CANDIDATES = 9
SPAN = 2.5                      # widest td ratio offered either way


class Candidates(QWidget):
    """The row of patch-vs-base squares; a click picks one."""

    def __init__(self, page):
        super().__init__()
        self.page = page
        self.hit = []
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def mousePressEvent(self, e):
        x = e.position().x()
        for x0, x1, td, ratio in self.hit:
            if x0 <= x <= x1:
                self.page.apply(td, ratio)
                return

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.BG))
        self.hit = []
        pg = self.page
        fil, base = pg.ed.fil(), pg.base()
        w, h = self.width(), self.height()
        if fil is None or base is None or w < 60 or h < 60:
            return
        n, lh = pg.layers.value(), pg.ed.layer.value
        base_lin = base.linear()
        ratios = np.exp(np.linspace(-np.log(SPAN), np.log(SPAN), N_CANDIDATES))
        pad, top = 10, 26
        cw = (w - 2 * pad) / len(ratios)
        ph = max(120, h - top - 72)     # big patches: the whole judgement is visual
        small = QFont(p.font())
        small.setPointSize(8)
        p.setFont(small)
        for i, ratio in enumerate(ratios):
            td = fil.td * ratio
            x0 = pad + i * cw
            patch = colormath.to_hex(patch_rgb(fil, base_lin, n, lh, td))
            # base on the left, filament-over-base on the right: the eye judges the step
            p.fillRect(QRectF(x0, top, cw / 2, ph), QColor(base.color))
            p.fillRect(QRectF(x0 + cw / 2, top, cw / 2 - 4, ph), QColor(patch))
            current = abs(ratio - 1.0) < 1e-9
            p.setPen(QPen(QColor(theme.ACCENT if current else theme.LINE), 2 if current else 1))
            p.drawRect(QRectF(x0 - 1, top - 1, cw - 2, ph + 2))
            p.setPen(QColor(theme.ACCENT if current else theme.FG_DIM))
            p.drawText(QRectF(x0, 0, cw - 4, top - 4), Qt.AlignCenter, "now" if current else f"×{ratio:.2f}")
            p.setPen(QColor(theme.FG_DIM))
            p.drawText(QRectF(x0, top + ph + 2, cw - 4, 18), Qt.AlignCenter, f"{td:.3f}")
            self.hit.append((x0, x0 + cw - 4, td, ratio))
        p.setPen(QColor(theme.FG))
        bold = QFont(p.font())
        bold.setBold(True)
        bold.setPointSize(10)
        p.setFont(bold)
        p.drawText(pad, int(top + ph + 22), w - pad, 20, Qt.AlignLeft,
                   f"print {n} layer{'s' if n > 1 else ''} × {lh:g} mm = {n * lh:.2f} mm of "
                   f"{fil.name}, over {base.name}")
        try:       # the base has to be opaque, or the patch is partly the build plate
            need = int(np.ceil(base.td_vec().max() * 4.6 / lh))
        except SystemExit:
            need = 0
        if need:
            demanding = need * lh > 1.0
            p.setFont(small)
            p.setPen(QColor(theme.WARN if demanding else theme.FG_DIM))
            p.drawText(pad, int(top + ph + 44), w - pad, 18, Qt.AlignLeft,
                       f"the {base.name} underneath has to be opaque: {need} layers "
                       f"({need * lh:.2f} mm)."
                       + ("  Thinner and you are partly measuring the build plate." if demanding
                          else "  Left half of each square is that bare base."))


class MatchPage(QWidget):
    def __init__(self, editor):
        super().__init__()
        self.ed = editor
        col = QVBoxLayout(self)
        col.setContentsMargins(12, 12, 12, 12)
        head = QLabel("Match a printed patch by eye")
        head.setStyleSheet("font-weight: bold")
        col.addWidget(head)
        col.addWidget(theme.hint(
            "Print a wedge, then hold it against the screen and pick the square whose step from "
            "the base colour looks like yours. Compare the DIFFERENCE between the two halves, not "
            "the colours themselves — that is what survives the screen being lit differently from "
            "the plastic."))
        ctl = QHBoxLayout()
        ctl.addWidget(QLabel("Over"))
        self.cb_base = QComboBox()
        self.cb_base.setMinimumWidth(220)
        self.cb_base.currentIndexChanged.connect(lambda *_: self.canvas.update())
        ctl.addWidget(self.cb_base)
        ctl.addSpacing(12)
        ctl.addWidget(QLabel("Layers"))
        self.layers = spin(1, 40, 1, 1)
        self.layers.valueChanged.connect(lambda *_: self.canvas.update())
        ctl.addWidget(self.layers)
        ctl.addWidget(QLabel("× layer height"))
        ctl.addWidget(editor.layer.spin())
        best = QPushButton("Best layer count")
        best.clicked.connect(self._recommend)
        wedge = QPushButton("Calibrate with a wedge…")
        wedge.clicked.connect(self._write_wedge)
        ctl.addSpacing(12)
        ctl.addWidget(best)
        ctl.addWidget(wedge)
        ctl.addStretch(1)
        col.addLayout(ctl)
        self.rec = theme.hint("")
        col.addWidget(self.rec)
        self.canvas = Candidates(self)
        col.addWidget(self.canvas, 1)
        self.picked = QLabel("")
        col.addWidget(self.picked)
        editor.layer.changed.connect(lambda *_: self.canvas.update())
        editor.edited.connect(self.refresh)

    def base(self):
        return self.ed.db.filaments.get(self.cb_base.currentText())

    def refresh(self):
        fil = self.ed.fil()
        if fil is None:
            return
        names = [f.id for f in self.ed.db.filaments.values() if f.id != fil.id]
        if names != [self.cb_base.itemText(i) for i in range(self.cb_base.count())]:
            keep = self.cb_base.currentText()
            self.cb_base.blockSignals(True)
            self.cb_base.clear()
            self.cb_base.addItems(names)
            white = next((n for n in names if "white" in n), names[0] if names else "")
            self.cb_base.setCurrentText(keep if keep in names else white)
            self.cb_base.blockSignals(False)
        self.canvas.update()

    def _recommend(self):
        fil, base = self.ed.fil(), self.base()
        if fil is None or base is None:
            return
        lh = self.ed.layer.value
        n, de = best_layers(fil, base.color, lh)
        self.layers.setValue(n)
        mm = f"{n} layers = {n * lh:.2f} mm of {fil.name}"
        if de < 3:
            self.rec.setText(f"{mm} over {base.name} is the best this pairing can do, and it is "
                             f"still weak (dE {de:.1f} for a 1.6x td error). Try a more contrasting "
                             "base — a translucent filament shows almost nothing over white.")
            self.rec.setObjectName("warn")
        else:
            self.rec.setText(f"{mm} over {base.name}: a 1.6x td error moves this patch by dE "
                             f"{de:.1f}, so picking the closest square should pin td to roughly "
                             "±15–20%.")
            self.rec.setObjectName("hint")
        self.rec.style().unpolish(self.rec)
        self.rec.style().polish(self.rec)
        self.canvas.update()

    def _write_wedge(self):
        """Hand the wedge writer the base and depth this page is asking about."""
        self.ed.request_calibrate(steps=max(4, self.layers.value()), base_id=self.cb_base.currentText())

    def apply(self, td, ratio):
        fil, base = self.ed.fil(), self.base()
        if fil is None or base is None:
            return
        if abs(ratio - 1.0) < 1e-9:
            self.picked.setText("that is the current value — nothing changed")
            return
        n, lh, old = self.layers.value(), self.ed.layer.value, fil.td
        fil.td, fil.td_rgb = round(float(td), 4), None
        note = (f"td set by eye: matched a printed {n}-layer patch ({n * lh:.2f} mm at {lh:g} mm "
                f"layers) over {base.name} on {date.today().isoformat()}")
        if note not in (fil.notes or ""):
            fil.notes = ((fil.notes + " | ").lstrip(" |") + note) if fil.notes else note
        fil.provenance = "matched"
        self.ed.reload_fields()
        self.ed.touch()
        self.picked.setText(f"td {old:.4f} → {td:.4f}  (×{ratio:.2f})  — provenance now 'matched'")
