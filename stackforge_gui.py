#!/usr/bin/env python3
"""stackforge_gui -- desktop front end for stackforge.

Every option the CLI exposes is here, plus the things a GUI is actually better
at: seeing the simulated print next to the target while you turn knobs, and
picking a filament loadout by looking at renders rather than reading a table.

    python3 stackforge_gui.py [image.jpg]

Nothing heavy runs on the UI thread. Solves and rankings go to a worker and
report back through a queue, so the window stays responsive and every
long-running job can be cancelled.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import zipfile
import threading
import traceback
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import tkinter as tk
from PIL import Image
from tkinter import filedialog, messagebox, simpledialog, ttk

import guikit
import stackforge as sf
import tdcolor
import td3mf
from filamentdb import DB
from guikit import (ACCENT, BG, BG3, ERR, FG, FG_DIM, OK, WARN, ImageView,
                    Section, swatch_image)

APP = "stackforge"
PRESET_FILE = os.path.join(os.path.expanduser("~/.config"), "stackforge", "presets.json")


# --------------------------------------------------------------------------
# worker plumbing
# --------------------------------------------------------------------------


@dataclass
class Job:
    """A unit of background work and whatever it produced."""

    kind: str
    result: object = None
    error: str = ""


class Worker:
    """Runs one job at a time on a thread, reporting through a queue.

    Cancellation is cooperative: `cancel()` sets a flag that progress
    callbacks check, so a job stops at its next checkpoint rather than being
    killed mid-write.
    """

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def submit(self, kind, fn):
        if self.busy:
            return False
        self._cancel.clear()

        def run():
            try:
                self.q.put(Job(kind, fn()))
            except Cancelled:
                self.q.put(Job("cancelled"))
            except BaseException:
                # SystemExit too: filamentdb/td3mf report bad data that way,
                # and an uncaught one kills this thread with the UI still busy.
                self.q.put(Job(kind, error=traceback.format_exc()))

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return True

    def progress(self, frac, msg):
        if self._cancel.is_set():
            raise Cancelled()
        self.q.put(Job("progress", (frac, msg)))


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------


class App(tk.Tk):
    def __init__(self, image_path=None, db_path="filaments.json"):
        super().__init__()
        self.title(f"{APP} — full-colour plaques from filament stacks")
        self.geometry("1500x950")
        self.minsize(1180, 760)
        self.configure(bg=BG)

        self.worker = Worker()
        self.db_path = db_path
        self.db = DB(db_path)
        self.image_path = None
        self.source_img = None      # PIL, full res as loaded
        self.fitted = None          # ndarray at working resolution
        self.result = None          # dict from the last generate; None once stale
        self.rank_results = None

        self._init_style()
        self._build_menu()
        self._build_ui()
        self._poll()

        self._reload_filaments()
        if image_path:
            self._load_image(image_path)
        self._refresh_estimate()

    # -- chrome ----------------------------------------------------------

    def _init_style(self):
        guikit.apply_theme(self)

    def _build_menu(self):
        m = tk.Menu(self)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="Open image…", accelerator="Ctrl+O", command=self._open_image)
        f.add_command(label="Export 3MF…", accelerator="Ctrl+E", command=self._export)
        f.add_separator()
        f.add_command(label="Save preview PNG…", command=self._save_preview)
        f.add_separator()
        f.add_command(label="Quit", accelerator="Ctrl+Q", command=self.destroy)
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
        self.config(menu=m)

        self.bind("<Control-o>", lambda e: self._open_image())
        self.bind("<Control-e>", lambda e: self._export())
        self.bind("<Control-q>", lambda e: self.destroy())

    def _build_ui(self):
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill="both", expand=True)

        panes = ttk.PanedWindow(outer, orient="horizontal")
        panes.pack(fill="both", expand=True)

        panes.add(self._build_left(panes), weight=0)
        panes.add(self._build_center(panes), weight=1)
        panes.add(self._build_right(panes), weight=0)

        self._build_status(outer)

    # -- left: filament library -----------------------------------------

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

    # -- centre: previews ------------------------------------------------

    def _build_center(self, master):
        f = ttk.Frame(master)
        self.nb = ttk.Notebook(f)
        self.nb.pack(fill="both", expand=True)

        self.view_target = ImageView(self.nb, "Open an image to begin  (Ctrl+O)")
        self.view_sim = ImageView(self.nb, "Generate to see the simulated print")
        self.view_err = ImageView(self.nb, "Generate to see where colour is unreachable")
        self.view_combo = ImageView(self.nb, "Tick more filaments than toolheads and Generate ranks them")

        self.nb.add(self.view_target, text="Target")
        self.nb.add(self.view_sim, text="Simulated print")
        self.nb.add(self.view_err, text="Error map")
        self.nb.add(self.view_combo, text="Combinations")

        legend = ttk.Frame(f)
        legend.pack(fill="x", pady=(6, 0))
        self.lbl_legend = ttk.Label(legend, text="", style="Hint.TLabel")
        self.lbl_legend.pack(side="left")
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._update_legend())
        return f

    def _update_legend(self):
        tab = self.nb.index(self.nb.select())
        self.lbl_legend.config(text=[
            "The image as it will be laid out, fitted to the plaque.",
            "How the print should actually look, composited through the filament stacks.",
            "Green = reachable. Red = outside the gamut; add a filament rather than "
            "changing settings.",
            "Target first, then the best loadouts with their swatches and error.",
        ][tab])

    # -- right: parameters -----------------------------------------------

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

    # -- bottom: actions and status --------------------------------------

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

    # -- filament list ---------------------------------------------------

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

    # -- config ----------------------------------------------------------

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

    def _refresh_estimate(self):
        self._check_stale()
        try:
            a = self.config_ns()
        except (tk.TclError, ValueError):
            return
        problems = sf.check_args(a)
        if problems:
            self.lbl_est.config(text="\n".join(problems))
            return
        sel = self.selected()
        # _validate adds the base if it is unticked, so count it here too.
        base_id = self.v_base.get()
        if base_id in self.db.filaments and base_id not in {f.id for f in sel}:
            sel = sel + [self.db.filaments[base_id]]
        n, slots = len(sel), a.slots
        total = (a.first_layer_height
                 + (a.base_layers - 1 + a.max_layers) * a.layer_height)
        w_px = max(1, int(round(a.width / a.resolution)))
        if self.source_img is not None:
            h_px = (max(1, int(round(a.height / a.resolution))) if a.height > 0
                    else max(1, int(round(w_px * self.source_img.height / self.source_img.width))))
        else:
            h_px = w_px
        combos = 0
        if n > slots:
            from math import comb
            combos = comb(max(0, n - 1), slots - 1)

        lines = [
            f"grid       {w_px} x {h_px} px",
            f"plaque     {w_px*a.resolution:.0f} x {h_px*a.resolution:.0f} x {total:.2f} mm",
            f"layers     {a.base_layers} base + {a.max_layers} colour",
            f"selected   {n} filament{'s' if n != 1 else ''}",
        ]
        if combos:
            lines.append(f"ranking    {combos} combinations")
        base = self.db.filaments.get(self.v_base.get())
        if base is not None:
            base_h = a.first_layer_height + (a.base_layers - 1) * a.layer_height
            try:
                t = float(base.transmittance(base_h).max())
            except SystemExit:
                t = 0.0
            if t > 0.01:
                lines.append(f"base       {base_h:.2f} mm passes {100*t:.0f}% — not opaque")
        est = [f.id for f in sel if f.provenance != "measured"]
        if est:
            lines.append(f"unmeasured {len(est)} of {n}")
        self.lbl_est.config(text="\n".join(lines))

    # -- image -----------------------------------------------------------

    def _open_image(self):
        p = filedialog.askopenfilename(
            title="Open image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp"),
                       ("All files", "*.*")])
        if p:
            self._load_image(p)

    def _load_image(self, path):
        try:
            self.source_img = tdcolor.open_image(path)
        except Exception as exc:
            messagebox.showerror(APP, f"Could not open {path}:\n{exc}")
            return
        self.image_path = path
        self.title(f"{APP} — {os.path.basename(path)}")
        self._refresh_fit()
        self._refresh_estimate()
        self._status(f"loaded {os.path.basename(path)} "
                     f"({self.source_img.width}x{self.source_img.height})")

    def _refresh_fit(self):
        if self.source_img is None:
            return
        a = self.config_ns()
        w_px = max(1, int(round(a.width / a.resolution)))
        h_px = (max(1, int(round(a.height / a.resolution))) if a.height > 0
                else max(1, int(round(w_px * self.source_img.height / self.source_img.width))))
        base = self.db.filaments.get(self.v_base.get())
        pad = tuple(int(v) for v in base.rgb()) if base else (255, 255, 255)
        self.fitted = tdcolor.fit_image(self.image_path, w_px, h_px, a.fit, pad=pad)
        self.view_target.set_image(self.fitted)
        self.nb.select(0)
        self._update_legend()

    # -- actions ---------------------------------------------------------

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

    # -- staleness: any input change steps the pipeline back ---------------

    def _signature(self):
        """Everything the generated result depends on, or None if unreadable."""
        try:
            a = self.config_ns()
        except (tk.TclError, ValueError):
            return None
        pool = tuple(sorted(repr(f) for f in self.selected()))
        return (tuple(sorted(vars(a).items())), self.image_path, pool, self.v_base.get())

    def _check_stale(self):
        if self.result is None or self.result["sig"] == self._signature():
            return
        self.result = None
        self.rank_results = None
        self.btn_export.config(state="disabled")
        self._tab_titles(stale=True)
        self._status("inputs changed: Generate to update the preview", WARN)

    def _tab_titles(self, stale):
        sfx = " (out of date)" if stale else ""
        for view, title in ((self.view_sim, "Simulated print"),
                            (self.view_err, "Error map"),
                            (self.view_combo, "Combinations")):
            self.nb.tab(view, text=title + sfx)

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

    def _on_solved(self, r):
        self._finish()
        self.result = r
        self.rank_results = r["ranked"]
        self._tab_titles(stale=False)
        if r["ranked"]:
            self.view_combo.set_image(Image.open(r["ranked"]["sheet"]))
        else:
            self.view_combo.set_image(None)
        self.view_sim.set_image(r["achieved"])
        heat = np.clip(r["err"] / 20.0, 0, 1)
        self.view_err.set_image(
            (np.stack([heat, 1 - heat, np.zeros_like(heat)], -1) * 255).astype(np.uint8))
        self.nb.select(1)
        self._update_legend()
        self.btn_export.config(state="normal")

        e = r["err"]
        labels = r["labels"]
        per_layer = np.mean([len(np.unique(labels[i])) for i in range(labels.shape[0])])
        capped = "   ·   gamut hit the state cap" if r["gamut"].capped else ""
        thin = sf.thin_fraction(labels, r["gamut"].base_index)
        if thin > 0.05 and r["args"].resolution < sf.MIN_FEATURE_MM - 1e-9:
            capped += f"   ·   {100*thin:.0f}% of colour in 1-px features (slicer drops them)"
        if r["ranked"]:
            capped = (f"   ·   best of {len(r['ranked']['results'])} combinations: "
                      + ", ".join(f.name for f in r["fils"]) + capped)
        self._status(
            f"dE mean {e.mean():.1f}  p95 {np.percentile(e,95):.1f}  "
            f"max {e.max():.1f}  (blurred {r['blurred']:.1f})   ·   "
            f"{per_layer:.1f} filaments per layer{capped}",
            OK if e.mean() < 8 else WARN)
        self._check_stale()     # inputs may have changed while it ran

    def _export(self):
        if self.worker.busy:            # Ctrl+E bypasses the disabled button
            return
        if not self.result:
            messagebox.showwarning(APP, "Nothing to export yet: press Generate "
                                        "(it is out of date if you changed anything).")
            return
        r = self.result
        p = filedialog.asksaveasfilename(
            title="Export 3MF", defaultextension=".3mf",
            filetypes=[("3MF", "*.3mf")],
            initialfile=os.path.splitext(os.path.basename(r["image_path"] or "plaque"))[0]
            + "_plaque.3mf")
        if not p:
            return
        a = r["args"]
        try:
            plate, decals, total_h = sf.build_geometry(
                r["labels"], a.width, a.resolution, a.layer_height,
                a.first_layer_height + (a.base_layers - 1) * a.layer_height,
                r["gamut"].base_index, len(r["fils"]))
            sf.write_plaque(
                p, self.v_flavor.get(), plate, decals, r["fils"],
                r["gamut"].base_index, self.v_part.get(),
                self.v_template.get() or None, a.layer_height,
                a.first_layer_height)
        except (Exception, SystemExit) as exc:
            messagebox.showerror(APP, f"Export failed:\n{exc}")
            return
        nbox = sum(len(t) // 12 for _, _, t in decals)
        size = os.path.getsize(p) / 1e6
        self._status(f"wrote {os.path.basename(p)}  ({size:.1f} MB, {nbox} boxes)", OK)
        messagebox.showinfo(
            APP,
            f"Wrote {p}\n\n"
            f"{total_h:.2f} mm thick · {nbox} boxes · {size:.1f} MB\n\n"
            f"Load order:\n" +
            "\n".join(f"  T{i}  {f.label()}" for i, f in enumerate(r["fils"], 1)) +
            f"\n\nSlice at layer height EXACTLY {a.layer_height} mm with a "
            f"{a.first_layer_height} mm first layer, or the modifiers land "
            f"between layers and colours drop out.")

    def _save_preview(self):
        if not self.result:
            messagebox.showwarning(APP, "Generate a preview first.")
            return
        p = filedialog.asksaveasfilename(defaultextension=".png",
                                         filetypes=[("PNG", "*.png")])
        if p:
            Image.fromarray(self.result["achieved"].astype(np.uint8)).save(p)
            self._status(f"saved {os.path.basename(p)}", OK)

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

    # -- presets ---------------------------------------------------------

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

    def _edit_filaments(self):
        # Imported here rather than at module scope: the editor is a separate
        # tool and nothing in a normal solve needs it loaded.
        import filamentdb_gui

        def reopen(path):
            self.db_path = path
            self._reload_filaments()
            self._status("filament database reloaded", OK)

        filamentdb_gui.open_window(self, self.db_path, on_close=reopen)

    def _pick_db(self):
        p = filedialog.askopenfilename(title="Open filament database",
                                       filetypes=[("JSON", "*.json")])
        if p:
            self.db_path = p
            self._reload_filaments()

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


def main():
    path = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None
    db = os.environ.get("FILAMENT_DB", "filaments.json")
    App(path, db).mainloop()


if __name__ == "__main__":
    main()
