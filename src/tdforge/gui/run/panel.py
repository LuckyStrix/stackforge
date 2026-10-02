"""ToolPanel: a CommandForm, Run / Cancel, a TerminalView and (optionally) an image preview.

Previews: if the command has --preview, --gamut-preview or --rank-sheet and the user left
it empty, a temp path is filled in for the run and the PNG is shown on success. The user's
own output path is never touched.
"""
from __future__ import annotations

import os
import tempfile
from tkinter import messagebox, ttk

from tdforge.gui import theme
from tdforge.gui.argform import argv as av
from tdforge.gui.argform.form import CommandForm
from tdforge.gui.run.runner import tool_command, Job
from tdforge.gui.presetbar import PresetBar
from tdforge.gui.run.terminal import TerminalView

PREVIEW_DESTS = ("preview", "gamut_preview", "rank_sheet", "sheet")


class ToolPanel(ttk.Frame):
    def __init__(self, master, spec, tool: str, command: tuple = (), project=None,
                 cwd: str | None = None, presets=None,
                 terminal=None, on_done=None):
        super().__init__(master)
        self.tool, self.command, self.cwd = tool, tuple(command), cwd
        self.on_done = on_done
        self.job: Job | None = None
        self._tmpdir: str | None = None
        self._preview_path: str | None = None

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=1)
        if terminal is None:
            paned.add(right, weight=2)

        self.form = CommandForm(left, spec, tool, self.command, project, on_change=self._form_changed)
        self.form.pack(fill="both", expand=True)
        if project is not None:
            project.subscribe(self.form.refresh_project)
        self.presets = PresetBar(left, self.form, presets) if presets is not None else None
        if self.presets:
            self.presets.pack(fill="x", pady=(6, 0))
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(6, 0))
        self.run_btn = ttk.Button(btns, text="Run", command=self.run)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(6, 0))

        self.shared_terminal = terminal is not None
        self.term = terminal or TerminalView(right)
        if terminal is None:
            self.term.pack(fill="both", expand=True)
        self.view: theme.ImageView | None = None
        if terminal is None and any(d in self.form.entries for d in PREVIEW_DESTS):
            self.view = theme.ImageView(right, "no preview yet")
            self.view.pack(fill="both", expand=True, pady=(6, 0))
        self._form_changed()

    def _form_changed(self, *_):
        if not hasattr(self, "run_btn"):      # fires during form construction
            return
        ok = not self.form.validate() and not self._running()
        self.run_btn.config(state="normal" if ok else "disabled")

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
            messagebox.showerror("Cannot run", "\n".join(errs), parent=self)
            return
        argv = self._argv()
        self.job = Job(tool_command(self.tool, argv), cwd=self.cwd)
        self.term.attach(self.job, on_finish=self._finished)
        self.run_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")

    def cancel(self):
        if self._running():
            self.job.cancel()
        return "break"

    def _finished(self, job: Job):
        self.cancel_btn.config(state="disabled")
        self._form_changed()
        if self.on_done:
            self.on_done(self, job)
        if self.view and job.returncode == 0 and self._preview_path and os.path.exists(self._preview_path):
            from PIL import Image
            self.view.set_image(Image.open(self._preview_path).convert("RGB"))
