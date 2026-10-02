"""PlaqueArea: the hand-built designer plus an "All options" sub-tab (the generated form).

Where the two overlap, the designer is the default; the generated form reaches every flag
the designer does not expose.
"""
from __future__ import annotations

from tkinter import ttk

from tdforge.gui.argform.spec import introspect
from tdforge.gui.run.panel import ToolPanel
from tdforge.gui.tabs.plaque.tab import PlaqueTab
from tdforge.tools import stackforge


class PlaqueArea(ttk.Frame):
    title = "Plaque"

    def __init__(self, master, image_path=None, project=None, presets=None, host=None):
        super().__init__(master)
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        db = project.get("db") if project else "filaments.json"
        self.designer = PlaqueTab(self.nb, image_path, db, host=host)
        self.nb.add(self.designer, text="Designer")
        self.options = ToolPanel(self.nb, introspect(stackforge.build_parser(), "stackforge"),
                                 "stackforge", project=project, presets=presets)
        self.nb.add(self.options, text="All options")

    # delegate the host protocol to the designer
    def shortcuts(self):
        return self.designer.shortcuts()

    def build_menu(self, root):
        return self.designer.build_menu(root)

    def confirm_close(self):
        return self.designer.confirm_close()

    def on_show(self):
        self.designer.on_show()

    def set_db(self, path):
        self.designer.db_path = path
        self.designer._reload_filaments()
