"""stackforge: one window for every tool. Project bar on top, one tab per area."""
from __future__ import annotations

import sys

from PySide6.QtCore import QByteArray, QLoggingCategory
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QMainWindow, QTabWidget, QVBoxLayout, QWidget

from stackforge.gui.project import Project
from stackforge.gui import theme
from stackforge.gui.panel import ToolPanel
from stackforge.gui.projectbar import ProjectBar
from stackforge.gui.settings import PresetStore, Settings

APP = "stackforge"


class HostWindow(QMainWindow):
    """Hosts tabs. A tab is a QWidget with a `title` and, optionally, confirm_close() / on_show()."""

    def __init__(self, settings: Settings | None = None, image_path=None):
        super().__init__()
        self._image = image_path
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
        self._bound: set = set()
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
        from stackforge.gui.tabs.calibrate import CalibrateTab
        from stackforge.gui.tabs.filaments import FilamentsTab
        from stackforge.gui.tabs.plaque import PlaqueArea
        from stackforge.gui.tabs.measure import MeasureTab
        from stackforge.gui.tabs.paint import PaintTab
        from stackforge.gui.tabs.tools import ToolsTab
        self.add_tab(PlaqueArea(self.project, self.presets, host=self, image_path=self._image))
        for cls in (PaintTab, FilamentsTab, CalibrateTab, MeasureTab, ToolsTab):
            self.add_tab(cls(self.project, self.presets, host=self))

    def add_tab(self, tab):
        self.tabs[tab.title] = tab
        self.nb.addTab(tab, tab.title)
        for seq in getattr(tab, "shortcuts", lambda: {})():
            if seq not in self._bound:
                self._bound.add(seq)
                QShortcut(QKeySequence(seq), self, activated=lambda s=seq: self._dispatch(s))

    def _dispatch(self, seq):
        fn = getattr(self.current(), "shortcuts", lambda: {})().get(seq)
        if fn:
            fn()

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
    # Qt's own file dialog asks the system icon theme for oversized SVGs and logs a harmless
    # warning for each; hide just that category
    QLoggingCategory.setFilterRules("qt.svg.draw=false")
    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    theme.apply(app)
    args = sys.argv[1:] if argv is None else argv
    image = next((a for a in args if not a.startswith("-")), None)
    win = HostWindow(image_path=image)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
