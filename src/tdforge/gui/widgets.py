"""FieldSpec -> Qt widget. Each builder returns a FieldWidget with get/set/enable/readonly."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLineEdit,
                               QPlainTextEdit, QPushButton, QWidget)

from tdforge.gui.argform.spec import FieldSpec

IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;All files (*)"
MODEL_FILTER = "3MF (*.3mf);;All files (*)"


class FieldWidget:
    """Uniform handle over whatever widget a kind needs; get() returns raw text (or bool)."""

    def __init__(self, widget: QWidget, getter, setter, controls=(), readonly_ctl=None):
        self.widget = widget
        self._get, self._set = getter, setter
        self._controls = list(controls)
        self._ro = readonly_ctl

    def get(self):
        return self._get()

    def set(self, v):
        self._set(v)

    def enable(self, on: bool):
        for c in self._controls:
            c.setEnabled(on)

    def is_enabled(self) -> bool:
        return all(c.isEnabled() for c in self._controls)

    refresh = None      # pickers that show database contents set this

    def readonly(self, on: bool):
        if self._ro is not None:
            self._ro.setReadOnly(on)


def _text(v):
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return " ".join(str(x) for x in v)
    return str(v)


def _entry(f: FieldSpec, on_change, browse=None, narrow=False):
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    ed = QLineEdit()
    if f.kind in ("int", "float") or narrow:
        ed.setMaximumWidth(140)
    ed.textChanged.connect(lambda *_: on_change())
    lay.addWidget(ed, 1)
    if f.kind in ("int", "float") or narrow:
        lay.addStretch(1)          # keep a capped-width box left-aligned
    controls = [ed]
    if browse:
        b = QPushButton("Browse…")
        b.clicked.connect(lambda: browse(ed))
        lay.addWidget(b)
        controls.append(b)
    w = FieldWidget(box, lambda: ed.text().strip(), lambda v: ed.setText(_text(v)), controls, ed)
    w.edit = ed
    return w


def _open(filt, title):
    def go(ed):
        p, _ = QFileDialog.getOpenFileName(ed, title, "", filt)
        if p:
            ed.setText(p)
    return go


def _save(ed):
    p, _ = QFileDialog.getSaveFileName(ed, "Save as")
    if p:
        ed.setText(p)


def _dir(ed):
    p = QFileDialog.getExistingDirectory(ed, "Choose folder")
    if p:
        ed.setText(p)


def build(f: FieldSpec, kind: str, on_change, source=None) -> FieldWidget:
    """Build the widget for `kind` (overrides.KINDS; project kinds arrive as 'entry')."""
    if kind in ("filament_id", "filament_or_hex", "filament_ids", "hex_list", "color"):
        from tdforge.gui import pickers
        src = source or pickers.FilamentSource()
        if kind == "filament_id":
            return pickers.filament_combo(f, on_change, src)
        if kind == "filament_or_hex":
            return pickers.filament_combo(f, on_change, src, allow_hex=True)
        if kind == "filament_ids":
            return pickers.filament_list(f, on_change, src)
        if kind == "color":
            return pickers.color_field(f, on_change)
        return pickers.palette_field(f, on_change, src)
    if kind == "check":
        cb = QCheckBox()
        cb.setChecked(bool(f.default))
        cb.toggled.connect(lambda *_: on_change())
        return FieldWidget(cb, cb.isChecked, lambda v: cb.setChecked(bool(v)), [cb])
    if kind == "combo":
        cb = QComboBox()
        cb.addItem("")               # nothing preselected: a required choice must be picked
        cb.addItems([str(c) for c in f.choices])
        cb.currentTextChanged.connect(lambda *_: on_change())

        def set_(v):
            i = cb.findText(_text(v))
            cb.setCurrentIndex(max(i, 0))
        return FieldWidget(cb, cb.currentText, set_, [cb])
    if kind in ("int", "float"):
        return _entry(f, on_change)
    if kind == "repeat":
        te = QPlainTextEdit()
        te.setFixedHeight(64)
        te.setPlaceholderText("one value per line")
        te.textChanged.connect(lambda *_: on_change())
        return FieldWidget(
            te, lambda: [ln.strip() for ln in te.toPlainText().splitlines() if ln.strip()],
            lambda v: te.setPlainText("\n".join(str(x) for x in (v or []))), [te], te)
    if kind in ("file_in", "model_3mf"):
        return _entry(f, on_change, _open(MODEL_FILTER if kind == "model_3mf" else "All files (*)", f.flag))
    if kind == "image":
        return _entry(f, on_change, _open(IMAGE_FILTER, "Image"))
    if kind == "file_out":
        return _entry(f, on_change, _dir if "dir" in f.dest else _save)
    return _entry(f, on_change)       # entry, list, hex_list, filament_id(s), project:*
