"""Plaque designer: pick an image and filaments, see the simulated print, export a 3MF.

Everything the stackforge CLI exposes, plus what a GUI is better at: seeing the result next to
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

from tdforge.core import tdcolor
from tdforge.core.filamentdb import DB, DEFAULT_DB
from tdforge.gui import theme
from tdforge.gui.imageview import ImageView
from tdforge.gui.worker import Worker
from tdforge.tools import stackforge as sf

APP = "stackforge"
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
        self.s_res = _spin(0.1, 2.0, 0.05, sf.MIN_FEATURE_MM, 2)
        self.s_maxl = _spin(2, 40, 1, 16)
        self.s_basel = _spin(1, 20, 1, 5)
        g.addRow("Width (mm)", self.s_width)
        g.addRow("Height (mm)", self.s_height)
        g.addRow("", theme.hint("0 keeps the image's aspect ratio."))
        g.addRow("Resolution (mm)", self.s_res)
        g.addRow("", theme.hint("Match your nozzle width. Finer means far more geometry."))
        g.addRow("Colour layers", self.s_maxl)
        g.addRow("", theme.hint("The gamut stops growing once the deepest stack goes opaque, so past "
                                "~16 this is just print time."))
        g.addRow("Base layers", self.s_basel)

        c = section("Colour")
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
        for wdg in (self.c_dither, self.c_rankby):
            wdg.currentTextChanged.connect(lambda *_: self._inputs_changed())
        return scroll

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
        lh, fl = self.project.layers() if self.project else (None, None)
        lh = lh or 0.08
        return lh, fl or lh

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
        prev = self._checked()
        prefer = ("white", "black", "blue", "red")
        self._loading = True
        self.list.clear()
        rows = sorted(self.db.filaments.values(), key=lambda f: (f.brand, f.series, f.name))
        for fil in rows:
            measured = fil.provenance == "measured"
            it = QListWidgetItem(_swatch(fil.rgb()),
                                 f"{fil.name}      td {fil.td:.2f}" + ("" if measured else "   est"))
            it.setData(Qt.UserRole, fil.id)
            it.setToolTip(fil.label())
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            on = fil.id in prev if prev else any(p == fil.name.lower() for p in prefer)
            it.setCheckState(Qt.Checked if on else Qt.Unchecked)
            if not measured:
                it.setForeground(QColor(theme.FG_DIM))
            self.list.addItem(it)
        names = [f.id for f in self.db.filaments.values()]
        self.base.blockSignals(True)
        cur = self.base.currentText()
        self.base.clear()
        self.base.addItems(names)
        if cur in names:
            self.base.setCurrentText(cur)
        elif names:
            self.base.setCurrentText(next((n for n in names if "white" in n), names[0]))
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
            if not self.list.item(i).isHidden():
                self.list.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)
        self._loading = False
        self._inputs_changed()

    def _select_measured(self):
        self._loading = True
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setCheckState(Qt.Checked if self.db.filaments[it.data(Qt.UserRole)].provenance == "measured"
                             else Qt.Unchecked)
        self._loading = False
        self._inputs_changed()

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
            base_layers=self.s_basel.value(), resolution=self.s_res.value(),
            width=self.s_width.value(), height=self.s_height.value(), grid=self.s_grid.value(),
            cap=400_000, dither=self.c_dither.currentText(), fit=self.c_fit.currentText(),
            slots=self.s_slots.value(), top=self.s_top.value(), rank_by=self.c_rankby.currentText(),
            rank_samples=self.s_samples.value())

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
        base_id = self.base.currentText()
        if base_id not in self.db.filaments:
            QMessageBox.warning(self, APP, "Choose a base filament.")
            return None
        base = self.db.filaments[base_id]
        if base.id not in {f.id for f in sel}:
            self._set_checked(self._checked() | {base.id})
        sel = [base] + [f for f in sel if f.id != base.id]
        return sel, base

    # ---- image, estimate, staleness ----------------------------------------------------
    def _open_image(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open image", "", IMAGE_FILTER)
        if p:
            self._load_image(p)

    def _load_image(self, path):
        try:
            self.source_img = tdcolor.open_image(path)
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
        w_px = max(1, int(round(a.width / a.resolution)))
        h_px = (max(1, int(round(a.height / a.resolution))) if a.height > 0
                else max(1, int(round(w_px * self.source_img.height / self.source_img.width))))
        base = self.db.filaments.get(self.base.currentText())
        pad = tuple(int(v) for v in base.rgb()) if base else (255, 255, 255)
        self.fitted = tdcolor.fit_image(self.image_path, w_px, h_px, a.fit, pad=pad)
        self.view[0].set_image(self.fitted)
        self.views.setCurrentIndex(0)
        self._update_legend()

    def _update_legend(self, *_):
        self.legend.setText(LEGEND[self.views.currentIndex()])

    def _signature(self):
        pool = tuple(sorted(repr(f) for f in self.selected()))
        return (tuple(sorted(vars(self.config_ns()).items())), self.image_path, pool,
                self.base.currentText())

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
        problems = sf.check_args(a)
        if problems:
            self.lbl_est.setText("\n".join(problems))
            return
        sel = self.selected()
        base_id = self.base.currentText()
        if base_id in self.db.filaments and base_id not in {f.id for f in sel}:
            sel = sel + [self.db.filaments[base_id]]      # _validate adds the base if unticked
        n, slots = len(sel), a.slots
        total = a.first_layer_height + (a.base_layers - 1 + a.max_layers) * a.layer_height
        w_px = max(1, int(round(a.width / a.resolution)))
        if self.source_img is not None:
            h_px = (max(1, int(round(a.height / a.resolution))) if a.height > 0
                    else max(1, int(round(w_px * self.source_img.height / self.source_img.width))))
        else:
            h_px = w_px
        combos = comb(max(0, n - 1), slots - 1) if n > slots else 0
        lines = [f"grid       {w_px} x {h_px} px",
                 f"plaque     {w_px*a.resolution:.0f} x {h_px*a.resolution:.0f} x {total:.2f} mm",
                 f"layers     {a.base_layers} base + {a.max_layers} colour "
                 f"at {a.layer_height:g} / {a.first_layer_height:g} mm",
                 f"selected   {n} filament{'s' if n != 1 else ''}"]
        if combos:
            lines.append(f"ranking    {combos} combinations")
        base = self.db.filaments.get(base_id)
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
        if self._layers() == (0.08, 0.08) and not (self.project and self.project.layers()[0]):
            lines.append("layers     no template: 0.08 mm assumed (set a template above)")
        self.lbl_est.setText("\n".join(lines))

    # ---- running -----------------------------------------------------------------------
    def _generate(self):
        v = self._validate()
        if not v:
            return
        sel, base = v
        a = self.config_ns()
        problems = sf.check_args(a)
        if problems:
            QMessageBox.warning(self, APP, "\n".join(problems))
            return
        self._refresh_fit()
        img, sig = self.fitted, self._signature()
        self._start("generate", lambda: dict(self._job_auto(sel, base, a, img), sig=sig))

    def _job_auto(self, sel, base, a, img):
        """The whole pipeline: rank the pool if it is bigger than the toolheads, then solve."""
        ranked, lo = None, 0.0
        if len(sel) > a.slots:
            res = sf.rank_subsets(sel, base, a, img,
                                  progress=lambda f, m: self.worker.progress(f * 0.5, m), verbose=False)
            top = min(a.top, len(res))
            entries = sf.render_candidates(
                res, base, a, img, top, progress=lambda f, m: self.worker.progress(0.5 + f * 0.2, m))
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
        g = sf.Gamut(sel, base, a.layer_height, a.max_layers, a.grid, a.cap, verbose=False, progress=prog)
        prog(0.7, "matching pixels to reachable colours")
        state = sf.solve_image(g, img, a.dither)
        achieved = np.round(g.srgb()[state])
        err = np.linalg.norm(tdcolor.srgb_to_lab(achieved.astype(np.float64))
                             - tdcolor.srgb_to_lab(img.astype(np.float64)), axis=-1)
        prog(0.95, "building layer labels")
        blurred = tdcolor.blurred_de(achieved, img, sf.BLUR_MM / a.resolution)[0]
        labels, _ = sf.trim_base_layers(sf.layer_labels(g, state), g.base_index)
        return {"gamut": g, "labels": labels, "achieved": achieved, "err": err, "blurred": blurred,
                "fils": sel, "base": base, "args": a, "image_path": self.image_path}

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
                self._status("failed", theme.ERR)
                QMessageBox.critical(self, APP, job.error)
            elif job.kind == "generate":
                self._on_solved(job.result)

    def _on_solved(self, r):
        self._finish()
        self.result = r
        self.rank_results = r["ranked"]
        self._tab_titles(stale=False)
        self.view[3].set_image(Image.open(r["ranked"]["sheet"]) if r["ranked"] else None)
        self.view[1].set_image(r["achieved"])
        heat = np.clip(r["err"] / 20.0, 0, 1)
        self.view[2].set_image((np.stack([heat, 1 - heat, np.zeros_like(heat)], -1) * 255).astype(np.uint8))
        self.views.setCurrentIndex(1)
        self._update_legend()
        self.btn_export.setEnabled(True)

        e, labels = r["err"], r["labels"]
        per_layer = np.mean([len(np.unique(labels[i])) for i in range(labels.shape[0])])
        capped = "   ·   gamut hit the state cap" if r["gamut"].capped else ""
        thin = sf.thin_fraction(labels, r["gamut"].base_index)
        if thin > 0.05 and r["args"].resolution < sf.MIN_FEATURE_MM - 1e-9:
            capped += f"   ·   {100*thin:.0f}% of colour in 1-px features (slicer drops them)"
        if r["ranked"]:
            capped = (f"   ·   best of {len(r['ranked']['results'])} combinations: "
                      + ", ".join(f.name for f in r["fils"]) + capped)
        self._status(f"dE mean {e.mean():.1f}  p95 {np.percentile(e,95):.1f}  max {e.max():.1f}  "
                     f"(blurred {r['blurred']:.1f})   ·   {per_layer:.1f} filaments per layer{capped}",
                     theme.OK if e.mean() < 8 else theme.WARN)
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
        r = self.result
        start = os.path.splitext(os.path.basename(r["image_path"] or "plaque"))[0] + "_plaque.3mf"
        p, _ = QFileDialog.getSaveFileName(self, "Export 3MF", start, "3MF (*.3mf)")
        if not p:
            return
        if not p.lower().endswith(".3mf"):
            p += ".3mf"
        a = r["args"]
        try:
            plate, decals, total_h = sf.build_geometry(
                r["labels"], a.width, a.resolution, a.layer_height,
                a.first_layer_height + (a.base_layers - 1) * a.layer_height,
                r["gamut"].base_index, len(r["fils"]))
            sf.write_plaque(p, self._flavor(), plate, decals, r["fils"], r["gamut"].base_index,
                            self._part(), self._template(), a.layer_height, a.first_layer_height)
        except (Exception, SystemExit) as exc:
            QMessageBox.critical(self, APP, f"Export failed:\n{exc}")
            return
        nbox = sum(len(t) // 12 for _, _, t in decals)
        size = os.path.getsize(p) / 1e6
        self._status(f"wrote {os.path.basename(p)}  ({size:.1f} MB, {nbox} boxes)", theme.OK)
        QMessageBox.information(
            self, APP,
            f"Wrote {p}\n\n{total_h:.2f} mm thick · {nbox} boxes · {size:.1f} MB\n\nLoad order:\n"
            + "\n".join(f"  T{i}  {f.label()}" for i, f in enumerate(r["fils"], 1))
            + f"\n\nSlice at layer height EXACTLY {a.layer_height} mm with a {a.first_layer_height} mm "
              f"first layer, or the modifiers land between layers and colours drop out.")

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
                "filaments": sorted(f.id for f in self.selected()), "base": self.base.currentText()}

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
        for key, widget in (("fit", self.c_fit), ("dither", self.c_dither), ("rankby", self.c_rankby)):
            if key in p:
                widget.setCurrentText(p[key])
        want = set(p.get("filaments", []))
        self._set_checked(want)
        notes = []
        if want - set(self.db.filaments):
            notes.append(f"{len(want - set(self.db.filaments))} filament(s) no longer in the database")
        if p.get("base") in self.db.filaments:
            self.base.setCurrentText(p["base"])
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
    """The designer plus an "All options" sub-tab (the generated stackforge form).

    The designer is the default; the generated form reaches every flag it does not expose.
    """

    title = "Plaque"

    def __init__(self, project=None, presets=None, host=None, image_path=None):
        super().__init__()
        from tdforge.gui.argform.spec import introspect
        from tdforge.gui.panel import ToolPanel
        self.designer = PlaqueDesigner(project, presets, host, image_path)
        self.options = ToolPanel(introspect(sf.build_parser(), "stackforge"), "stackforge",
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
