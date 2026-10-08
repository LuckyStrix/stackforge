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
BASE_LINE = re.compile(r'--base\s+"(#[0-9A-Fa-f]{6})"')
READINGS_LINE = re.compile(r"^readings file: (.+?)\s*$", re.M)


class MeasureTab(QWidget):
    title = "Measure"

    def __init__(self, project=None, presets=None, host=None):
        super().__init__()
        self.host = host
        self._hexes = self._base = self._readings = None
        lay = QVBoxLayout(self)
        steps = QLabel(
            "<b>Reading printed wedges</b> &nbsp; 1. Plug in the ColorMunki. &nbsp; 2. Choose "
            "<i>measure-wedge</i> below and pick the <b>wedge sheet</b> Calibrate wrote next to "
            "the 3MF, then press <b>Run</b>. &nbsp; 3. Follow the prompts in the box at the bottom "
            "(type in the input line, Enter to confirm): the bare base first, then each step, "
            "wedge by wedge. &nbsp; 4. It writes a <b>readings file</b> next to the sheet. On "
            "this computer press <b>Copy readings to Calibrate</b>; on another, take the file "
            "there and use <i>Load readings…</i> in Calibrate.")
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
        self._hexes = self._base = self._readings = None
        if job.returncode == 0 and panel.command == ("measure-wedge",):
            text = self.term.screen.text
            r = READINGS_LINE.findall(text)
            if r:
                self._readings = r[-1]
            else:
                m, b = HEX_LINE.findall(text), BASE_LINE.findall(text)
                self._hexes = m[-1] if m else None
                self._base = b[-1] if b and self._hexes else None
        self.copy_btn.setEnabled(bool(self._hexes or self._readings))

    def _to_calibrate(self):
        cal = self.host.tabs.get("Calibrate") if self.host else None
        if cal is None:
            return
        if self._readings:
            cal.load_readings(self._readings)
        elif self._hexes:
            cal.set_measured(self._hexes, self._base)
        else:
            return
        self.host.show_tab("Calibrate")
        self.copy_btn.setEnabled(False)          # once per run, so B is not filled twice
