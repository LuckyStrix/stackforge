"""Plaque and Filaments tabs while the hand-built views are being ported.

Both show the generated forms; the original tk windows (designer, filament editor) are one
button away in the classic GUI, which runs as its own process.
"""
from __future__ import annotations

import subprocess
import sys

from PySide6.QtWidgets import QHBoxLayout, QPushButton, QTabWidget, QVBoxLayout, QWidget

from tdforge.core import filamentdb
from tdforge.gui.argform.spec import introspect
from tdforge.gui.qt import theme
from tdforge.gui.qt.panel import ToolPanel
from tdforge.gui.qt.tabs.common import ToolTabs
from tdforge.tools import polymaker, stackforge

WRITES = {("filamentdb", c) for c in ("add", "set", "rm", "seed", "import-sku", "import-hueforge")} | {
    ("polymaker", ("import",))}


def open_classic():
    """The tk GUI (plaque designer + filament editor) as its own process."""
    subprocess.Popen([sys.executable, "-c", "from tdforge.gui.app import main; main()"])


def _classic_row(what: str) -> QWidget:
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(theme.hint(f"The {what} is still the classic window while it is ported."), 1)
    b = QPushButton("Open classic GUI")
    b.clicked.connect(open_classic)
    row.addWidget(b)
    return w


class PlaqueTab(QWidget):
    title = "Plaque"

    def __init__(self, project=None, presets=None, host=None):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.addWidget(_classic_row("plaque designer (live preview, ranking viewer)"))
        self.options = ToolPanel(introspect(stackforge.build_parser(), "stackforge"), "stackforge",
                                 project=project, presets=presets)
        lay.addWidget(self.options, 1)

    def set_db(self, path):
        pass


class FilamentsTab(QWidget):
    title = "Filaments"

    def __init__(self, project=None, presets=None, host=None):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.addWidget(_classic_row("filament editor (details, look, match by eye, calibrate)"))
        self.cli = QTabWidget()
        self.tools = {}
        for tool, builder in (("filamentdb", filamentdb.build_parser), ("polymaker", polymaker.build_parser)):
            tt = ToolTabs.for_tool(tool, builder, project=project, presets=presets)
            self.tools[tool] = tt
            self.cli.addTab(tt, tool)
        lay.addWidget(self.cli, 1)

    def reload_db(self, path=None):
        pass
