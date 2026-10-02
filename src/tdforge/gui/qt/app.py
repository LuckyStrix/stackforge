"""tdforge-gui: one window for every tool. Project bar on top, one tab per area."""
from __future__ import annotations

import sys

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QMainWindow, QTabWidget, QVBoxLayout, QWidget

from tdforge.gui.project import Project
from tdforge.gui.qt import theme
from tdforge.gui.qt.panel import ToolPanel
from tdforge.gui.qt.projectbar import ProjectBar
from tdforge.gui.settings import PresetStore, Settings

APP = "tdforge"


class HostWindow(QMainWindow):
    """Hosts tabs. A tab is a QWidget with a `title` and, optionally, confirm_close() / on_show()."""

    def __init__(self, settings: Settings | None = None):
        super().__init__()
        self.settings = settings or Settings()
        self.project = Project(self.settings)
        self.presets = PresetStore()
        self.setWindowTitle(APP)
        self.resize(1360, 880)
        self.setMinimumSize(900, 600)
        geo = self.settings.get("geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.bar = ProjectBar(self.project)
        lay.addWidget(self.bar)
        self.nb = QTabWidget()
        lay.addWidget(self.nb, 1)
        self.setCentralWidget(central)
        self.tabs: dict[str, QWidget] = {}
        self._add_tabs()

        quit_ = QAction("Quit", self)
        quit_.setShortcut(QKeySequence.Quit)
        quit_.triggered.connect(self.close)
        self.menuBar().addMenu("File").addAction(quit_)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=lambda: self._for_visible_panels(ToolPanel.run))
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=lambda: self._for_visible_panels(ToolPanel.run))
        QShortcut(QKeySequence("Esc"), self, activated=lambda: self._for_visible_panels(ToolPanel.cancel))

        self._db = self.project.get("db")
        self.project.subscribe(self._project_changed)
        self.nb.currentChanged.connect(self._tab_changed)
        last = self.settings.get("last_tab")
        if last in self.tabs:
            self.show_tab(last)

    # ---- tabs --------------------------------------------------------------------------
    def _add_tabs(self):
        from tdforge.gui.qt.tabs.calibrate import CalibrateTab
        from tdforge.gui.qt.tabs.legacy import FilamentsTab, PlaqueTab
        from tdforge.gui.qt.tabs.measure import MeasureTab
        from tdforge.gui.qt.tabs.paint import PaintTab
        from tdforge.gui.qt.tabs.tools import ToolsTab
        for cls in (PlaqueTab, PaintTab, FilamentsTab, CalibrateTab, MeasureTab, ToolsTab):
            self.add_tab(cls(self.project, self.presets, host=self))

    def add_tab(self, tab):
        self.tabs[tab.title] = tab
        self.nb.addTab(tab, tab.title)

    def show_tab(self, title: str):
        self.nb.setCurrentWidget(self.tabs[title])

    def current(self):
        return self.nb.currentWidget()

    def _tab_changed(self, _i):
        on_show = getattr(self.current(), "on_show", None)
        if on_show:
            on_show()

    def _for_visible_panels(self, fn):
        """Ctrl+Enter runs / Esc cancels the form the user is looking at."""
        tab = self.current()
        for panel in (tab.findChildren(ToolPanel) if tab else []):
            if panel.isVisible():
                fn(panel)

    def _project_changed(self):
        db = self.project.get("db")
        if db == self._db:
            return
        self._db = db
        for t in self.tabs.values():
            for hook in ("set_db", "reload_db"):
                if hasattr(t, hook):
                    getattr(t, hook)(db)

    def closeEvent(self, ev):
        for tab in self.tabs.values():
            cc = getattr(tab, "confirm_close", None)
            if cc and not cc():
                ev.ignore()
                return
        self.settings.set("geometry", bytes(self.saveGeometry().toBase64()).decode())
        cur = self.current()
        if cur is not None:
            self.settings.set("last_tab", cur.title)
        ev.accept()


def main(argv=None):
    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    theme.apply(app)
    win = HostWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
