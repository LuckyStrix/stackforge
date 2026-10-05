"""Tools tab: dither_compare (contact sheet in the viewer) and make_samples."""
from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from stackforge.gui.tabs.common import ToolTabs
from stackforge.tools import dither_compare, make_samples


class ToolsTab(QWidget):
    title = "Tools"

    def __init__(self, project=None, presets=None, host=None):
        super().__init__()
        self.tabs = ToolTabs([
            ("halftone compare", "dither_compare", dither_compare.build_parser, ()),
            ("make fixtures", "make_samples", make_samples.build_parser, ()),
        ], project=project, presets=presets)
        QVBoxLayout(self).addWidget(self.tabs)
