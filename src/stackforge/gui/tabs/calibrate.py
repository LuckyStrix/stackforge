"""Calibrate tab: wedge, chips and fit as sub-tabs over the generated forms."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from stackforge.gui.tabs.common import ToolTabs
from stackforge.tools import calibrate


class CalibrateTab(QWidget):
    title = "Calibrate"

    def __init__(self, project=None, presets=None, host=None):
        super().__init__()
        self.host = host
        self.tabs = ToolTabs.for_tool("calibrate", calibrate.build_parser, project=project,
                                      presets=presets, on_done=self._done)
        lay = QVBoxLayout(self)
        steps = QLabel(
            "<b>Measuring a filament's td</b> &nbsp; 1. <i>wedge</i>: choose the filament and an "
            "opaque base, Run, then print the file it writes. &nbsp; 2. Read each step with the "
            "Measure tab (or a photo cropped to the steps). &nbsp; 3. <i>fit</i>: paste the "
            "colours, Run; it saves the result to your filament library. &nbsp; The same steps, "
            "one filament at a time, are on Filaments ▸ Editor ▸ Calibrate.")
        steps.setWordWrap(True)
        steps.setObjectName("hint")
        lay.addWidget(steps)
        lay.addWidget(self.tabs)
        # Saving is the point of a fit; a bad one is still refused by the tool itself.
        self.tabs.panels["fit"].form.set_values({"write": True})

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
