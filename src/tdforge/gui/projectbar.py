"""ProjectBar: the strip across the top, bound to Project/Settings and shared by every tab."""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, ttk

from tdforge.gui import theme
from tdforge.gui.project import Project


class ProjectBar(ttk.Frame):
    def __init__(self, master, project: Project):
        super().__init__(master, padding=(8, 6))
        self.project = project
        s = project.settings
        self.vars = {k: tk.StringVar(value=s.get(k)) for k in ("db", "template", "flavor", "part_type",
                                                                "layer_height", "first_layer")}
        self.vars["db"].set(project.get("db"))      # the path in effect, not a blank
        self.override = tk.BooleanVar(value=project.override)
        self._warn = tk.StringVar()
        self._loading = False

        r1 = ttk.Frame(self)
        r1.pack(fill="x")
        self._path_row(r1, "Database", "db", [("JSON", "*.json")])
        self._path_row(r1, "Template", "template", [("3MF", "*.3mf")])
        r2 = ttk.Frame(self)
        r2.pack(fill="x", pady=(4, 0))
        self._combo(r2, "Flavor", "flavor", ["orca", "prusa"])
        self._combo(r2, "Part type", "part_type", ["modifier", "part"])
        ttk.Label(r2, text="Layer / first").pack(side="left", padx=(12, 4))
        self.lh = ttk.Entry(r2, textvariable=self.vars["layer_height"], width=7)
        self.fl = ttk.Entry(r2, textvariable=self.vars["first_layer"], width=7)
        self.lh.pack(side="left")
        self.fl.pack(side="left", padx=(4, 0))
        ttk.Checkbutton(r2, text="override", variable=self.override,
                        command=self._override_changed).pack(side="left", padx=(6, 0))
        ttk.Label(r2, textvariable=self._warn, foreground=theme.WARN).pack(side="left", padx=(12, 0))
        for k in ("db", "template", "flavor", "part_type"):
            self.vars[k].trace_add("write", lambda *_, k=k: self._edited(k))
        for k in ("layer_height", "first_layer"):
            self.vars[k].trace_add("write", lambda *_, k=k: self._edited(k))
        self.sync()

    def _path_row(self, parent, label, key, types):
        ttk.Label(parent, text=label).pack(side="left", padx=(0, 4))
        ttk.Entry(parent, textvariable=self.vars[key], width=34).pack(side="left", padx=(0, 2))
        ttk.Button(parent, text="…", width=3,
                   command=lambda: self._browse(key, types)).pack(side="left", padx=(0, 12))

    def _combo(self, parent, label, key, values):
        ttk.Label(parent, text=label).pack(side="left", padx=(0, 4))
        ttk.Combobox(parent, textvariable=self.vars[key], values=values, state="readonly",
                     width=9).pack(side="left", padx=(0, 8))

    def _browse(self, key, types):
        p = filedialog.askopenfilename(filetypes=types + [("All files", "*.*")])
        if p:
            self.vars[key].set(p)
            if key == "template":
                self.project.settings.add_recent(p)

    # ---- editing -----------------------------------------------------------------------
    def _edited(self, key):
        if self._loading:
            return
        if key in ("layer_height", "first_layer") and not self.override.get():
            return
        self.project.set(key, self.vars[key].get().strip())
        self.sync()

    def _override_changed(self):
        if self.override.get():
            # start the override from what the template says, so it is an edit, not a blank
            lh, fl = self.project.derived_layers()
            for k, v in (("layer_height", lh), ("first_layer", fl)):
                if v is not None and not self.project.settings.get(k):
                    self.project.settings.set(k, f"{v:g}")
        self.project.set("layer_override", bool(self.override.get()))
        self.sync()

    def sync(self):
        """Refresh the layer boxes (derived unless overridden) and the warning chip."""
        self._loading = True
        try:
            on = self.override.get()
            if on:
                for k in ("layer_height", "first_layer"):
                    self.vars[k].set(self.project.settings.get(k))
            else:
                lh, fl = self.project.derived_layers()
                self.vars["layer_height"].set("" if lh is None else f"{lh:g}")
                self.vars["first_layer"].set("" if fl is None else f"{fl:g}")
            for e in (self.lh, self.fl):
                e.config(state="normal" if on else "readonly")
            self._warn.set(self.project.warning() or "")
        finally:
            self._loading = False
