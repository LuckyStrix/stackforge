"""Small helpers shared by the filament editor's pages."""
from __future__ import annotations

import html

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QDoubleSpinBox, QFormLayout, QGroupBox, QScrollArea, QSpinBox,
                               QTextEdit, QVBoxLayout, QWidget)

from stackforge.gui import theme

KIND_COLOUR = {"head": theme.FG, "dim": theme.FG_DIM, "ok": theme.OK, "warn": theme.WARN,
               "err": theme.ERR, "": theme.FG}


class SharedValue(QObject):
    """One number several pages show: the layer height is a property of how you print, not of a
    page, so the preview, the by-eye match and the wedge must all assume the same one."""
    changed = Signal(float)

    def __init__(self, value):
        super().__init__()
        self._v = float(value)

    @property
    def value(self) -> float:
        return self._v

    def set(self, v):
        v = float(v)
        if v != self._v:
            self._v = v
            self.changed.emit(v)

    def spin(self, lo=0.04, hi=0.3) -> QDoubleSpinBox:
        """A spin box that edits this value and follows it."""
        s = spin(lo, hi, 0.01, self._v, 2)
        s.valueChanged.connect(self.set)
        self.changed.connect(lambda v: s.value() != v and s.setValue(v))
        return s


def spin(lo, hi, step, value, decimals=None) -> QSpinBox | QDoubleSpinBox:
    w = QSpinBox() if decimals is None else QDoubleSpinBox()
    if decimals is not None:
        w.setDecimals(decimals)
    w.setRange(lo, hi)
    w.setSingleStep(step)
    w.setValue(value)
    w.setMaximumWidth(110)
    w.setKeyboardTracking(False)
    return w


def scroll_page():
    """(scroll area, inner layout): a page that scrolls when the window is small."""
    sc = QScrollArea()
    sc.setWidgetResizable(True)
    host = QWidget()
    col = QVBoxLayout(host)
    col.setContentsMargins(8, 8, 8, 8)
    sc.setWidget(host)
    return sc, col


def section(col, title, note=None) -> QFormLayout:
    """A titled group in `col`; returns its form layout."""
    g = QGroupBox(title)
    gl = QVBoxLayout(g)
    if note:
        gl.addWidget(theme.hint(note))
    f = QFormLayout()
    f.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
    f.setVerticalSpacing(6)
    gl.addLayout(f)
    col.addWidget(g)
    return f


def report_box(height=None) -> QTextEdit:
    t = QTextEdit()
    t.setReadOnly(True)
    theme.mono(t)
    if height:
        t.setMinimumHeight(height)
    return t


def show_lines(box: QTextEdit, lines):
    """Fill a report box from (text, kind) lines; whitespace is kept, colour follows kind."""
    rows = []
    for text, kind in lines:
        weight = "bold" if kind == "head" else "normal"
        rows.append(f'<span style="color:{KIND_COLOUR.get(kind, theme.FG)};font-weight:{weight}">'
                    f'{html.escape(text) or "&nbsp;"}</span>')
    box.setHtml('<pre style="margin:0">' + "<br>".join(rows) + "</pre>")
