"""ProjectBar: the strip across the top, bound to Project/Settings and shared by every tab."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QToolButton, QVBoxLayout, QWidget)

from stackforge.gui.project import Project

TEMPLATE_HELP = (
    "Your Flash Studio project, saved as a .3mf. stackforge copies your printer, filament and "
    "print settings from it and builds the plaque on its layer height.\n\n"
    "To make one: open Flash Studio, choose the Creator 5 printer and your print profile "
    "(e.g. 0.12 mm), set up all four filament slots, then File > Save Project As… and "
    "pick that file here. Make it once; re-save it if you change the profile.")


class ProjectBar(QWidget):
    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.project = project
        self._loading = True
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(6)
        self.db = QLineEdit(project.get("db"))
        self.template = QLineEdit(project.settings.get("template"))
        self.flavor = QComboBox()
        self.flavor.addItems(["orca", "prusa"])
        self.flavor.setCurrentText(project.settings.get("flavor"))
        self.part = QComboBox()
        self.part.addItems(["modifier", "part"])
        self.part.setCurrentText(project.settings.get("part_type"))
        self.lh, self.fl = QLineEdit(), QLineEdit()
        for e in (self.lh, self.fl):
            e.setFixedWidth(70)
        self.override = QCheckBox("override")
        self.override.setChecked(project.override)
        self.warn = QLabel()
        self.warn.setObjectName("warn")
        self.warn.setWordWrap(True)

        self.template.setPlaceholderText("choose your Flash Studio project file (.3mf)…")
        self.layers_note = QLabel()
        self.layers_note.setObjectName("hint")
        self.advanced = QToolButton()
        self.advanced.setText("Advanced")
        self.advanced.setCheckable(True)
        self.advanced.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.advanced.setArrowType(Qt.RightArrow)
        self.advanced.setToolTip("Filament database, slicer flavour, part type and a manual "
                                 "layer height. The defaults are right for Flash Studio.")

        r1 = QHBoxLayout()
        lbl = QLabel("Slicer project")
        lbl.setToolTip(TEMPLATE_HELP)
        self.template.setToolTip(TEMPLATE_HELP)
        r1.addWidget(lbl)
        r1.addWidget(self.template, 1)
        r1.addWidget(self._browse(self.template, "Slicer project (*.3mf);;All files (*)",
                                  "Choose your Flash Studio project (.3mf)", "Choose…"))
        r1.addSpacing(12)
        r1.addWidget(self.layers_note)
        r1.addSpacing(12)
        r1.addWidget(self.advanced)

        self.adv_row = QWidget()
        r2 = QHBoxLayout(self.adv_row)
        r2.setContentsMargins(0, 0, 0, 0)
        r2.addWidget(QLabel("Filament database"))
        r2.addWidget(self.db, 1)
        r2.addWidget(self._browse(self.db, "JSON (*.json);;All files (*)",
                                  "Choose a filament database"))
        r2.addSpacing(12)
        for text, w in (("Slicer flavor", self.flavor), ("Part type", self.part),
                        ("Layer height", self.lh), ("First layer", self.fl)):
            r2.addWidget(QLabel(text))
            r2.addWidget(w)
            r2.addSpacing(8)
        r2.addWidget(self.override)
        self.override.setText("set by hand")
        self.adv_row.setVisible(False)
        self.advanced.toggled.connect(self._toggle_advanced)
        outer.addLayout(r1)
        outer.addWidget(self.adv_row)
        outer.addWidget(self.warn)

        self.db.editingFinished.connect(lambda: self._edited("db", self.db.text().strip()))
        self.template.editingFinished.connect(lambda: self._edited("template", self.template.text().strip()))
        self.flavor.currentTextChanged.connect(lambda v: self._edited("flavor", v))
        self.part.currentTextChanged.connect(lambda v: self._edited("part_type", v))
        self.lh.editingFinished.connect(lambda: self._edited("layer_height", self.lh.text().strip()))
        self.fl.editingFinished.connect(lambda: self._edited("first_layer", self.fl.text().strip()))
        self.override.toggled.connect(self._override_changed)
        self._loading = False
        self.sync()

    def _toggle_advanced(self, on):
        self.adv_row.setVisible(on)
        self.advanced.setArrowType(Qt.DownArrow if on else Qt.RightArrow)

    def _browse(self, edit: QLineEdit, filt: str, title="Choose file", text="…") -> QPushButton:
        b = QPushButton(text)
        if text == "…":
            b.setMaximumWidth(32)

        def go():
            p, _ = QFileDialog.getOpenFileName(self, title, edit.text(), filt)
            if p:
                edit.setText(p)
                edit.editingFinished.emit()
        b.clicked.connect(go)
        return b

    def _edited(self, key, value):
        if self._loading:
            return
        if key in ("layer_height", "first_layer") and not self.override.isChecked():
            return
        self.project.set(key, value)
        self.sync()

    def _override_changed(self, on):
        if self._loading:
            return
        if on:
            # start the override from what the template says, so it is an edit, not a blank
            lh, fl = self.project.derived_layers()
            for k, v in (("layer_height", lh), ("first_layer", fl)):
                if v is not None and not self.project.settings.get(k):
                    self.project.settings.set(k, f"{v:g}")
        self.project.set("layer_override", bool(on))
        self.sync()

    def sync(self):
        """Refresh the layer boxes (derived unless overridden) and the warning."""
        self._loading = True
        try:
            on = self.override.isChecked()
            if on:
                lh, fl = (self.project.settings.get(k) for k in ("layer_height", "first_layer"))
            else:
                d = self.project.derived_layers()
                lh, fl = ("" if v is None else f"{v:g}" for v in d)
            self.lh.setText(lh)
            self.fl.setText(fl)
            self.lh.setReadOnly(not on)
            self.fl.setReadOnly(not on)
            cur = self.project.layers()
            if cur[0] is None:
                self.layers_note.setText("")
            else:
                src = "set by hand" if on else "from project"
                self.layers_note.setText(f"layers {cur[0]:g} mm, first {cur[1] or cur[0]:g} mm "
                                         f"({src})")
            w = self.project.warning() or ""
            # an unusual flavour/part type/override hides in Advanced; keep it visible
            if (on or (self.project.settings.get("flavor") or "orca") != "orca"
                    or (self.project.settings.get("part_type") or "modifier") != "modifier"):
                self.advanced.setChecked(True)
            self.warn.setText(w)
            self.warn.setVisible(bool(w))
        finally:
            self._loading = False
