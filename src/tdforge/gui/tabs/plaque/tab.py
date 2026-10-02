"""PlaqueTab: the stackforge view as a panel of the host app.

Everything the CLI exposes plus live previews and a ranking viewer. Nothing heavy runs on
the UI thread: solves and rankings go to a worker and report back through a queue, so the
window stays responsive and every long-running job can be cancelled.
"""

from __future__ import annotations

import os
import queue

import numpy as np
import tkinter as tk
from tkinter import messagebox, ttk

from tdforge.core import tdcolor
from tdforge.core.filamentdb import DB
from tdforge.gui.tabs.plaque import APP
from tdforge.gui.tabs.plaque.controls import ControlsMixin
from tdforge.gui.tabs.plaque.job import Worker
from tdforge.gui.tabs.plaque.view import ViewMixin
from tdforge.gui.theme import ERR, FG_DIM
from tdforge.tools import stackforge as sf


class PlaqueTab(ControlsMixin, ViewMixin, ttk.Frame):
    title = "Plaque"

    def __init__(self, master, image_path=None, db_path="filaments.json", host=None):
        super().__init__(master)
        self.host = host            # HostApp: title, quit, tab switching (None when embedded alone)

        self.worker = Worker()
        self.db_path = db_path
        self.db = DB(db_path)
        self.image_path = None
        self.source_img = None      # PIL, full res as loaded
        self.fitted = None          # ndarray at working resolution
        self.result = None          # dict from the last generate; None once stale
        self.rank_results = None

        self.m_presets = None
        self._build_ui()
        self._poll()

        self._reload_filaments()
        if image_path:
            self._load_image(image_path)
        self._refresh_estimate()

    def _build_ui(self):
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill="both", expand=True)

        panes = ttk.PanedWindow(outer, orient="horizontal")
        panes.pack(fill="both", expand=True)

        panes.add(self._build_left(panes), weight=0)
        panes.add(self._build_center(panes), weight=1)
        panes.add(self._build_right(panes), weight=0)

        self._build_status(outer)

    def _generate(self):
        v = self._validate()
        if not v:
            return
        sel, base = v
        try:
            a = self.config_ns()
        except (tk.TclError, ValueError):
            messagebox.showwarning(APP, "A setting is empty or not a number.")
            return
        problems = sf.check_args(a)
        if problems:
            messagebox.showwarning(APP, "\n".join(problems))
            return
        self._refresh_fit()
        img = self.fitted

        sig = self._signature()
        self._start("generate",
                    lambda: dict(self._job_auto(sel, base, a, img), sig=sig))

    def _job_auto(self, sel, base, a, img):
        """The whole pipeline: rank the pool if it is bigger than the
        toolheads, then solve with the winner."""
        ranked = None
        lo = 0.0
        if len(sel) > a.slots:
            res = sf.rank_subsets(sel, base, a, img,
                                  progress=lambda f, m: self.worker.progress(f * 0.5, m),
                                  verbose=False)
            top = min(a.top, len(res))
            entries = sf.render_candidates(
                res, base, a, img, top,
                progress=lambda f, m: self.worker.progress(0.5 + f * 0.2, m))
            import tempfile
            fd, path = tempfile.mkstemp(prefix="stackforge_combos_", suffix=".png")
            os.close(fd)
            sf.contact_sheet(path, entries, img)
            ranked = {"results": res, "sheet": path}
            sel = res[0]["fils"]
            lo = 0.7
        r = self._job_solve(sel, base, a, img, lo)
        r["ranked"] = ranked
        return r

    def _job_solve(self, sel, base, a, img, lo=0.0):
        def prog(f, m):
            self.worker.progress(lo + (1 - lo) * f, m)
        g = sf.Gamut(sel, base, a.layer_height, a.max_layers, a.grid, a.cap,
                     verbose=False, progress=prog)
        prog(0.7, "matching pixels to reachable colours")
        state = sf.solve_image(g, img, a.dither)
        achieved = np.round(g.srgb()[state])
        err = np.linalg.norm(
            tdcolor.srgb_to_lab(achieved.astype(np.float64))
            - tdcolor.srgb_to_lab(img.astype(np.float64)), axis=-1)
        prog(0.95, "building layer labels")
        blurred = tdcolor.blurred_de(achieved, img, sf.BLUR_MM / a.resolution)[0]
        labels, _ = sf.trim_base_layers(sf.layer_labels(g, state), g.base_index)
        return {"gamut": g, "labels": labels, "achieved": achieved, "err": err,
                "blurred": blurred, "fils": sel, "base": base, "args": a,
                "image_path": self.image_path}

    def _start(self, kind, fn):
        if self.worker.busy:
            return
        if not self.worker.submit(kind, fn):
            return
        self.btn_go.config(state="disabled")
        self.btn_export.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.prog["value"] = 0
        self._status("working…")

    def _finish(self):
        self.btn_go.config(state="normal")
        self.btn_cancel.config(state="disabled")
        self.prog["value"] = 0

    def _poll(self):
        try:
            while True:
                job = self.q_get()
                if job is None:
                    break
                if job.kind == "progress":
                    frac, msg = job.result
                    self.prog["value"] = max(0, min(100, frac * 100))
                    self._status(msg)
                elif job.kind == "cancelled":
                    self._finish()
                    self._status("cancelled")
                elif job.error:
                    self._finish()
                    self._status("failed", ERR)
                    messagebox.showerror(APP, job.error)
                elif job.kind == "generate":
                    self._on_solved(job.result)
        finally:
            self.after(60, self._poll)

    def q_get(self):
        try:
            return self.worker.q.get_nowait()
        except queue.Empty:
            return None

    def _about(self):
        messagebox.showinfo(
            APP,
            "stackforge\n\n"
            "Flat full-colour plaques built from per-pixel filament stacks.\n\n"
            "Unlike a height-map approach, every pixel gets its own stack and the "
            "plaque comes out flat — colour lives in the vertical composition, so "
            "there is no surface topography to catch raking light.\n\n"
            "Colours are only as good as the filament database. Entries marked "
            "'est' have not been measured; run calibrate.py.")

    def _status(self, msg, colour=FG_DIM):
        self.lbl_status.config(text=msg, foreground=colour)


    # -- host integration --------------------------------------------------

    def _set_title(self, text):
        if self.host is not None:
            self.host.set_title(text)

    def build_menu(self, root):
        m = tk.Menu(root)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="Open image…", accelerator="Ctrl+O", command=self._open_image)
        f.add_command(label="Export 3MF…", accelerator="Ctrl+E", command=self._export)
        f.add_separator()
        f.add_command(label="Save preview PNG…", command=self._save_preview)
        f.add_separator()
        f.add_command(label="Quit", accelerator="Ctrl+Q",
                      command=lambda: root.event_generate("<<CloseRequest>>"))
        m.add_cascade(label="File", menu=f)

        d = tk.Menu(m, tearoff=0)
        d.add_command(label="Edit filaments…", command=self._edit_filaments)
        d.add_command(label="Reload database", command=self._reload_filaments)
        d.add_command(label="Open database file…", command=self._pick_db)
        m.add_cascade(label="Database", menu=d)

        self.m_presets = tk.Menu(m, tearoff=0)
        m.add_cascade(label="Presets", menu=self.m_presets)
        self._rebuild_presets_menu()

        h = tk.Menu(m, tearoff=0)
        h.add_command(label="About", command=self._about)
        m.add_cascade(label="Help", menu=h)
        return m

    def shortcuts(self) -> dict:
        return {"<Control-o>": self._open_image, "<Control-e>": self._export}

    def confirm_close(self) -> bool:
        return True

    def on_show(self):
        """Back on this tab: pick up edits made on the Filaments tab."""
        try:
            mtime = os.path.getmtime(self.db_path)
        except OSError:
            return
        if mtime != getattr(self, "_db_mtime", mtime):
            self._reload_filaments()
        self._db_mtime = mtime

    def _edit_filaments(self):
        """The editor is the Filaments tab now; edits show up when we come back."""
        if self.host is not None:
            self.host.show_tab("Filaments")
