"""Plaque tab, controls: filament list, option panel, presets, template and database pickers."""

from __future__ import annotations

import json
import os
import zipfile
from types import SimpleNamespace

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from tdforge.tools import stackforge as sf
from tdforge.core import td3mf
from tdforge.core.filamentdb import DB
from tdforge.gui.tabs.plaque import APP
from tdforge.gui.tabs.plaque import PRESET_FILE
from tdforge.gui.theme import ACCENT, BG, BG3, FG, FG_DIM, OK, WARN, Section, swatch_image


class ControlsMixin:
    """Left and right panels of the plaque tab (state lives on PlaqueTab)."""

    def _build_left(self, master):
        f = ttk.Frame(master, width=340)
        f.pack_propagate(False)

        head = ttk.Frame(f)
        head.pack(fill="x", pady=(0, 6))
        ttk.Label(head, text="Filament library", style="Head.TLabel").pack(side="left")
        ttk.Button(head, text="⟳", width=3, command=self._reload_filaments).pack(side="right")

        ttk.Label(
            f,
            text="Tick every filament you own or might load. Ticking more than "
                 "the toolhead count switches the Generate button to ranking.",
            style="Hint.TLabel", wraplength=320,
        ).pack(fill="x", pady=(0, 6))

        search = ttk.Frame(f)
        search.pack(fill="x", pady=(0, 4))
        ttk.Label(search, text="Filter").pack(side="left")
        self.v_filter = tk.StringVar()
        self.v_filter.trace_add("write", lambda *_: self._render_filaments())
        ttk.Entry(search, textvariable=self.v_filter).pack(
            side="left", fill="x", expand=True, padx=(6, 0))

        wrap = ttk.Frame(f)
        wrap.pack(fill="both", expand=True)
        self.fil_canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, width=320)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.fil_canvas.yview)
        self.fil_canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.fil_canvas.pack(side="left", fill="both", expand=True)
        self.fil_inner = ttk.Frame(self.fil_canvas)
        self.fil_window = self.fil_canvas.create_window(
            (0, 0), window=self.fil_inner, anchor="nw")
        self.fil_inner.bind(
            "<Configure>",
            lambda e: self.fil_canvas.configure(scrollregion=self.fil_canvas.bbox("all")),
        )
        self.fil_canvas.bind(
            "<Configure>",
            lambda e: self.fil_canvas.itemconfig(self.fil_window, width=e.width),
        )
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.fil_canvas.bind_all(seq, self._on_wheel)

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="All", command=lambda: self._set_all(True)).pack(
            side="left", expand=True, fill="x", padx=(0, 3))
        ttk.Button(btns, text="None", command=lambda: self._set_all(False)).pack(
            side="left", expand=True, fill="x", padx=3)
        ttk.Button(btns, text="Measured only", command=self._select_measured).pack(
            side="left", expand=True, fill="x", padx=(3, 0))

        base = ttk.Frame(f)
        base.pack(fill="x", pady=(10, 0))
        ttk.Label(base, text="Base (opaque backing)").pack(anchor="w")
        self.v_base = tk.StringVar()
        self.cb_base = ttk.Combobox(base, textvariable=self.v_base, state="readonly")
        self.cb_base.pack(fill="x", pady=(3, 0))
        self.cb_base.bind("<<ComboboxSelected>>", lambda e: self._refresh_estimate())
        ttk.Label(
            base,
            text="Occupies one toolhead — Toolheads 4 means the base plus three "
                 "others. It is a full colour too, not just backing: short stacks "
                 "pad against it. Usually white or black.",
            style="Hint.TLabel", wraplength=320,
        ).pack(fill="x", pady=(2, 0))
        return f

    def _on_wheel(self, e):
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            if w is self.fil_canvas:
                delta = 1 if getattr(e, "num", 0) == 5 else -1 if getattr(e, "num", 0) == 4 else (
                    -1 if e.delta > 0 else 1)
                self.fil_canvas.yview_scroll(delta, "units")
                return
            w = getattr(w, "master", None)

    def _build_right(self, master):
        outer = ttk.Frame(master, width=340)
        outer.pack_propagate(False)

        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, width=324)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        f = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=f, anchor="nw")
        f.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))

        def num(parent, var, frm, to, inc):
            # Classic tk.Spinbox rather than ttk: clam's TSpinbox ignores
            # fieldbackground, so a themed one renders light-on-light here.
            return tk.Spinbox(
                parent, textvariable=var, from_=frm, to=to, increment=inc,
                width=10, command=self._refresh_estimate,
                bg=BG3, fg=FG, insertbackground=FG, buttonbackground=BG3,
                readonlybackground=BG3, highlightthickness=0, relief="flat",
                borderwidth=4, font=("TkDefaultFont", 9),
                selectbackground=ACCENT, selectforeground="#0d1420",
            )

        # geometry
        g = Section(f, "Plaque geometry")
        g.pack(fill="x", pady=(0, 8))
        self.v_width = tk.DoubleVar(value=150.0)
        self.v_height = tk.DoubleVar(value=0.0)
        self.v_res = tk.DoubleVar(value=sf.MIN_FEATURE_MM)
        self.v_layer = tk.DoubleVar(value=0.08)
        self.v_first = tk.DoubleVar(value=0.08)
        self.v_maxl = tk.IntVar(value=16)
        self.v_basel = tk.IntVar(value=5)
        g.field("Width (mm)", num(g, self.v_width, 10, 500, 5))
        g.field("Height (mm)", num(g, self.v_height, 0, 500, 5),
                "0 keeps the image's aspect ratio.")
        g.field("Resolution (mm)", num(g, self.v_res, 0.1, 2.0, 0.05),
                "Match your nozzle width. Finer means far more geometry.")
        g.field("Layer height (mm)", num(g, self.v_layer, 0.04, 0.3, 0.01),
                "Must match what the slicer will use, or the modifiers land "
                "between layers and colours drop out.")
        g.field("First layer (mm)", num(g, self.v_first, 0.04, 0.5, 0.01),
                "Usually thicker than the rest; it offsets every colour layer "
                "above it.")
        g.field("Colour layers", num(g, self.v_maxl, 2, 40, 1),
                "The gamut stops growing once the deepest stack goes opaque, "
                "so past ~16 this is just print time.")
        g.field("Base layers", num(g, self.v_basel, 1, 20, 1))
        for v in (self.v_width, self.v_height, self.v_res, self.v_layer,
                  self.v_first, self.v_maxl, self.v_basel):
            v.trace_add("write", lambda *_: self._refresh_estimate())

        # colour
        c = Section(f, "Colour")
        c.pack(fill="x", pady=(0, 8))
        self.v_fit = tk.StringVar(value="cover")
        self.v_dither = tk.StringVar(value="none")
        self.v_grid = tk.IntVar(value=192)
        c.field("Image fit", ttk.Combobox(c, textvariable=self.v_fit, state="readonly",
                                          values=["cover", "contain", "stretch"], width=10))
        c.field("Dither", ttk.Combobox(c, textvariable=self.v_dither, state="readonly",
                                       values=["none", "ordered", "blue", "floyd"], width=10),
                "Rarely helps here: stacking already fills the gamut densely, so "
                "dithering adds geometry without reducing error. Try it with 2–3 "
                "filaments.")
        c.field("Gamut grid", num(c, self.v_grid, 48, 384, 16),
                "Dedup resolution of the colour search. Higher is finer and slower.")
        self.v_fit.trace_add("write", lambda *_: (self._refresh_fit(), self._refresh_estimate()))
        self.v_dither.trace_add("write", lambda *_: self._refresh_estimate())

        # ranking
        r = Section(f, "Combination ranking")
        r.pack(fill="x", pady=(0, 8))
        self.v_slots = tk.IntVar(value=4)
        self.v_top = tk.IntVar(value=5)
        self.v_rankby = tk.StringVar(value="mean")
        self.v_samples = tk.IntVar(value=20000)
        r.field("Toolheads", num(r, self.v_slots, 2, 8, 1),
                "Total spools loaded, including the base. Set this to 4 for a "
                "four-toolhead machine.")
        r.field("Show best", num(r, self.v_top, 1, 12, 1))
        r.field("Rank by", ttk.Combobox(r, textvariable=self.v_rankby, state="readonly",
                                        values=["mean", "p95"], width=10),
                "p95 targets worst-case error instead of average — better when a "
                "few badly-wrong regions bother you more than a slight overall shift.")
        r.field("Colours scored", num(r, self.v_samples, 500, 100000, 500))
        for v in (self.v_slots, self.v_top, self.v_rankby, self.v_samples):
            v.trace_add("write", lambda *_: self._refresh_estimate())

        # output
        o = Section(f, "Output")
        o.pack(fill="x", pady=(0, 8))
        self.v_flavor = tk.StringVar(value="orca")
        self.v_part = tk.StringVar(value="modifier")
        o.field("Slicer flavour", ttk.Combobox(o, textvariable=self.v_flavor,
                                               state="readonly",
                                               values=["orca", "prusa"], width=10))
        o.field("Decal type", ttk.Combobox(o, textvariable=self.v_part, state="readonly",
                                           values=["modifier", "part"], width=10),
                "Switch to 'part' if your slicer ignores extruder overrides on "
                "modifier volumes.")
        self.v_template = tk.StringVar(value="")
        tf = ttk.Frame(o)
        self.lbl_template = ttk.Label(tf, text="(none)", style="Stat.TLabel")
        self.lbl_template.pack(side="left", fill="x", expand=True)
        ttk.Button(tf, text="…", width=3, command=self._pick_template).pack(side="right")
        ttk.Button(tf, text="✕", width=3, command=self._clear_template).pack(side="right")
        o.field("Template project", tf,
                "A project .3mf exported from your slicer. Its printer, filament "
                "and print settings are carried over verbatim, so the output "
                "opens as a project instead of bare geometry.")

        # estimate
        e = Section(f, "Estimate")
        e.pack(fill="x", pady=(0, 8))
        self.lbl_est = ttk.Label(e, text="", style="Stat.TLabel", justify="left")
        e.row(self.lbl_est)
        return outer

    def _build_status(self, master):
        bar = ttk.Frame(master)
        bar.pack(fill="x", pady=(8, 0))

        self.btn_go = ttk.Button(bar, text="Generate", style="Go.TButton",
                                 command=self._generate)
        self.btn_go.pack(side="left")
        self.btn_export = ttk.Button(bar, text="Export 3MF…", command=self._export,
                                     state="disabled")
        self.btn_export.pack(side="left", padx=6)
        self.btn_cancel = ttk.Button(bar, text="Cancel", command=self.worker.cancel,
                                     state="disabled")
        self.btn_cancel.pack(side="left")

        self.prog = ttk.Progressbar(bar, mode="determinate", length=220)
        self.prog.pack(side="right")
        self.lbl_status = ttk.Label(bar, text="Ready", style="Stat.TLabel")
        self.lbl_status.pack(side="right", padx=12)

    def _reload_filaments(self):
        try:
            self.db = DB(self.db_path)
        except SystemExit as exc:
            messagebox.showerror(APP, str(exc))
            return
        # Keep whatever was ticked across a reload -- editing the database
        # should not silently throw away the loadout being worked on.
        prev = {fid for fid, v in getattr(self, "checks", {}).items() if v.get()}
        self.checks = {}
        self._swatch_refs = []
        prefer = ("white", "black", "blue", "red")
        for fid, fil in self.db.filaments.items():
            on = fid in prev if prev else any(p == fil.name.lower() for p in prefer)
            self.checks[fid] = tk.BooleanVar(value=on)
            self.checks[fid].trace_add("write", lambda *_: self._refresh_estimate())
        self._render_filaments()

        names = [f.id for f in self.db.filaments.values()]
        self.cb_base["values"] = names
        if names and self.v_base.get() not in names:
            white = next((n for n in names if "white" in n), names[0])
            self.v_base.set(white)
        self._refresh_estimate()

    def _render_filaments(self):
        for w in self.fil_inner.winfo_children():
            w.destroy()
        self._swatch_refs = []
        needle = self.v_filter.get().strip().lower()
        rows = sorted(self.db.filaments.values(), key=lambda f: (f.brand, f.series, f.name))
        for fil in rows:
            if needle and needle not in fil.label().lower():
                continue
            row = ttk.Frame(self.fil_inner)
            row.pack(fill="x", pady=1)
            ttk.Checkbutton(row, variable=self.checks[fil.id]).pack(side="left")
            img = swatch_image(fil.rgb())
            self._swatch_refs.append(img)
            tk.Label(row, image=img, bg=BG).pack(side="left", padx=(0, 7))
            measured = fil.provenance == "measured"
            tk.Label(row, text=fil.name, bg=BG, fg=FG if measured else FG_DIM,
                     anchor="w").pack(side="left", fill="x", expand=True)
            tk.Label(row, text=f"td {fil.td:.2f}", bg=BG, fg=FG_DIM,
                     font=("TkFixedFont", 8)).pack(side="right")
            if not measured:
                tk.Label(row, text="est", bg=BG, fg=WARN,
                         font=("TkDefaultFont", 7)).pack(side="right", padx=4)

    def _set_all(self, val):
        for v in self.checks.values():
            v.set(val)

    def _select_measured(self):
        for fid, v in self.checks.items():
            v.set(self.db.filaments[fid].provenance == "measured")

    def selected(self):
        return [self.db.filaments[fid] for fid, v in self.checks.items() if v.get()]

    def config_ns(self):
        return SimpleNamespace(
            layer_height=float(self.v_layer.get()),
            first_layer_height=float(self.v_first.get()),
            max_layers=int(self.v_maxl.get()),
            base_layers=int(self.v_basel.get()),
            resolution=float(self.v_res.get()),
            width=float(self.v_width.get()),
            height=float(self.v_height.get()),
            grid=int(self.v_grid.get()),
            cap=400_000,
            dither=self.v_dither.get(),
            fit=self.v_fit.get(),
            slots=int(self.v_slots.get()),
            top=int(self.v_top.get()),
            rank_by=self.v_rankby.get(),
            rank_samples=int(self.v_samples.get()),
        )

    def _validate(self):
        if self.source_img is None:
            messagebox.showwarning(APP, "Open an image first.")
            return None
        sel = self.selected()
        if len(sel) < 2:
            messagebox.showwarning(APP, "Tick at least two filaments.")
            return None
        base_id = self.v_base.get()
        if base_id not in self.db.filaments:
            messagebox.showwarning(APP, "Choose a base filament.")
            return None
        base = self.db.filaments[base_id]
        if base.id not in {f.id for f in sel}:
            sel = [base] + sel
            self.checks[base.id].set(True)
        sel = [base] + [f for f in sel if f.id != base.id]
        return sel, base

    def _pick_template(self):
        p = filedialog.askopenfilename(title="Choose a project 3MF exported from your slicer",
                                       filetypes=[("3MF project", "*.3mf")])
        if not p:
            return
        try:
            z = zipfile.ZipFile(p)
        except (zipfile.BadZipFile, OSError) as exc:
            messagebox.showwarning(APP, f"{os.path.basename(p)} is not a 3MF: {exc}")
            return
        with z:
            if "Metadata/project_settings.config" not in z.namelist():
                messagebox.showwarning(
                    APP,
                    f"{os.path.basename(p)} has no project settings in it.\n\n"
                    "Export it from your slicer as a PROJECT (.3mf), not as a "
                    "plain model.")
                return
        self.v_template.set(p)
        self.lbl_template.config(text=os.path.basename(p), foreground=OK)
        # The template's profile is what will actually slice this, so let it
        # set the layer grid rather than leaving a silent mismatch.
        lh, flh = td3mf.template_layer_settings(p)
        if lh:
            self.v_layer.set(lh)
            self.v_first.set(flh or lh)
            self._status(f"adopted layer height {lh} mm, first layer "
                         f"{flh or lh} mm from template", OK)
        else:
            self._status("template has no layer height; set it to match "
                         "the profile by hand", WARN)
        self._refresh_estimate()

    def _clear_template(self):
        self.v_template.set("")
        self.lbl_template.config(text="(none)", foreground=FG_DIM)

    def _preset_vars(self):
        return {"width": self.v_width, "height": self.v_height, "res": self.v_res,
                "layer": self.v_layer, "first": self.v_first, "maxl": self.v_maxl,
                "basel": self.v_basel, "fit": self.v_fit, "dither": self.v_dither,
                "grid": self.v_grid, "slots": self.v_slots, "top": self.v_top,
                "rankby": self.v_rankby, "samples": self.v_samples,
                "flavor": self.v_flavor, "part": self.v_part,
                "template": self.v_template}

    def _read_presets(self):
        try:
            with open(PRESET_FILE) as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_presets(self, data):
        try:
            os.makedirs(os.path.dirname(PRESET_FILE), exist_ok=True)
            with open(PRESET_FILE, "w") as fh:
                json.dump(data, fh, indent=2)
        except OSError as exc:
            messagebox.showerror(APP, f"Could not write {PRESET_FILE}:\n{exc}")

    def _rebuild_presets_menu(self):
        if self.m_presets is None:
            return
        m = self.m_presets
        m.delete(0, "end")
        m.add_command(label="Save current settings as…", command=self._save_preset)
        names = sorted(self._read_presets())
        if names:
            m.add_separator()
            for n in names:
                m.add_command(label=n, command=lambda n=n: self._apply_preset(n))
            dm = tk.Menu(m, tearoff=0)
            for n in names:
                dm.add_command(label=n, command=lambda n=n: self._delete_preset(n))
            m.add_cascade(label="Delete", menu=dm)

    def _save_preset(self):
        name = simpledialog.askstring(APP, "Preset name:", parent=self)
        name = (name or "").strip()
        if not name:
            return
        data = self._read_presets()
        if name in data and not messagebox.askyesno(APP, f"Replace preset '{name}'?"):
            return
        try:
            vals = {k: v.get() for k, v in self._preset_vars().items()}
        except tk.TclError:
            messagebox.showwarning(APP, "A setting is empty or not a number.")
            return
        data[name] = {"vars": vals,
                      "filaments": sorted(f.id for f in self.selected()),
                      "base": self.v_base.get()}
        self._write_presets(data)
        self._rebuild_presets_menu()
        self._status(f"saved preset '{name}'", OK)

    def _delete_preset(self, name):
        if not messagebox.askyesno(APP, f"Delete preset '{name}'?"):
            return
        data = self._read_presets()
        data.pop(name, None)
        self._write_presets(data)
        self._rebuild_presets_menu()

    def _apply_preset(self, name):
        p = self._read_presets().get(name)
        if not p:
            return
        notes = []
        tvars = self._preset_vars()
        for k, val in p.get("vars", {}).items():
            if k in tvars:
                try:
                    tvars[k].set(val)
                except tk.TclError:
                    notes.append(f"ignored bad {k}")
        tpl = self.v_template.get()
        if tpl and not os.path.exists(tpl):
            notes.append(f"template {os.path.basename(tpl)} is missing")
            tpl = ""
        self.v_template.set(tpl)
        self.lbl_template.config(text=os.path.basename(tpl) if tpl else "(none)",
                                 foreground=OK if tpl else FG_DIM)
        want = set(p.get("filaments", []))
        missing = want - set(self.checks)
        for fid, v in self.checks.items():
            v.set(fid in want)
        if missing:
            notes.append(f"{len(missing)} filament(s) no longer in the database")
        if p.get("base") in self.db.filaments:
            self.v_base.set(p["base"])
        self._refresh_fit()
        self._refresh_estimate()
        self._status(f"loaded preset '{name}'" + (f" ({'; '.join(notes)})" if notes else ""),
                     WARN if notes else OK)

    def _pick_db(self):
        p = filedialog.askopenfilename(title="Open filament database",
                                       filetypes=[("JSON", "*.json")])
        if p:
            self.db_path = p
            self._reload_filaments()
