"""ProjectBar: the strip across the top, bound to Project/Settings and shared by every tab."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from tdforge.gui.project import Project


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

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("Database"))
        r1.addWidget(self.db, 1)
        r1.addWidget(self._browse(self.db, "JSON (*.json);;All files (*)"))
        r1.addSpacing(12)
        r1.addWidget(QLabel("Template"))
        r1.addWidget(self.template, 1)
        r1.addWidget(self._browse(self.template, "3MF (*.3mf);;All files (*)"))
        r2 = QHBoxLayout()
        for text, w in (("Flavor", self.flavor), ("Part type", self.part),
                        ("Layer height", self.lh), ("First layer", self.fl)):
            r2.addWidget(QLabel(text))
            r2.addWidget(w)
            r2.addSpacing(12)
        r2.addWidget(self.override)
        r2.addStretch(1)
        outer.addLayout(r1)
        outer.addLayout(r2)
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

    def _browse(self, edit: QLineEdit, filt: str) -> QPushButton:
        b = QPushButton("…")
        b.setMaximumWidth(32)

        def go():
            p, _ = QFileDialog.getOpenFileName(self, "Choose file", edit.text(), filt)
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
            w = self.project.warning() or ""
            self.warn.setText(w)
            self.warn.setVisible(bool(w))
        finally:
            self._loading = False
