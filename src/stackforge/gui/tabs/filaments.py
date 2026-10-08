"""Filaments tab: the library editor, plus the filamentdb / polymaker commands as generated forms."""
from __future__ import annotations

from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from stackforge.core import filamentdb
from PySide6.QtWidgets import QMessageBox

from stackforge.gui import theme
from stackforge.gui.filaments.editor import FilamentEditor
from stackforge.gui.tabs.common import ToolTabs
from stackforge.tools import polymaker

WRITES = {("filamentdb", c) for c in ("add", "set", "rm", "seed", "import-sku", "import-hueforge")} | {
    ("polymaker", ("import",))}

class FilamentsTab(QWidget):
    title = "Filaments"

    def __init__(self, project=None, presets=None, host=None):
        super().__init__()
        self.project, self.host = project, host
        db = project.get("db") if project else filamentdb.DEFAULT_DB
        self.ed = FilamentEditor(db, project.get("catalog") if project else polymaker.CACHE,
                                 project=project)
        self.ed.on_calibrate = self._calibrate if host is not None else None
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

    def _calibrate(self, fid, steps=None, base_id=None):
        cal = self.host.tabs.get("Calibrate")
        if cal is not None:
            cal.start(fid, steps, base_id)
            self.host.show_tab("Calibrate")

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
        """Re-read the database after another tab wrote it.

        Unsaved edits here are merged onto the new file, never saved over it: saving the
        stale copy would throw away what the other tab (a calibration fit) just wrote.
        """
        path = path or self.ed.db_path
        if path != self.ed.db_path:
            if self.ed.dirty and not self.ed.confirm_discard():
                return
            self.ed.load(path)
            return
        if not self.ed.dirty:
            self.ed.load(path)
            return
        lost = self.ed.merge_from_disk()
        msg = "reloaded the database; your unsaved edits are kept"
        if lost:
            msg += f" except to {', '.join(lost)}, which the other tab just changed"
            QMessageBox.information(
                self, "Filaments",
                f"The filament database was just updated (for example by a calibration fit).\n\n"
                f"Your unsaved edits to {', '.join(lost)} were replaced by the new values; "
                f"your other unsaved edits are kept. Press Save when you are done.")
        self.ed.status(msg, theme.WARN)
