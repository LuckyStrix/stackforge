"""PresetBar: Save / Load / Delete for one form, over a PresetStore."""
from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QInputDialog, QLabel, QMessageBox,
                               QPushButton, QWidget)

from stackforge.gui.settings import PresetStore


class PresetBar(QWidget):
    def __init__(self, form, store: PresetStore, parent=None):
        super().__init__(parent)
        self.form, self.store = form, store
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("Preset"))
        self.combo = QComboBox()
        self.combo.setMinimumWidth(140)
        lay.addWidget(self.combo, 1)
        for text, fn in (("Load", self.load), ("Save…", self.save), ("Delete", self.delete)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            lay.addWidget(b)
        self.refresh()

    def refresh(self, select=""):
        self.combo.clear()
        self.combo.addItems(self.store.names(self.form.tool, self.form.command))
        if select:
            self.combo.setCurrentText(select)

    def save(self):
        name, ok = QInputDialog.getText(self, "Save preset", "Preset name:", text=self.combo.currentText())
        if not ok or not name.strip():
            return
        try:
            self.store.save(self.form.tool, self.form.command, name, self.form.preset_values())
        except ValueError as e:
            QMessageBox.warning(self, "Preset", str(e))
            return
        self.refresh(self.store.clean(name))

    def load(self):
        name = self.combo.currentText()
        if not name:
            return
        skipped = self.form.set_values(self.store.load(self.form.tool, self.form.command, name))
        if skipped:
            QMessageBox.information(self, "Preset", "Skipped keys that no longer exist: " + ", ".join(skipped))

    def delete(self):
        name = self.combo.currentText()
        if name:
            self.store.delete(self.form.tool, self.form.command, name)
            self.refresh()
