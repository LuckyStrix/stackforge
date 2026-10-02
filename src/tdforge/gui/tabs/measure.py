"""Measure tab: munki subcommands as sub-tabs over one terminal.

munki is unverified on hardware: the terminal passes through exactly what the CLI prints and
takes its input() prompts, so there is no GUI logic here to be wrong.
"""
from __future__ import annotations

import re
import shutil
from tkinter import ttk

from tdforge.gui import theme
from tdforge.gui.run.terminal import TerminalView
from tdforge.gui.tabs.common import ToolTabs
from tdforge.tools import munki

HEX_LINE = re.compile(r'--measured\s+"([^"]+)"')


class MeasureTab(ttk.Frame):
    title = "Measure"

    def __init__(self, master, project=None, presets=None, host=None):
        super().__init__(master)
        self.host = host
        self._hexes: str | None = None
        banner = self._banner()
        if banner:
            ttk.Label(self, text=banner, foreground=theme.WARN, wraplength=900,
                      justify="left").pack(fill="x", padx=8, pady=(8, 0))
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True)
        top = ttk.Frame(paned)
        bottom = ttk.Frame(paned)
        paned.add(top, weight=3)
        paned.add(bottom, weight=2)
        self.term = TerminalView(bottom)
        self.term.pack(fill="both", expand=True)
        row = ttk.Frame(bottom)
        row.pack(fill="x", pady=(4, 0))
        self.copy_btn = ttk.Button(row, text="Copy hex list to Calibrate ▸ fit", state="disabled",
                                   command=self._to_calibrate)
        self.copy_btn.pack(side="left")
        self.tabs = ToolTabs.for_tool(top, "munki", munki.build_parser, project=project,
                                      presets=presets, terminal=self.term, on_done=self._done)
        self.tabs.pack(fill="both", expand=True)

    @staticmethod
    def _banner():
        if shutil.which("spotread") or shutil.which(munki.NOSPOS_WRAPPER):
            return None
        return ("spotread (ArgyllCMS) is not on PATH, and neither is the "
                f"{munki.NOSPOS_WRAPPER} wrapper: measuring will fail until one is installed.")

    def _done(self, panel, job):
        m = HEX_LINE.search(self.term.screen.text) if job.returncode == 0 else None
        self._hexes = m.group(1) if m and panel.command == ("measure-wedge",) else None
        self.copy_btn.config(state="normal" if self._hexes else "disabled")

    def _to_calibrate(self):
        cal = self.host.tabs.get("Calibrate") if self.host else None
        if cal is not None and self._hexes:
            cal.set_measured(self._hexes)
            self.host.show_tab("Calibrate")
