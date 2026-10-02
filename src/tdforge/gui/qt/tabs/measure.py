"""Measure tab: munki subcommands as sub-tabs over one terminal.

munki is unverified on hardware: the terminal passes through exactly what the CLI prints and
takes its input() prompts, so there is no GUI logic here to be wrong.
"""
from __future__ import annotations

import re
import shutil

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSplitter, QVBoxLayout, QWidget

from tdforge.gui.qt.terminal import TerminalView
from tdforge.gui.qt.tabs.common import ToolTabs
from tdforge.tools import munki

HEX_LINE = re.compile(r'--measured\s+"([^"]+)"')


class MeasureTab(QWidget):
    title = "Measure"

    def __init__(self, project=None, presets=None, host=None):
        super().__init__()
        self.host = host
        self._hexes = None
        lay = QVBoxLayout(self)
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
        self.copy_btn = QPushButton("Copy hex list to Calibrate ▸ fit")
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._to_calibrate)
        row.addWidget(self.copy_btn)
        row.addStretch(1)
        bl.addLayout(row)
        self.tabs = ToolTabs.for_tool("munki", munki.build_parser, project=project, presets=presets,
                                      terminal=self.term, on_done=self._done)
        split.addWidget(self.tabs)
        split.addWidget(bottom)
        split.setSizes([380, 300])

    @staticmethod
    def _banner():
        if shutil.which("spotread") or shutil.which(munki.NOSPOS_WRAPPER):
            return None
        return ("spotread (ArgyllCMS) is not on PATH, and neither is the "
                f"{munki.NOSPOS_WRAPPER} wrapper: measuring will fail until one is installed.")

    def _done(self, panel, job):
        m = HEX_LINE.search(self.term.screen.text) if job.returncode == 0 else None
        self._hexes = m.group(1) if m and panel.command == ("measure-wedge",) else None
        self.copy_btn.setEnabled(bool(self._hexes))

    def _to_calibrate(self):
        cal = self.host.tabs.get("Calibrate") if self.host else None
        if cal is not None and self._hexes:
            cal.set_measured(self._hexes)
            self.host.show_tab("Calibrate")
