"""Calibrate tab: wedge, chips and fit as sub-tabs over the generated forms."""
from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from stackforge.gui.tabs.common import ToolTabs
from stackforge.tools import calibrate


class CalibrateTab(QWidget):
    title = "Calibrate"

    def __init__(self, project=None, presets=None, host=None):
        super().__init__()
        self.host = host
        self.tabs = ToolTabs.for_tool("calibrate", calibrate.build_parser, project=project,
                                      presets=presets, on_done=self._done)
        QVBoxLayout(self).addWidget(self.tabs)

    def _done(self, panel, job):
        """A finished `fit --write` changed the database: tell the Filaments tab."""
        if job.returncode == 0 and panel.command == ("fit",) and panel.form.values().get("write"):
            fil = self.host.tabs.get("Filaments") if self.host else None
            if fil is not None:
                fil.reload_db()

    def set_measured(self, hexes: str):
        """Fill `fit --measured` (from the Measure tab) and show that sub-tab."""
        panel = self.tabs.panels["fit"]
        panel.form.set_values({"measured": hexes})
        self.tabs.select(panel)
