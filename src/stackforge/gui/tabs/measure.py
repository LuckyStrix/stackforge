"""Measure tab: measure subcommands as sub-tabs over one terminal.

measure is unverified on hardware: the terminal passes through exactly what the CLI prints and
takes its input() prompts, so there is no GUI logic here to be wrong.
"""
from __future__ import annotations

import re
import shutil

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSplitter, QVBoxLayout, QWidget

from stackforge.gui.terminal import TerminalView
from stackforge.gui.tabs.common import ToolTabs
from stackforge.tools import measure

HEX_LINE = re.compile(r'--measured\s+"([^"]+)"')


class MeasureTab(QWidget):
    title = "Measure"

    def __init__(self, project=None, presets=None, host=None):
        super().__init__()
        self.host = host
        self._hexes = None
        lay = QVBoxLayout(self)
        steps = QLabel(
            "<b>Reading a printed wedge</b> &nbsp; 1. Plug in the ColorMunki. &nbsp; 2. Choose <i>measure-wedge</i> below, set the number "
            "of steps, press <b>Run</b>. &nbsp; 3. Follow the prompts in the box at the bottom "
            "(type in the input line, Enter to confirm). &nbsp; 4. When it finishes, press "
            "<b>Copy readings to Calibrate</b>: they go into the next empty wedge there; choose "
            "the filament and the wedge's base, then Fit.")
        steps.setWordWrap(True)
        steps.setObjectName("hint")
        lay.addWidget(steps)
        banner = self._banner()
        if banner:
            w = QLabel(banner)
            w.setObjectName("warn")
            w.setWordWrap(True)
            lay.addWidget(w)
        split = QSplitter(Qt.Vertical)
        lay.addWidget(split, 1)
        self.term = TerminalView()
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.term, 1)
        row = QHBoxLayout()
        self.copy_btn = QPushButton("Copy readings to Calibrate")
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._to_calibrate)
        row.addWidget(self.copy_btn)
        row.addStretch(1)
        bl.addLayout(row)
        self.tabs = ToolTabs.for_tool("measure", measure.build_parser, project=project, presets=presets,
                                      terminal=self.term, on_done=self._done)
        if shutil.which(measure.NOSPOS_WRAPPER):
            # The patched Argyll is installed because this meter needs it.
            for panel in self.tabs.panels.values():
                if "nospos" in panel.form.entries:
                    panel.form.set_values({"nospos": True})
        split.addWidget(self.tabs)
        split.addWidget(bottom)
        split.setSizes([380, 300])

    @staticmethod
    def _banner():
        if shutil.which("spotread") or shutil.which(measure.NOSPOS_WRAPPER):
            return None
        return ("spotread (ArgyllCMS) is not on PATH, and neither is the "
                f"{measure.NOSPOS_WRAPPER} wrapper: measuring will fail until one is installed.")

    def _done(self, panel, job):
        m = HEX_LINE.search(self.term.screen.text) if job.returncode == 0 else None
        self._hexes = m.group(1) if m and panel.command == ("measure-wedge",) else None
        self.copy_btn.setEnabled(bool(self._hexes))

    def _to_calibrate(self):
        cal = self.host.tabs.get("Calibrate") if self.host else None
        if cal is not None and self._hexes:
            cal.set_measured(self._hexes)
            self.host.show_tab("Calibrate")
            self.copy_btn.setEnabled(False)      # once per reading, so B is not filled twice
