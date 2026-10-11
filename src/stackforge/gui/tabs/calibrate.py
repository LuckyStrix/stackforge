"""Calibrate tab: print the wedges a filament is calibrated from.

"Guided" (gui/filaments/calibrate.CalibratePage) walks one filament through its wedges and hands
the measured readings file to the spectral fit in the Spectra tab. "All options" holds the
wedge/chips command forms for what the guided page does not expose (several filaments on one
plate, chips). The Filaments editor and the Measure tab hand over through start() and
load_readings(). There is no fit on this tab: calibration is spectral only.
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
        self.guided = CalibratePage(project, on_readings=self.fit_readings, presets=presets)
        self.tabs = ToolTabs.for_tool("calibrate", calibrate.build_parser, project=project,
                                      presets=presets)
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

    def start(self, fid, steps=None, base_id=None):
        """Open the guided page on filament `fid` (from the Filaments editor)."""
        self.guided.reload()
        self.guided.select(fid)
        self.guided.prepare(steps=steps, base_id=base_id)
        self.nb.setCurrentWidget(self.guided)

    def fit_readings(self, path, fid) -> bool:
        """Open the Spectra tab's fit on a readings file. False if there is no Spectra tab."""
        spectra = self.host.tabs.get("Spectra") if self.host else None
        if spectra is None:
            return False
        spectra.fit_readings(path, fid)
        self.host.show_tab("Spectra")
        return True

    def load_readings(self, path) -> bool:
        """A readings file from Measure: checked by the guided page, then on to the fit."""
        self.guided.reload()
        return self.guided.load_readings(path)
