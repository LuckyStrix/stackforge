"""Semantic pickers: choose filaments and colours by looking at them, never by typing an id.

Every picker is a FieldWidget whose get() returns the raw text the CLI wants (ids joined by
commas, or hex colours), so the argv/preset/exclusion logic is unchanged.
"""
from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QColorDialog, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
                               QWidget)

from stackforge.core import colormath
from stackforge.core.filamentdb import DB, DEFAULT_DB
from stackforge.gui import theme
from stackforge.gui.widgets import FieldWidget


def swatch_icon(hexcol, w=18, h=14) -> QIcon:
    pm = QPixmap(w, h)
    pm.fill(QColor(hexcol))
    return QIcon(pm)


class FilamentSource:
    """The filaments of the project's database, re-read when the file changes."""

    def __init__(self, project=None, path=None):
        self.project, self._path = project, path
        self._db, self._mtime = None, None

    @property
    def path(self):
        return (self.project.get("db") if self.project else None) or self._path or DEFAULT_DB

    def filaments(self) -> list:
        p = self.path
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            mtime = None
        if self._db is None or self._db.path != p or mtime != self._mtime:
            try:
                self._db, self._mtime = DB(p), mtime
            except SystemExit:
                self._db, self._mtime = None, mtime
                return []
        return sorted(self._db.filaments.values(), key=lambda f: (f.brand, f.series, f.name))


def _is_hex(s: str) -> bool:
    try:
        colormath.parse_hex(s)
        return True
    except ValueError:
        return False


def _norm_hex(s: str) -> str:
    s = s.strip()
    return colormath.to_hex(colormath.parse_hex(s)) if _is_hex(s) else s


# ---- one filament ----------------------------------------------------------------------------
def filament_combo(f, on_change, src: FilamentSource, allow_hex=False) -> FieldWidget:
    cb = QComboBox()
    cb.setEditable(allow_hex)
    cb.setMinimumWidth(220)
    if allow_hex:
        cb.lineEdit().setPlaceholderText("pick a filament, or type a hex colour like #E8E8EE")

    def fill(keep=None):
        cur = keep if keep is not None else get()
        cb.blockSignals(True)
        cb.clear()
        cb.addItem("" if not allow_hex else "")
        for fil in src.filaments():
            cb.addItem(swatch_icon(fil.color), fil.label(), fil.id)
        cb.blockSignals(False)
        set_(cur)

    def get():
        text = cb.currentText().strip()
        i = cb.findText(text)
        if i > 0:
            return cb.itemData(i)
        if allow_hex and text:
            return _norm_hex(text)
        return ""

    def set_(v):
        v = "" if v is None else str(v)
        i = next((j for j in range(1, cb.count()) if cb.itemData(j) == v), -1)
        if i < 0:
            i = cb.findText(v)
        if i >= 0:
            cb.setCurrentIndex(i)
        elif allow_hex and v:
            cb.setEditText(v)
        else:
            cb.setCurrentIndex(0)

    cb.currentTextChanged.connect(lambda *_: on_change())
    w = FieldWidget(cb, get, set_, [cb])
    w.refresh = lambda: fill()
    fill("")
    return w


# ---- several filaments, in order -------------------------------------------------------------
class FilamentChecklist(QDialog):
    """Tick filaments and order them (the order is the extruder order)."""

    def __init__(self, filaments, chosen, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose filaments")
        self.resize(420, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(theme.hint("Tick the filaments to use. Order matters for extruder order: "
                                 "select a row and move it with the arrows."))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter…")
        self.filter.textChanged.connect(self._apply_filter)
        lay.addWidget(self.filter)
        self.list = QListWidget()
        self.list.setIconSize(QSize(18, 14))
        by_id = {f.id: f for f in filaments}
        order = [by_id[i] for i in chosen if i in by_id] + [f for f in filaments if f.id not in chosen]
        for fil in order:
            it = QListWidgetItem(swatch_icon(fil.color), fil.label())
            it.setData(Qt.UserRole, fil.id)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if fil.id in chosen else Qt.Unchecked)
            self.list.addItem(it)
        row = QHBoxLayout()
        row.addWidget(self.list, 1)
        side = QVBoxLayout()
        for text, d in (("▲", -1), ("▼", 1)):
            b = QPushButton(text)
            b.setMaximumWidth(36)
            b.clicked.connect(lambda _=False, d=d: self._move(d))
            side.addWidget(b)
        side.addStretch(1)
        row.addLayout(side)
        lay.addLayout(row, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _apply_filter(self):
        needle = self.filter.text().strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(needle) and needle not in it.text().lower())

    def _move(self, d):
        r = self.list.currentRow()
        if r < 0 or not 0 <= r + d < self.list.count():
            return
        it = self.list.takeItem(r)
        self.list.insertItem(r + d, it)
        self.list.setCurrentRow(r + d)

    def chosen(self) -> list:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]


def filament_list(f, on_change, src: FilamentSource) -> FieldWidget:
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    label = QLabel()
    label.setWordWrap(True)
    btn = QPushButton("Choose…")
    lay.addWidget(label, 1)
    lay.addWidget(btn)
    state = {"ids": []}
    sep = " " if f.nargs is not None else ","     # nargs fields take separate arguments

    def show():
        by_id = {x.id: x for x in src.filaments()}
        names = [by_id[i].name if i in by_id else i for i in state["ids"]]
        label.setText(f"{len(names)}: " + ", ".join(names) if names else "none chosen")
        label.setObjectName("" if names else "hint")
        label.setStyleSheet("" if names else f"color: {theme.FG_DIM}")

    def choose():
        dlg = FilamentChecklist(src.filaments(), state["ids"], box)
        if dlg.exec():
            state["ids"] = dlg.chosen()
            show()
            on_change()

    def set_(v):
        if isinstance(v, (list, tuple)):
            ids = [str(x) for x in v]
        else:
            ids = [x for x in str(v or "").replace(",", " ").split() if x]
        state["ids"] = ids
        show()
        on_change()

    btn.clicked.connect(choose)
    w = FieldWidget(box, lambda: sep.join(state["ids"]), set_, [btn, label])
    w.refresh = show
    show()
    return w


# ---- colours ---------------------------------------------------------------------------------
class SwatchStrip(QWidget):
    """A row of colour chips for a comma/space separated hex list."""

    def __init__(self):
        super().__init__()
        self.colors: list[str] = []
        self.setFixedHeight(22)
        self.hide()

    def set_text(self, text: str):
        out = []
        for tok in text.replace(",", " ").split():
            if _is_hex(tok):
                out.append(_norm_hex(tok))
        self.colors = out
        self.setVisible(bool(out))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        x = 0
        for c in self.colors[:40]:
            p.fillRect(x, 2, 18, 18, QColor(c))
            p.setPen(QColor(theme.LINE))
            p.drawRect(x, 2, 17, 17)
            x += 22


def _pick_color(parent, current="#808080") -> str | None:
    col = QColorDialog.getColor(QColor(current if _is_hex(current) else "#808080"), parent, "Choose a colour")
    return col.name().upper() if col.isValid() else None


def color_field(f, on_change) -> FieldWidget:
    """One colour: hex text, a live chip and a picker."""
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    ed = QLineEdit()
    ed.setPlaceholderText("#RRGGBB")
    ed.setMaximumWidth(120)
    chip = QLabel()
    chip.setFixedSize(26, 22)
    btn = QPushButton("Pick…")
    lay.addWidget(ed)
    lay.addWidget(chip)
    lay.addWidget(btn)
    lay.addStretch(1)

    def paint():
        ok = _is_hex(ed.text())
        chip.setStyleSheet(f"background: {_norm_hex(ed.text()) if ok else 'transparent'}; "
                           f"border: 1px solid {theme.LINE};")

    def pick():
        c = _pick_color(box, ed.text())
        if c:
            ed.setText(c)

    ed.textChanged.connect(lambda *_: (paint(), on_change()))
    btn.clicked.connect(pick)
    paint()
    return FieldWidget(box, lambda: _norm_hex(ed.text()) if ed.text().strip() else "",
                       lambda v: ed.setText("" if v is None else str(v)), [ed, btn], ed)


def palette_field(f, on_change, src: FilamentSource) -> FieldWidget:
    """A list of colours: type or paste hex, add with a picker, or take them from filaments."""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    ed = QLineEdit()
    ed.setPlaceholderText("#RRGGBB, #RRGGBB, …  (or add below)")
    strip = SwatchStrip()
    row = QHBoxLayout()
    add = QPushButton("+ Colour…")
    fil = QPushButton("From filaments…")
    clear = QPushButton("Clear")
    for b in (add, fil, clear):
        row.addWidget(b)
    row.addStretch(1)
    lay.addWidget(ed)
    lay.addWidget(strip)
    lay.addLayout(row)

    def append(hexcol):
        t = ed.text().strip().rstrip(",")
        ed.setText((t + ", " if t else "") + hexcol)

    def add_color():
        c = _pick_color(box)
        if c:
            append(c)

    def from_filaments():
        dlg = FilamentChecklist(src.filaments(), [], box)
        dlg.setWindowTitle("Take colours from filaments")
        if dlg.exec():
            by_id = {x.id: x for x in src.filaments()}
            for i in dlg.chosen():
                append(colormath.to_hex(by_id[i].rgb()))

    add.clicked.connect(add_color)
    fil.clicked.connect(from_filaments)
    clear.clicked.connect(ed.clear)
    ed.textChanged.connect(lambda *_: (strip.set_text(ed.text()), on_change()))
    sep = " " if f.nargs is not None else ","

    def get():
        toks = [_norm_hex(t) for t in ed.text().replace(",", " ").split()]
        return sep.join(toks)

    return FieldWidget(box, get, lambda v: ed.setText(" ".join(v) if isinstance(v, (list, tuple))
                                                      else ("" if v is None else str(v))),
                       [ed, add, fil, clear], ed)
