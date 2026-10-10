"""Plaque designer: pick an image and filaments, see the simulated print, export a 3MF.

Everything the plaque CLI exposes, plus what a GUI is better at: seeing the result next to
the target while you turn knobs, and choosing a loadout by looking at renders. Nothing heavy
runs on the UI thread: solves and rankings go to a worker, so the window stays responsive and
every job can be cancelled. Layer height, flavor, part type and template come from the
project bar, so there is one place for them.
"""
from __future__ import annotations

import os
import queue
import tempfile
from math import comb
from types import SimpleNamespace

import numpy as np
from PIL import Image
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QScrollArea,
                               QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget)

from stackforge.core import colormath, spectral, threemf
from stackforge.core.filamentdb import DB, DEFAULT_DB, PROVENANCE_LABEL, provenance_counts
from stackforge.gui import theme
from stackforge.gui.imageview import ImageView
from stackforge.gui.worker import Worker, show_error
from stackforge.tools import plaque

APP = "Plaque"
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp);;All files (*)"
LEGEND = [
    "The image as it will be laid out, fitted to the plaque.",
    "How the print should actually look, composited through the filament stacks.",
    "Green = reachable. Red = outside the gamut; add a filament rather than changing settings.",
    "Target first, then the best loadouts with their swatches and error.",
]
EMPTY = ["1. Open an image   2. Tick the filaments you own   3. Press Generate",
         "Generate to see the simulated print",
         "Generate to see where colour is unreachable",
         "Tick more filaments than toolheads and Generate ranks them"]


def _spin(lo, hi, step, value, decimals=None):
    w = QSpinBox() if decimals is None else QDoubleSpinBox()
    if decimals is not None:
        w.setDecimals(decimals)
    w.setRange(lo, hi)
    w.setSingleStep(step)
    w.setValue(value)
    w.setMaximumWidth(120)
    w.setKeyboardTracking(False)
    return w


def _combo(values, current):
    c = QComboBox()
    c.addItems(values)
    c.setCurrentText(current)
    return c


def _swatch(rgb, w=18, h=14) -> QIcon:
    pm = QPixmap(w, h)
    pm.fill(QColor(*[int(v) for v in rgb]))
    return QIcon(pm)


class PlaqueDesigner(QWidget):
    title = "Plaque"

    def __init__(self, project=None, presets=None, host=None, image_path=None):
        super().__init__()
        self.project, self.presets, self.host = project, presets, host
        self.worker = Worker()
        self.db_path = project.get("db") if project else DEFAULT_DB
        self.db = DB(self.db_path)
        self.image_path = None
        self.source_img = None
        self.fitted = None
        self.result = None
        self.rank_results = None
        self._loading = False
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        if project is not None:
            project.subscribe(self._project_changed)
        self._reload_filaments()
        if image_path:
            self._load_image(image_path)
        self._refresh_estimate()

    # ---- layout ------------------------------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        split = QSplitter()
        outer.addWidget(split, 1)
        split.addWidget(self._build_left())
        split.addWidget(self._build_center())
        split.addWidget(self._build_right())
        split.setSizes([320, 700, 340])
        split.setStretchFactor(1, 1)
        outer.addLayout(self._build_bar())

    def _build_left(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 4, 0)

        img = QGroupBox("1 · Image")
        il = QHBoxLayout(img)
        self.btn_open = QPushButton("Open image…")
        self.btn_open.clicked.connect(self._open_image)
        self.lbl_image = QLabel("none yet")
        self.lbl_image.setObjectName("hint")
        il.addWidget(self.btn_open)
        il.addWidget(self.lbl_image, 1)
        lay.addWidget(img)

        box = QGroupBox("2 · Filaments you own or might load")
        bl = QVBoxLayout(box)
        bl.addWidget(theme.hint("Tick them all. Ticking more than the toolhead count makes Generate "
                                "rank the combinations for you."))
        row = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter…")
        self.filter.textChanged.connect(self._apply_filter)
        reload_ = QPushButton("⟳")
        reload_.setToolTip("Reload the database")
        reload_.setMaximumWidth(34)
        reload_.clicked.connect(self._reload_filaments)
        row.addWidget(self.filter, 1)
        row.addWidget(reload_)
        bl.addLayout(row)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.itemChanged.connect(lambda _i: self._inputs_changed())
        bl.addWidget(self.list, 1)
        self.lbl_measured = QLabel()
        self.lbl_measured.setObjectName("warn")
        self.lbl_measured.setWordWrap(True)
        bl.addWidget(self.lbl_measured)
        quick = QHBoxLayout()
        for text, fn in (("All", lambda: self._set_all(True)), ("None", lambda: self._set_all(False)),
                         ("Measured only", self._select_measured)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            quick.addWidget(b)
        bl.addLayout(quick)
        bl.addWidget(QLabel("Base (opaque backing)"))
        self.base = QComboBox()
        self.base.currentTextChanged.connect(lambda _t: self._inputs_changed())
        self.base.activated.connect(lambda *_: setattr(self, "_held_base", ""))   # the user chose
        bl.addWidget(self.base)
        bl.addWidget(theme.hint("The base takes one toolhead and is a full colour too: short stacks pad "
                                "against it. Usually white or black."))
        lay.addWidget(box, 1)
        return w

    def _build_center(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 0, 4, 0)
        self.views = QTabWidget()
        names = ("Target", "Simulated print", "Error map", "Combinations")
        self.view = [ImageView(EMPTY[i]) for i in range(4)]
        for name, v in zip(names, self.view):
            self.views.addTab(v, name)
        self.views.currentChanged.connect(self._update_legend)
        lay.addWidget(self.views, 1)
        self.legend = theme.hint(LEGEND[0])
        lay.addWidget(self.legend)
        return w

    def _build_right(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(4, 0, 8, 0)
        scroll.setWidget(host)

        def section(title, note=None):
            g = QGroupBox(title)
            gl = QVBoxLayout(g)
            if note:
                gl.addWidget(theme.hint(note))
            f = QFormLayout()
            f.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
            f.setVerticalSpacing(6)
            gl.addLayout(f)
            col.addWidget(g)
            return f

        g = section("3 · Plaque geometry", "Layer height, first layer, template, flavor and part type "
                                           "come from the project bar at the top.")
        self.s_width = _spin(10, 500, 5, 150.0, 1)
        self.s_height = _spin(0, 500, 5, 0.0, 1)
        self.s_res = _spin(0.1, 2.0, 0.05, plaque.MIN_FEATURE_MM, 2)
        self.s_maxl = _spin(2, 40, 1, 16)
        self.s_basel = _spin(0, 80, 1, 0)
        self.s_basel.setSpecialValueText("auto")
        g.addRow("Width (mm)", self.s_width)
        g.addRow("Height (mm)", self.s_height)
        g.addRow("", theme.hint("0 keeps the image's aspect ratio."))
        g.addRow("Resolution (mm)", self.s_res)
        g.addRow("", theme.hint("Match your nozzle width. Finer means far more geometry."))
        g.addRow("Colour layers", self.s_maxl)
        g.addRow("", theme.hint("The gamut stops growing once the deepest stack goes opaque, so past "
                                "~16 this is just print time."))
        g.addRow("Base layers", self.s_basel)
        g.addRow("", theme.hint("Auto picks the fewest layers that stop light getting through the "
                                "base; a see-through base makes every colour wrong."))

        c = section("Colour")
        self.c_optics = _combo(["rgb", "spectral"], "rgb")
        self.c_illum = _combo(list(spectral.ILLUMINANTS), "D65")
        c.addRow("Optics", self.c_optics)
        c.addRow("", theme.hint("spectral: Kubelka-Munk per wavelength from measured wedge spectra. "
                                "Only spectrally calibrated filaments can be ticked."))
        c.addRow("Viewing light", self.c_illum)
        self.c_fit = _combo(["cover", "contain", "stretch"], "cover")
        self.c_dither = _combo(["none", "ordered", "blue", "floyd"], "none")
        self.s_grid = _spin(48, 384, 16, 192)
        c.addRow("Image fit", self.c_fit)
        c.addRow("Dither", self.c_dither)
        c.addRow("", theme.hint("Rarely helps: stacking already fills the gamut densely. Try it with "
                                "2–3 filaments."))
        c.addRow("Gamut grid", self.s_grid)
        c.addRow("", theme.hint("Higher is finer and slower."))

        r = section("Combination ranking")
        self.s_slots = _spin(2, 8, 1, 4)
        self.s_top = _spin(1, 12, 1, 5)
        self.c_rankby = _combo(["mean", "p95"], "mean")
        self.s_samples = _spin(500, 100000, 500, 20000)
        r.addRow("Toolheads", self.s_slots)
        r.addRow("", theme.hint("Total spools loaded, including the base. 4 for a four-toolhead machine."))
        r.addRow("Show best", self.s_top)
        r.addRow("Rank by", self.c_rankby)
        r.addRow("", theme.hint("p95 targets worst-case error instead of the average."))
        r.addRow("Colours scored", self.s_samples)

        e = QGroupBox("Estimate")
        el = QVBoxLayout(e)
        self.lbl_est = QLabel()
        theme.mono(self.lbl_est)
        self.lbl_est.setWordWrap(True)
        el.addWidget(self.lbl_est)
        col.addWidget(e)

        if self.presets is not None:
            p = QGroupBox("Presets")
            pl = QHBoxLayout(p)
            self.preset_combo = QComboBox()
            pl.addWidget(self.preset_combo, 1)
            for text, fn in (("Load", self._apply_preset), ("Save…", self._save_preset),
                             ("Delete", self._delete_preset)):
                b = QPushButton(text)
                b.clicked.connect(fn)
                pl.addWidget(b)
            col.addWidget(p)
            self._refresh_presets()
        col.addStretch(1)

        for wdg in (self.s_width, self.s_height, self.s_res, self.s_maxl, self.s_basel, self.s_grid,
                    self.s_slots, self.s_top, self.s_samples):
            wdg.valueChanged.connect(lambda *_: self._inputs_changed())
        self.c_fit.currentTextChanged.connect(lambda *_: (self._refresh_fit(), self._inputs_changed()))
        for wdg in (self.c_dither, self.c_rankby, self.c_illum):
            wdg.currentTextChanged.connect(lambda *_: self._inputs_changed())
        self.c_illum.setEnabled(False)
        self.c_optics.currentTextChanged.connect(lambda *_: self._optics_changed())
        return scroll

    def _spectral(self) -> bool:
        return self.c_optics.currentText() == "spectral"

    def _optics_changed(self):
        self.c_illum.setEnabled(self._spectral())
        self._reload_filaments()

    def _build_bar(self):
        bar = QHBoxLayout()
        self.btn_go = QPushButton("Generate")
        self.btn_go.setObjectName("primary")
        self.btn_go.clicked.connect(self._generate)
        self.btn_export = QPushButton("Export 3MF…")
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self._export)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.worker.cancel)
        self.lbl_status = QLabel("Ready")
        self.prog = QProgressBar()
        self.prog.setRange(0, 100)
        self.prog.setMaximumWidth(220)
        self.prog.setTextVisible(False)
        for wdg in (self.btn_go, self.btn_export, self.btn_cancel):
            bar.addWidget(wdg)
        bar.addWidget(self.lbl_status, 1)
        bar.addWidget(self.prog)
        return bar

    # ---- host protocol -----------------------------------------------------------------
    def shortcuts(self) -> dict:
        return {"Ctrl+O": self._open_image, "Ctrl+E": self._export}

    def on_show(self):
        try:
            mtime = os.path.getmtime(self.db_path)
        except OSError:
            return
        if mtime != getattr(self, "_db_mtime", mtime):
            self._reload_filaments()
        self._db_mtime = mtime

    def set_db(self, path):
        self.db_path = path
        self._reload_filaments()

    def _project_changed(self):
        db = self.project.get("db")
        if db != self.db_path:
            self.set_db(db)
        else:
            self._refresh_fit_quiet()
            self._inputs_changed()

    def _refresh_fit_quiet(self):
        pass

    # ---- project values ----------------------------------------------------------------
    def _layers(self):
        """The project's layer grid; 0.08 stands in only for the estimate, never for a print
        (Generate refuses while _grid_problem() says so)."""
        lh, fl = self.project.layers() if self.project else (None, None)
        lh = lh or 0.08
        return lh, fl or lh

    def _grid_problem(self):
        """Why a plaque can't be built on the right layer grid yet, in plain words, or None."""
        t = self._template()
        if t and not os.path.exists(t):
            return ("Your Flash Studio project file was moved or deleted:\n" + t
                    + "\n\nChoose it again with the \u201cSlicer project\u201d box at the top.")
        if t:
            try:
                threemf.check_template(t)
            except SystemExit as exc:
                return str(exc)
        lh = self.project.layers()[0] if self.project else None
        if self._flavor() == "orca" and lh is None:
            return ("First choose your Flash Studio project file with the \u201cSlicer project\u201d "
                    "box at the top.\n\nIt tells stackforge your printer, your four filament "
                    "slots and the layer height the slicer will use; the plaque must be built "
                    "on exactly that layer height or the colours print on alternate layers.")
        return None

    def _flavor(self):
        return (self.project.get("flavor") if self.project else None) or "orca"

    def _part(self):
        return (self.project.get("part_type") if self.project else None) or "modifier"

    def _template(self):
        return (self.project.get("template") if self.project else None) or None

    # ---- filaments ---------------------------------------------------------------------
    def _checked(self) -> set:
        return {self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked}

    def _reload_filaments(self):
        try:
            self.db = DB(self.db_path)
        except SystemExit as exc:
            QMessageBox.critical(self, APP, str(exc))
            return
        # Keep whatever was ticked across a reload: editing the database should not silently
        # throw away the loadout being worked on.
        # Spectral optics unticks uncalibrated filaments; _held remembers them so switching back
        # to RGB restores the loadout.
        prev = self._checked() | getattr(self, "_held", set())
        held = set()
        prefer = ("white", "black", "blue", "red")
        self._loading = True
        self.list.clear()
        rows = sorted(self.db.filaments.values(), key=lambda f: (f.brand, f.series, f.name))
        spec = self._spectral()
        for fil in rows:
            prov = PROVENANCE_LABEL.get(fil.provenance, fil.provenance)
            cal = spectral.is_calibrated(fil)
            if spec:
                text = f"{fil.name}      " + ("spectral" if cal else "no spectral calibration")
                tip = (f"{fil.label()}\nK/S fitted {fil.spectral.get('measured_at') or '?'}" if cal else
                       f"{fil.label()}\nno spectral calibration: fit one with stackforge-spectral fit")
            else:
                text, tip = f"{fil.name}      td {fil.td:.2f} · {prov}", f"{fil.label()}\ntd source: {prov}"
            it = QListWidgetItem(_swatch(fil.rgb()), text)
            it.setData(Qt.UserRole, fil.id)
            it.setToolTip(tip)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            on = fil.id in prev if prev else any(p == fil.name.lower() for p in prefer)
            if spec and not cal:
                # Spectral optics refuses uncalibrated filaments; say so here, not at Generate.
                if on:
                    held.add(fil.id)
                on = False
                it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
            it.setCheckState(Qt.Checked if on else Qt.Unchecked)
            it.setForeground(QColor(theme.OK if spec and cal else theme.FG_DIM if spec else
                                    theme.PROVENANCE_COLOUR.get(fil.provenance, theme.FG_DIM)))
            self.list.addItem(it)
        self._held = held
        # Under spectral optics the base must be calibrated too: offer only those.
        bases = [f for f in self.db.filaments.values() if not spec or spectral.is_calibrated(f)]
        names = [f.id for f in bases]
        self.base.blockSignals(True)
        # A base dropped for being uncalibrated comes back when it is offered again.
        cur = getattr(self, "_held_base", "") or self._base_id()
        if cur in names:
            self._held_base = ""
        elif cur:
            self._held_base = cur
        self.base.clear()
        for fil in bases:
            self.base.addItem(_swatch(fil.rgb()), fil.label(), fil.id)
        if names:
            self._set_base(cur if cur in names else next((n for n in names if "white" in n), names[0]))
        self.base.blockSignals(False)
        self._loading = False
        self._apply_filter()
        self._inputs_changed()

    def _apply_filter(self):
        needle = self.filter.text().strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            fil = self.db.filaments.get(it.data(Qt.UserRole))
            it.setHidden(bool(needle) and fil is not None and needle not in fil.label().lower())

    def _set_all(self, on):
        self._loading = True
        for i in range(self.list.count()):
            it = self.list.item(i)
            if not it.isHidden() and it.flags() & Qt.ItemIsEnabled:
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)
        self._loading = False
        self._inputs_changed()

    def _select_measured(self):
        self._loading = True
        for i in range(self.list.count()):
            it = self.list.item(i)
            fil = self.db.filaments[it.data(Qt.UserRole)]
            # Under spectral optics "measured" means a spectral calibration, not an RGB td fit.
            ok = spectral.is_calibrated(fil) if self._spectral() else fil.provenance == "measured"
            it.setCheckState(Qt.Checked if ok else Qt.Unchecked)
        self._loading = False
        self._inputs_changed()

    def _set_base(self, fid):
        i = self.base.findData(fid)
        if i >= 0:
            self.base.setCurrentIndex(i)

    def _set_checked(self, ids: set):
        self._loading = True
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setCheckState(Qt.Checked if it.data(Qt.UserRole) in ids else Qt.Unchecked)
        self._loading = False

    def selected(self):
        ids = self._checked()
        return [f for fid, f in self.db.filaments.items() if fid in ids]

    # ---- configuration -----------------------------------------------------------------
    def config_ns(self):
        lh, fl = self._layers()
        return SimpleNamespace(
            layer_height=lh, first_layer_height=fl, max_layers=self.s_maxl.value(),
            base_layers=self.s_basel.value() or None, resolution=self.s_res.value(),
            width=self.s_width.value(), height=self.s_height.value(), grid=self.s_grid.value(),
            cap=400_000, dither=self.c_dither.currentText(), fit=self.c_fit.currentText(),
            slots=self.s_slots.value(), top=self.s_top.value(), rank_by=self.c_rankby.currentText(),
            rank_samples=self.s_samples.value(), optics=self.c_optics.currentText(),
            illuminant=self.c_illum.currentText())

    def _inputs_changed(self):
        if self._loading:
            return
        self._refresh_estimate()

    def _validate(self):
        if self.source_img is None:
            QMessageBox.warning(self, APP, "Open an image first.")
            return None
        sel = self.selected()
        if len(sel) < 2:
            QMessageBox.warning(self, APP, "Tick at least two filaments.")
            return None
        base_id = self._base_id()
        if base_id not in self.db.filaments:
            QMessageBox.warning(self, APP, "Choose a base filament.")
            return None
        problem = self._grid_problem()
        if problem:
            QMessageBox.warning(self, APP, problem)
            return None
        a = self.config_ns()
        w_px, h_px = plaque.pixel_grid(a, self.source_img.width, self.source_img.height)
        try:
            plaque.check_bed(self._template(), w_px * a.resolution, h_px * a.resolution)
            if self._template():
                threemf.check_template_slots(self._template(), min(len(sel), a.slots))
        except SystemExit as exc:
            QMessageBox.warning(self, APP, str(exc))
            return None
        base = self.db.filaments[base_id]
        if self._spectral():
            try:
                spectral.require_calibrated(list(sel) + [base], "Spectral optics")
            except SystemExit as exc:
                QMessageBox.warning(self, APP, str(exc))
                return None
        if base.id not in {f.id for f in sel}:
            self._set_checked(self._checked() | {base.id})
        sel = [base] + [f for f in sel if f.id != base.id]
        return sel, base

    def _base_id(self):
        return self.base.currentData() or ""

    # ---- image, estimate, staleness ----------------------------------------------------
    def _open_image(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open image", "", IMAGE_FILTER)
        if p:
            self._load_image(p)

    def _load_image(self, path):
        try:
            self.source_img = colormath.open_image(path)
        except Exception as exc:
            QMessageBox.critical(self, APP, f"Could not open {path}:\n{exc}")
            return
        self.image_path = path
        self.lbl_image.setText(os.path.basename(path))
        self._refresh_fit()
        self._refresh_estimate()
        self._status(f"loaded {os.path.basename(path)} ({self.source_img.width}x{self.source_img.height})")

    def _refresh_fit(self):
        if self.source_img is None:
            return
        a = self.config_ns()
        w_px, h_px = plaque.pixel_grid(a, self.source_img.width, self.source_img.height)
        base = self.db.filaments.get(self._base_id())
        pad = tuple(int(v) for v in base.rgb()) if base else (255, 255, 255)
        self.fitted = colormath.fit_image(self.image_path, w_px, h_px, a.fit, pad=pad)
        self.view[0].set_image(self.fitted)
        self.views.setCurrentIndex(0)
        self._update_legend()

    def _update_legend(self, *_):
        self.legend.setText(LEGEND[self.views.currentIndex()])

    def _signature(self):
        pool = tuple(sorted(repr(f) for f in self.selected()))
        return (tuple(sorted(vars(self.config_ns()).items())), self.image_path, pool,
                self._base_id(), self._template())

    def _check_stale(self):
        if self.result is None or self.result["sig"] == self._signature():
            return
        self.result = None
        self.rank_results = None
        self.btn_export.setEnabled(False)
        self._tab_titles(stale=True)
        self._status("inputs changed: Generate to update the preview", theme.WARN)

    def _tab_titles(self, stale):
        sfx = " (out of date)" if stale else ""
        for i, title in ((1, "Simulated print"), (2, "Error map"), (3, "Combinations")):
            self.views.setTabText(i, title + sfx)

    def _refresh_estimate(self):
        self._check_stale()
        a = self.config_ns()
        problems = plaque.check_args(a)
        if problems:
            self.lbl_est.setText("\n".join(problems))
            return
        sel = self.selected()
        base_id = self._base_id()
        if base_id in self.db.filaments and base_id not in {f.id for f in sel}:
            sel = sel + [self.db.filaments[base_id]]      # _validate adds the base if unticked
        n, slots = len(sel), a.slots
        base = self.db.filaments.get(base_id)
        auto = a.base_layers is None
        spec = self._spectral()
        if base is not None and spec and not spectral.is_calibrated(base):
            base = None                        # no K/S to judge it by; _validate refuses it
        if base is not None:
            plaque.resolve_base_layers(a, base, log=lambda _m: None)
        else:
            a.base_layers = a.base_layers or 1
        total = a.first_layer_height + (a.base_layers - 1 + a.max_layers) * a.layer_height
        if self.source_img is not None:
            w_px, h_px = plaque.pixel_grid(a, self.source_img.width, self.source_img.height)
        else:
            w_px = h_px = plaque.pixel_grid(a, 1, 1)[0]
        combos = comb(max(0, n - 1), slots - 1) if n > slots else 0
        lines = [f"grid       {w_px} x {h_px} px",
                 f"plaque     {w_px*a.resolution:.0f} x {h_px*a.resolution:.0f} x {total:.2f} mm",
                 f"layers     {a.base_layers}{' (auto)' if auto else ''} base + {a.max_layers} colour "
                 f"at {a.layer_height:g} / {a.first_layer_height:g} mm",
                 f"selected   {n} filament{'s' if n != 1 else ''}"]
        if combos:
            lines.append(f"ranking    {combos} combinations")
        warn = None
        if base is not None:
            try:
                warn = plaque.base_warning(base, a)
            except (SystemExit, spectral.SpectralError):
                warn = None
            if warn:
                lines.append("base       not opaque: " + warn)
        if spec:
            # Spectral optics judges filaments by their spectral calibration, not the RGB td.
            est = [f.id for f in sel if not spectral.is_calibrated(f)]
            if est:
                lines.append(f"spectral   {len(est)} of {n} have no spectral calibration: "
                             f"Generate will refuse them")
            for w in spectral.layer_mismatch([f for f in sel if f.id not in est], a.layer_height):
                lines.append("spectral   " + w)
            self.lbl_measured.setText("")
            est = []
        else:
            est = [f.id for f in sel if f.provenance != "measured"]
            if est:
                lines.append(f"unmeasured {len(est)} of {n}: colours are approximate until calibrated")
            self.lbl_measured.setText(
                f"{len(est)} of the {n} ticked filaments are not measured "
                f"({provenance_counts(f for f in sel if f.provenance != 'measured')}): the simulated "
                f"print is approximate until they are calibrated." if est else "")
        self.lbl_measured.setVisible(bool(est))
        grid = self._grid_problem()
        if grid:
            lines.append("layers     no slicer project yet: 0.08 mm assumed for this estimate only")
        self.lbl_est.setText("\n".join(lines))
        if self.result is None and not self.worker.busy:
            if grid:
                self._status(grid.split("\n")[0], theme.WARN)
            elif warn:
                self._status("Base is see-through: " + warn, theme.WARN)

    # ---- running -----------------------------------------------------------------------
    def _generate(self):
        v = self._validate()
        if not v:
            return
        sel, base = v
        a = self.config_ns()
        problems = plaque.check_args(a)
        if problems:
            QMessageBox.warning(self, APP, "\n".join(problems))
            return
        plaque.resolve_base_layers(a, base, log=lambda _m: None)
        self._refresh_fit()
        img, sig = self.fitted, self._signature()
        self._start("generate", lambda: dict(self._job_auto(sel, base, a, img), sig=sig))

    def _job_auto(self, sel, base, a, img):
        """The whole pipeline: rank the pool if it is bigger than the toolheads, then solve."""
        ranked, lo = None, 0.0
        if len(sel) > a.slots:
            res = plaque.rank_subsets(sel, base, a, img,
                                  progress=lambda f, m: self.worker.progress(f * 0.5, m), verbose=False)
            top = min(a.top, len(res))
            entries = plaque.render_candidates(
                res, base, a, img, top, progress=lambda f, m: self.worker.progress(0.5 + f * 0.2, m))
            fd, path = tempfile.mkstemp(prefix="stackforge_combos_", suffix=".png")
            os.close(fd)
            plaque.contact_sheet(path, entries, img)
            ranked = {"results": res, "sheet": path}
            sel = res[0]["fils"]
            lo = 0.7
        r = self._job_solve(sel, base, a, img, lo)
        r["ranked"] = ranked
        return r

    def _job_solve(self, sel, base, a, img, lo=0.0):
        r = plaque.solve(sel, base, a, img,
                         progress=lambda f, m: self.worker.progress(lo + (1 - lo) * f, m))
        r.update(args=a, image_path=self.image_path)
        return r

    def _start(self, kind, fn):
        if self.worker.busy or not self.worker.submit(kind, fn):
            return
        self.btn_go.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.prog.setValue(0)
        self._status("working…")

    def _finish(self):
        self.btn_go.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_export.setEnabled(self.result is not None)
        self.prog.setValue(0)

    def _poll(self):
        while True:
            try:
                job = self.worker.q.get_nowait()
            except queue.Empty:
                return
            if job.kind == "progress":
                frac, msg = job.result
                self.prog.setValue(int(max(0, min(100, frac * 100))))
                self._status(msg)
            elif job.kind == "cancelled":
                self._finish()
                self._status("cancelled")
            elif job.error:
                self._finish()
                self._status("failed: " + job.error.splitlines()[0], theme.ERR)
                show_error(self, APP, job)
            elif job.kind == "generate":
                self._on_solved(job.result)
            elif job.kind == "export":
                self._on_exported(job.result)

    def _on_solved(self, r):
        self.result = r
        self._finish()
        self.rank_results = r["ranked"]
        self._tab_titles(stale=False)
        self.view[3].set_image(Image.open(r["ranked"]["sheet"]) if r["ranked"] else None)
        self.view[1].set_image(r["achieved"])
        heat = np.clip(r["err"] / 20.0, 0, 1)
        self.view[2].set_image((np.stack([heat, 1 - heat, np.zeros_like(heat)], -1) * 255).astype(np.uint8))
        self.views.setCurrentIndex(1)
        self._update_legend()

        e, labels = r["err"], r["labels"]
        per_layer = np.mean([len(np.unique(labels[i])) for i in range(labels.shape[0])])
        notes = []
        if r["ranked"]:
            notes.append(f"best of {len(r['ranked']['results'])} combinations: "
                         + ", ".join(f.name for f in r["fils"]))
        thin = plaque.thin_fraction(labels, r["gamut"].base_index)
        if thin > 0.05 and r["args"].resolution < plaque.MIN_FEATURE_MM - 1e-9:
            notes.append(f"{100*thin:.0f}% of colour in 1-px features (slicer drops them)")
        if r["gamut"].capped:
            notes.append("gamut hit the state cap")
        warn = plaque.base_warning(r["base"], r["args"])
        if warn:
            notes.append("base is see-through")
        quality = plaque.match_quality(e.mean())
        self._status(f"Colour match: {quality}" + "".join("   ·   " + n for n in notes),
                     theme.OK if e.mean() < 10 and not warn else theme.WARN)
        self.lbl_status.setToolTip(
            f"Colour error (dE; under 5 is hard to see, over 20 is a colour these filaments "
            f"cannot make): mean {e.mean():.1f}, worst 5% {np.percentile(e, 95):.1f}, "
            f"max {e.max():.1f}, at arm's length {r['blurred'][0]:.1f}\n"
            f"{per_layer:.1f} filaments per layer on average" + (f"\n\n{warn}" if warn else ""))
        self._check_stale()     # inputs may have changed while it ran

    def _status(self, msg, colour=theme.FG_DIM):
        self.lbl_status.setText(msg)
        self.lbl_status.setStyleSheet(f"color: {colour}")

    # ---- export ------------------------------------------------------------------------
    def _export(self):
        if self.worker.busy:            # Ctrl+E bypasses the disabled button
            return
        if not self.result:
            QMessageBox.warning(self, APP, "Nothing to export yet: press Generate "
                                           "(it is out of date if you changed anything).")
            return
        problem = self._grid_problem()
        if problem:
            QMessageBox.warning(self, APP, problem)
            return
        r = self.result
        a = r["args"]
        warn = plaque.base_warning(r["base"], a)
        if warn and QMessageBox.question(
                self, APP, warn + "\n\nExport anyway?") != QMessageBox.Yes:
            return
        img = r["image_path"]
        name = os.path.splitext(os.path.basename(img or "plaque"))[0] + "_plaque.3mf"
        start = os.path.join(os.path.dirname(img), name) if img else name
        p, _ = QFileDialog.getSaveFileName(self, "Export 3MF", start, "3MF (*.3mf)")
        if not p:
            return
        if not p.lower().endswith(".3mf"):
            p += ".3mf"
        if self._template() and os.path.abspath(p) == os.path.abspath(self._template()):
            QMessageBox.warning(self, APP, "That is your slicer project file; choose another name "
                                           "so it is not overwritten.")
            return
        flavor, part, tpl = self._flavor(), self._part(), self._template()
        self._start("export", lambda: dict(
            plaque.export(p, r, a, flavor, part, tpl, progress=self.worker.progress), path=p))

    def _on_exported(self, info):
        self._finish()
        r, p = self.result, info["path"]
        size = os.path.getsize(p) / 1e6
        self._status(f"wrote {os.path.basename(p)}  ({size:.1f} MB, {info['nbox']} boxes)", theme.OK)
        notes = "".join(f"\n  · {n}" for n in info["notes"])
        QMessageBox.information(
            self, APP,
            f"Wrote {p}\n\n{info['total_h']:.2f} mm thick · {info['nbox']} boxes · {size:.1f} MB"
            + (f"\n\nChanged in the slicer settings:{notes}" if notes else "")
            + "\n\nLoad order:\n"
            + "\n".join(f"  T{i}  {f.label()}" for i, f in enumerate(r["fils"], 1))
            + "\n\n" + plaque.next_steps(r["fils"], r["args"]))

    def save_preview(self):
        if not self.result:
            QMessageBox.warning(self, APP, "Generate a preview first.")
            return
        p, _ = QFileDialog.getSaveFileName(self, "Save preview", "", "PNG (*.png)")
        if p:
            Image.fromarray(self.result["achieved"].astype(np.uint8)).save(p)
            self._status(f"saved {os.path.basename(p)}", theme.OK)

    # ---- presets -----------------------------------------------------------------------
    def _preset_values(self) -> dict:
        return {"width": self.s_width.value(), "height": self.s_height.value(), "res": self.s_res.value(),
                "maxl": self.s_maxl.value(), "basel": self.s_basel.value(), "fit": self.c_fit.currentText(),
                "dither": self.c_dither.currentText(), "grid": self.s_grid.value(),
                "slots": self.s_slots.value(), "top": self.s_top.value(),
                "rankby": self.c_rankby.currentText(), "samples": self.s_samples.value(),
                "optics": self.c_optics.currentText(), "illuminant": self.c_illum.currentText(),
                "filaments": sorted(f.id for f in self.selected()), "base": self._base_id()}

    def _refresh_presets(self, select=""):
        self.preset_combo.clear()
        self.preset_combo.addItems(self.presets.names("plaque"))
        if select:
            self.preset_combo.setCurrentText(select)

    def _save_preset(self):
        name, ok = QInputDialog.getText(self, APP, "Preset name:", text=self.preset_combo.currentText())
        if not ok or not name.strip():
            return
        try:
            self.presets.save("plaque", (), name, self._preset_values())
        except ValueError as e:
            QMessageBox.warning(self, APP, str(e))
            return
        self._refresh_presets(self.presets.clean(name))
        self._status(f"saved preset '{name.strip()}'", theme.OK)

    def _delete_preset(self):
        name = self.preset_combo.currentText()
        if name and QMessageBox.question(self, APP, f"Delete preset '{name}'?") == QMessageBox.Yes:
            self.presets.delete("plaque", (), name)
            self._refresh_presets()

    def _apply_preset(self):
        name = self.preset_combo.currentText()
        if not name:
            return
        p = self.presets.load("plaque", (), name)
        self._loading = True
        for key, widget in (("width", self.s_width), ("height", self.s_height), ("res", self.s_res),
                            ("maxl", self.s_maxl), ("basel", self.s_basel), ("grid", self.s_grid),
                            ("slots", self.s_slots), ("top", self.s_top), ("samples", self.s_samples)):
            if key in p:
                widget.setValue(p[key])
        for key, widget in (("fit", self.c_fit), ("dither", self.c_dither), ("rankby", self.c_rankby),
                            ("optics", self.c_optics), ("illuminant", self.c_illum)):
            if key in p:
                widget.setCurrentText(p[key])
        want = set(p.get("filaments", []))
        self._set_checked(want)
        notes = []
        if want - set(self.db.filaments):
            notes.append(f"{len(want - set(self.db.filaments))} filament(s) no longer in the database")
        if p.get("base") in self.db.filaments:
            self._set_base(p["base"])
        self._loading = False
        self._refresh_fit()
        self._refresh_estimate()
        self._status(f"loaded preset '{name}'" + (f" ({'; '.join(notes)})" if notes else ""),
                     theme.WARN if notes else theme.OK)

    def confirm_close(self):
        if self.worker.busy:
            self.worker.cancel()
        return True


class PlaqueArea(QTabWidget):
    """The designer plus an "All options" sub-tab (the generated plaque form).

    The designer is the default; the generated form reaches every flag it does not expose.
    """

    title = "Plaque"

    def __init__(self, project=None, presets=None, host=None, image_path=None):
        super().__init__()
        from stackforge.gui.argform.spec import introspect
        from stackforge.gui.panel import ToolPanel
        self.designer = PlaqueDesigner(project, presets, host, image_path)
        self.options = ToolPanel(introspect(plaque.build_parser(), "plaque"), "plaque",
                                 project=project, presets=presets)
        self.addTab(self.designer, "Designer")
        self.addTab(self.options, "All options")

    def shortcuts(self):
        return self.designer.shortcuts() if self.currentWidget() is self.designer else {}

    def on_show(self):
        self.designer.on_show()

    def set_db(self, path):
        self.designer.set_db(path)

    def confirm_close(self):
        return self.designer.confirm_close()
