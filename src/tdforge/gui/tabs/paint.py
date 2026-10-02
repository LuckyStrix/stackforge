"""Paint tab: colour an existing 3MF. Two modes over the generated forms.

*Project from above* is topdeco (an image on the top-visible surface); *Pattern or wrapped
image* is surfacecolor. Which parameters a pattern uses is a table in argform.overrides.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from tdforge.gui.argform.spec import introspect
from tdforge.gui.run.panel import ToolPanel
from tdforge.tools import surfacecolor, topdeco

MODES = (("topdeco", "Project from above"), ("surfacecolor", "Pattern or wrapped image"))
BUILDERS = {"topdeco": topdeco.build_parser, "surfacecolor": surfacecolor.build_parser}


class PaintTab(ttk.Frame):
    title = "Paint"

    def __init__(self, master, project=None, presets=None):
        super().__init__(master)
        self.mode = tk.StringVar(value=MODES[0][0])
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(8, 0))
        for key, label in MODES:
            ttk.Radiobutton(bar, text=label, value=key, variable=self.mode,
                            command=self._switch).pack(side="left", padx=(0, 12))
        ttk.Label(bar, text="--expr is evaluated as Python: trusted input only",
                  style="Hint.TLabel").pack(side="right")
        self.panels = {key: ToolPanel(self, introspect(BUILDERS[key](), key), key,
                                      project=project, presets=presets) for key, _ in MODES}
        self._switch()

    def _switch(self):
        for key, panel in self.panels.items():
            if key == self.mode.get():
                panel.pack(fill="both", expand=True, padx=8, pady=8)
            else:
                panel.pack_forget()
