"""Tools tab: halftone_compare (contact sheet in the viewer) and make_fixture."""
from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from tdforge.gui.qt.tabs.common import ToolTabs
from tdforge.tools import halftone_compare, make_fixture


class ToolsTab(QWidget):
    title = "Tools"

    def __init__(self, project=None, presets=None, host=None):
        super().__init__()
        self.tabs = ToolTabs([
            ("halftone compare", "halftone_compare", halftone_compare.build_parser, ()),
            ("make fixtures", "make_fixture", make_fixture.build_parser, ()),
        ], project=project, presets=presets)
        QVBoxLayout(self).addWidget(self.tabs)
