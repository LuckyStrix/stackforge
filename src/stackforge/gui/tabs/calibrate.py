"""Calibrate tab: the one place to calibrate a filament.

"Guided" (gui/filaments/calibrate.CalibratePage) walks one filament through wedge, readings and
fit, and saves the result to the library. "All options" holds the wedge/chips/fit command forms
for what the guided page does not expose (several filaments on one plate, chips, photos). The
Filaments editor and the Measure tab hand over to the guided page through start() and
set_measured().
"""
from __future__ import annotations

from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from stackforge.gui.filaments.calibrate import CalibratePage
from stackforge.gui.tabs.common import ToolTabs
from stackforge.tools import calibrate


class CalibrateTab(QWidget):
    title = "Calibrate"

    def __init__(self, project=None, presets=None, host=None):
        super().__init__()
        self.host = host
        self.guided = CalibratePage(project, on_saved=lambda _fid: self._db_written())
        self.tabs = ToolTabs.for_tool("calibrate", calibrate.build_parser, project=project,
                                      presets=presets, on_done=self._done)
        # Saving is the point of a fit; a bad one is still refused by the tool itself.
        self.tabs.panels["fit"].form.set_values({"write": True})
        self.nb = QTabWidget()
        self.nb.addTab(self.guided, "Guided")
        self.nb.addTab(self.tabs, "All options")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.nb)

    def on_show(self):
        self.guided.reload()            # the library may have been edited in another tab

    def reload_db(self, path=None):
        self.guided.reload(path)

    def _db_written(self):
        fil = self.host.tabs.get("Filaments") if self.host else None
        if fil is not None:
            fil.reload_db()

    def _done(self, panel, job):
        """A finished `fit --write` changed the database: tell the other tabs."""
        if job.returncode == 0 and panel.command == ("fit",) and panel.form.values().get("write"):
            self._db_written()
            self.guided.reload()

    def start(self, fid, steps=None, base_id=None):
        """Open the guided page on filament `fid` (from the Filaments editor)."""
        self.guided.reload()
        self.guided.select(fid)
        self.guided.prepare(steps=steps, base_id=base_id)
        self.nb.setCurrentWidget(self.guided)

    def set_measured(self, hexes: str):
        """Readings from the Measure tab: into the guided page's next empty wedge (and the
        fit form, for anyone using All options). Returns "A" or "B"."""
        self.tabs.panels["fit"].form.set_values({"measured": hexes})
        self.nb.setCurrentWidget(self.guided)
        return self.guided.add_measured(hexes)
