"""Filaments tab: the library editor, plus the filamentdb / polymaker commands as generated forms."""
from __future__ import annotations

from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from stackforge.core import filamentdb
from stackforge.gui.filaments.editor import FilamentEditor
from stackforge.gui.tabs.common import ToolTabs
from stackforge.tools import polymaker

WRITES = {("filamentdb", c) for c in ("add", "set", "rm", "seed", "import-sku", "import-hueforge")} | {
    ("polymaker", ("import",))}

class FilamentsTab(QWidget):
    title = "Filaments"

    def __init__(self, project=None, presets=None, host=None):
        super().__init__()
        self.project = project
        db = project.get("db") if project else filamentdb.DEFAULT_DB
        self.ed = FilamentEditor(db, project.get("catalog") if project else polymaker.CACHE)
        self.nb = QTabWidget()
        self.nb.addTab(self.ed, "Editor")
        self.ed.dirty_changed.connect(lambda on: self.nb.setTabText(0, "Editor •" if on else "Editor"))
        self.cli = {}
        cli = QTabWidget()
        for tool, builder in (("filamentdb", filamentdb.build_parser), ("polymaker", polymaker.build_parser)):
            tt = ToolTabs.for_tool(tool, builder, project=project, presets=presets, on_done=self._cli_done)
            self.cli[tool] = tt
            cli.addTab(tt, tool)
        self.nb.addTab(cli, "Command line")
        QVBoxLayout(self).addWidget(self.nb)

    def _cli_done(self, panel, job):
        """A database-writing command finished: show what it wrote."""
        key = (panel.tool, panel.command[0] if panel.tool == "filamentdb" else panel.command)
        if job.returncode == 0 and key in WRITES:
            self.reload_db()

    def shortcuts(self) -> dict:
        return {"Ctrl+S": self.ed.save, "Ctrl+O": self.ed.open_db, "Ctrl+N": self.ed.new_filament}

    def confirm_close(self) -> bool:
        return self.ed.confirm_discard()

    @property
    def dirty(self) -> bool:
        return self.ed.dirty

    def reload_db(self, path=None):
        """Re-read the database (after another tab changed it); keeps unsaved edits safe."""
        path = path or self.ed.db_path
        if self.ed.dirty and not self.ed.confirm_discard():
            return
        self.ed.load(path)
