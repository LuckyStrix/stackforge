"""ToolPanel: a CommandForm, Run / Cancel, a TerminalView and (optionally) an image preview.

Previews: if the command has --preview, --gamut-preview, --rank-sheet or --sheet and the user
left it empty, a temp path is filled in for the run and the PNG is shown on success. The
user's own output path is never touched. --toolchange-curve (paint) is filled the same way,
when the pattern uses it, and its curve is shown under the preview with a slider that
reruns with --max-toolchanges.
"""
from __future__ import annotations

import os
import tempfile

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QMessageBox, QPushButton, QSplitter, QVBoxLayout,
                               QWidget)

from stackforge.gui import theme
from stackforge.gui.argform import argv as av
from stackforge.gui.form import CommandForm
from stackforge.gui.imageview import ImageView
from stackforge.gui.presetbar import PresetBar
from stackforge.gui.terminal import TerminalView
from stackforge.gui.runner import Job, tool_command

PREVIEW_DESTS = ("preview", "gamut_preview", "rank_sheet", "sheet")
CURVE_DEST, LIMIT_DEST = "toolchange_curve", "max_toolchanges"


class ToolPanel(QWidget):
    def __init__(self, spec, tool: str, command: tuple = (), project=None, cwd=None,
                 presets=None, terminal=None, on_done=None, parent=None):
        super().__init__(parent)
        self.tool, self.command, self.cwd, self.on_done = tool, tuple(command), cwd, on_done
        self.job: Job | None = None
        self._tmpdir = None
        self._preview_path = None
        self._curve_path = None
        self.curve = None
        self.pose = None

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
        self.curve_on = None
        if CURVE_DEST in self.form.entries:
            self.curve_on = QCheckBox("Tool-change curve")
            self.curve_on.setChecked(True)
            self.curve_on.setToolTip("Also trade colour error against tool changes (colour "
                                     "patterns): solves every subset of the filaments that "
                                     "holds the base, ~8 s more on a 4-filament head")
            row.addWidget(self.curve_on)
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
            if tool == "paint":           # top_paint's --rotate turns its image, not the model
                from stackforge.gui.pose import PoseBox
                self.pose = PoseBox(self)
                right.addWidget(self.pose)
                right.setSizes([180, 300, 380])
            if CURVE_DEST in self.form.entries:
                from stackforge.gui.toolchange import ToolchangeBox
                self.curve = ToolchangeBox(self.run_with_limit)
                right.addWidget(self.curve)
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
        if getattr(self, "pose", None) is not None:
            self.pose.form_changed()

    def _running(self) -> bool:
        return self.job is not None and not self.job.done

    def _argv(self) -> list:
        vals = self.form.values()
        self._preview_path = None
        for dest in PREVIEW_DESTS:
            if dest in self.form.entries and not vals.get(dest):
                if self._tmpdir is None:
                    self._tmpdir = tempfile.mkdtemp(prefix="stackforge-")
                vals[dest] = os.path.join(self._tmpdir, dest + ".png")
                self._preview_path = self._preview_path or vals[dest]
        self._curve_path = None
        e = self.form.entries.get(CURVE_DEST)
        wanted = self.curve_on is not None and self.curve_on.isChecked()
        if self.curve is not None and wanted and e is not None and e.visible:  # hidden: not this pattern
            if not vals.get(CURVE_DEST):
                if self._tmpdir is None:
                    self._tmpdir = tempfile.mkdtemp(prefix="stackforge-")
                vals[CURVE_DEST] = os.path.join(self._tmpdir, CURVE_DEST + ".json")
            self._curve_path = vals[CURVE_DEST]
        return av.build_argv(self.form.root_spec, vals, self.command)

    def run_with_limit(self, limit):
        """Rerun with --max-toolchanges `limit` (None: no limit), from the curve's slider."""
        self.form.set_values({LIMIT_DEST: "" if limit is None else limit})
        self.run()

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

    def _report_failure(self):
        """A failed run says why, out loud: an "exit 1" in grey is easy to miss."""
        lines = [ln.strip() for ln in self.term.screen.text.splitlines() if ln.strip()]
        why = next((ln for ln in reversed(lines) if not ln.startswith(("Traceback", "File "))),
                   "it stopped with an error")
        for prefix in ("SystemExit: ", "error: "):
            why = why.split(prefix, 1)[-1]
        self.term.status.setStyleSheet(f"color: {theme.ERR}")
        box = QMessageBox(QMessageBox.Warning, "Did not finish",
                          f"{self.title()} failed:\n\n{why}", QMessageBox.Ok, self)
        box.setDetailedText(self.term.screen.text[-6000:])
        box.open()                          # not exec(): never blocks the event loop
        self._failure_box = box

    def title(self) -> str:
        return " ".join((self.tool,) + self.command)

    def cancel(self):
        if self._running():
            self.job.cancel()

    def _finished(self, job: Job):
        self.cancel_btn.setEnabled(False)
        self._form_changed()
        if job.returncode not in (0, None) and not job.cancelled:
            self._report_failure()
        if self.on_done:
            self.on_done(self, job)
        if self.view and job.returncode == 0 and self._preview_path and os.path.exists(self._preview_path):
            from PIL import Image
            self.view.set_image(Image.open(self._preview_path).convert("RGB"))
        if self.curve and job.returncode == 0 and self._curve_path and os.path.exists(self._curve_path):
            from stackforge.gui.toolchange import load_curve
            try:
                self.curve.set_curve(load_curve(self._curve_path))
            except (OSError, ValueError, KeyError) as exc:
                self.curve.show_error(f"could not read the tool-change curve: {exc}")
