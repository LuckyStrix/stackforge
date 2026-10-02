"""Tools tab: halftone_compare (contact sheet in the viewer) and make_fixture."""
from __future__ import annotations

from tkinter import ttk

from tdforge.gui.tabs.common import ToolTabs
from tdforge.tools import halftone_compare, make_fixture


class ToolsTab(ttk.Frame):
    title = "Tools"

    def __init__(self, master, project=None, presets=None):
        super().__init__(master)
        self.tabs = ToolTabs(self, [
            ("halftone compare", "halftone_compare", halftone_compare.build_parser, ()),
            ("make fixtures", "make_fixture", make_fixture.build_parser, ()),
        ], project=project, presets=presets)
        self.tabs.pack(fill="both", expand=True)
