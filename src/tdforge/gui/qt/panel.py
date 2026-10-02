"""ToolPanel: a CommandForm, Run / Cancel, a TerminalView and (optionally) an image preview.

Previews: if the command has --preview, --gamut-preview, --rank-sheet or --sheet and the user
left it empty, a temp path is filled in for the run and the PNG is shown on success. The
user's own output path is never touched.
"""
from __future__ import annotations

import os
import tempfile

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QMessageBox, QPushButton, QSplitter, QVBoxLayout,
                               QWidget)

from tdforge.gui.argform import argv as av
from tdforge.gui.qt.form import CommandForm
from tdforge.gui.qt.imageview import ImageView
from tdforge.gui.qt.presetbar import PresetBar
from tdforge.gui.qt.terminal import TerminalView
from tdforge.gui.run.runner import Job, tool_command

PREVIEW_DESTS = ("preview", "gamut_preview", "rank_sheet", "sheet")


class ToolPanel(QWidget):
    def __init__(self, spec, tool: str, command: tuple = (), project=None, cwd=None,
                 presets=None, terminal=None, on_done=None, parent=None):
        super().__init__(parent)
        self.tool, self.command, self.cwd, self.on_done = tool, tuple(command), cwd, on_done
        self.job: Job | None = None
        self._tmpdir = None
        self._preview_path = None

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        split = QSplitter()
        outer.addWidget(split)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 4, 0)
        self.form = CommandForm(spec, tool, self.command, project, on_change=self._form_changed)
        ll.addWidget(self.form, 1)
        if project is not None:
            project.subscribe(self.form.refresh_project)
        self.presets = PresetBar(self.form, presets) if presets is not None else None
        if self.presets:
            ll.addWidget(self.presets)
        row = QHBoxLayout()
        self.run_btn = QPushButton("Run")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self.run)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        row.addWidget(self.run_btn)
        row.addWidget(self.cancel_btn)
        row.addStretch(1)
        ll.addLayout(row)
        left.setMinimumWidth(480)
        split.addWidget(left)

        self.view = None
        if terminal is None:
            right = QSplitter(Qt.Vertical)
            self.term = TerminalView()
            right.addWidget(self.term)
            if any(d in self.form.entries for d in PREVIEW_DESTS):
                self.view = ImageView("no preview yet")
                right.addWidget(self.view)
                right.setSizes([220, 380])
            split.addWidget(right)
            split.setSizes([560, 600])
        else:
            self.term = terminal
        self._form_changed()

    # ---- state -------------------------------------------------------------------------
    def _form_changed(self, *_):
        if not hasattr(self, "run_btn"):        # fires during form construction
            return
        self.run_btn.setEnabled(not self.form.validate() and not self._running())

    def _running(self) -> bool:
        return self.job is not None and not self.job.done

    def _argv(self) -> list:
        vals = self.form.values()
        self._preview_path = None
        for dest in PREVIEW_DESTS:
            if dest in self.form.entries and not vals.get(dest):
                if self._tmpdir is None:
                    self._tmpdir = tempfile.mkdtemp(prefix="tdforge-")
                vals[dest] = os.path.join(self._tmpdir, dest + ".png")
                self._preview_path = self._preview_path or vals[dest]
        return av.build_argv(self.form.root_spec, vals, self.command)

    def run(self):
        if self._running():
            return
        errs = self.form.validate()
        if errs:
            QMessageBox.warning(self, "Cannot run", "\n".join(errs))
            return
        self.job = Job(tool_command(self.tool, self._argv()), cwd=self.cwd)
        self.term.attach(self.job, on_finish=self._finished)
        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)

    def cancel(self):
        if self._running():
            self.job.cancel()

    def _finished(self, job: Job):
        self.cancel_btn.setEnabled(False)
        self._form_changed()
        if self.on_done:
            self.on_done(self, job)
        if self.view and job.returncode == 0 and self._preview_path and os.path.exists(self._preview_path):
            from PIL import Image
            self.view.set_image(Image.open(self._preview_path).convert("RGB"))
