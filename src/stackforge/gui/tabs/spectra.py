"""Spectra tab: what each layer of a filament does to every wavelength.

Pick spectrally calibrated filaments on the left (several overlay), choose what they sit on, and
drag the layer slider: the chart shows the reflectance spectrum of that many layers (faint
ghosts for every other count), the strip under it the colour of each count. The stack builder
composes an arbitrary stack -- "2 orange over 3 blue over white" -- and shows why it comes out
the colour it does. The viewing-light switch re-renders everything under D65, D50 or A (a
metamerism check: two stacks that match in daylight can part under a lamp).

Only filaments with a spectral calibration (`Filament.spectral`, from `stackforge-spectral fit`)
can be shown. "Load demo spectra" swaps in clearly labelled SYNTHETIC filaments to explore with
before any are measured; they are never written to the library. The "All options" sub-tab holds
the stackforge-spectral commands (fit, show, demo-readings).
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFileDialog, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QSlider, QSpinBox, QSplitter, QTabWidget,
                               QVBoxLayout, QWidget)

from stackforge.core import colormath, wedgesheet
from stackforge.core import spectral as sp
from stackforge.core.filamentdb import DB, DEFAULT_DB
from stackforge.gui import theme
from stackforge.gui.pickers import swatch_icon
from stackforge.gui.spectrum_chart import Axis, Series, SpectrumChart, SwatchStrip, visible
from stackforge.gui.tabs.common import ToolTabs
from stackforge.tools import spectral as spectral_tool

APP = "stackforge"
VIEWS = [("reflectance", "Reflectance: layers over the base"),
         ("transmittance", "Transmittance: light through the layers"),
         ("ks", "K/S: absorption over scattering (per filament)")]
IDEAL = {"white": np.full(sp.NB, 0.9), "black": np.full(sp.NB, 0.03)}
WATERMARK = "SYNTHETIC — not measurements"


class StackRow(QWidget):
    """One group in the stack builder: n layers of a filament."""

    def __init__(self, owner, fils, fid=None, n=2):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        for f in fils:
            self.combo.addItem(swatch_icon(sp.colour_of(f)), f.name, f.id)
        if fid is not None and self.combo.findData(fid) >= 0:
            self.combo.setCurrentIndex(self.combo.findData(fid))
        self.count = QSpinBox()
        self.count.setRange(1, 40)
        self.count.setValue(n)
        self.count.setSuffix(" layers")
        rm = QPushButton("−")
        rm.setFixedWidth(28)
        rm.setToolTip("remove these layers")
        rm.clicked.connect(lambda: owner.remove_row(self))
        lay.addWidget(self.combo, 1)
        lay.addWidget(self.count)
        lay.addWidget(rm)
        self.combo.currentIndexChanged.connect(lambda *_: owner.refresh())
        self.count.valueChanged.connect(lambda *_: owner.refresh())


class SpectraViewer(QWidget):
    def __init__(self, project=None):
        super().__init__()
        self.project = project
        self.db = None
        self.demo = False
        self.readings = None           # (path, data, scale, layer height of the wedges)
        self.pool: dict = {}
        self.rows: list[StackRow] = []
        self._timer = QTimer(self)
        self._timer.setInterval(380)
        self._timer.timeout.connect(self._tick)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_left())
        split.addWidget(self._build_center())
        split.setStretchFactor(1, 1)
        split.setSizes([300, 1000])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(split)
        self.reload()

    # ---- layout -------------------------------------------------------------------------
    def _build_left(self):
        w = QWidget()
        col = QVBoxLayout(w)
        col.setContentsMargins(0, 0, 6, 0)
        head = QLabel("Filaments")
        head.setStyleSheet("font-weight: bold")
        col.addWidget(head)
        col.addWidget(theme.hint("Tick several to overlay them. Only spectrally calibrated filaments "
                                 "can be shown; the rest are listed greyed out."))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter…")
        self.filter.textChanged.connect(lambda *_: self._apply_filter())
        col.addWidget(self.filter)
        self.list = QListWidget()
        self.list.itemChanged.connect(lambda *_: self.refresh())
        col.addWidget(self.list, 1)
        self.btn_demo = QPushButton("Load demo spectra")
        self.btn_demo.setToolTip("Swap in SYNTHETIC filaments to explore with. Never saved.")
        self.btn_demo.clicked.connect(self.toggle_demo)
        col.addWidget(self.btn_demo)
        col.addWidget(QLabel("On top of (base)"))
        self.base = QComboBox()
        self.base.currentIndexChanged.connect(lambda *_: self.refresh())
        col.addWidget(self.base)
        rd = QHBoxLayout()
        self.btn_readings = QPushButton("Load readings…")
        self.btn_readings.setToolTip("A *.readings.json from Measure: dots the measured wedge steps "
                                     "over the model, for the ticked filaments it contains")
        self.btn_readings.clicked.connect(self._load_readings)
        self.btn_clear_readings = QPushButton("Clear")
        self.btn_clear_readings.clicked.connect(self._clear_readings)
        rd.addWidget(self.btn_readings, 1)
        rd.addWidget(self.btn_clear_readings)
        col.addLayout(rd)
        self.lbl_readings = theme.hint("")
        col.addWidget(self.lbl_readings)
        return w

    def _build_center(self):
        w = QWidget()
        col = QVBoxLayout(w)
        col.setContentsMargins(6, 0, 0, 0)
        ctl = QHBoxLayout()
        self.view = QComboBox()
        for key, text in VIEWS:
            self.view.addItem(text, key)
        self.view.currentIndexChanged.connect(lambda *_: self.refresh())
        self.illum = QComboBox()
        self.illum.addItems(list(sp.ILLUMINANTS))
        self.illum.setToolTip("The light the colours are rendered under (eye adapted to it)")
        self.illum.currentIndexChanged.connect(lambda *_: self.refresh())
        self.lh = QDoubleSpinBox()
        self.lh.setRange(0.02, 0.5)
        self.lh.setSingleStep(0.01)
        self.lh.setDecimals(2)
        self.lh.setSuffix(" mm")
        self.lh.setValue(self._project_layer())
        self.lh.valueChanged.connect(lambda *_: self.refresh())
        self.maxl = QSpinBox()
        self.maxl.setRange(1, 40)
        self.maxl.setValue(16)
        self.maxl.valueChanged.connect(self._max_changed)
        ctl.addWidget(self.view, 1)
        for lbl, wdg in (("Light", self.illum), ("Layer", self.lh), ("Up to", self.maxl)):
            ctl.addSpacing(8)
            ctl.addWidget(QLabel(lbl))
            ctl.addWidget(wdg)
        self.btn_png = QPushButton("Export PNG…")
        self.btn_png.clicked.connect(self._export)
        ctl.addSpacing(8)
        ctl.addWidget(self.btn_png)
        col.addLayout(ctl)

        sl = QHBoxLayout()
        self.btn_play = QPushButton("▶ Play")
        self.btn_play.setCheckable(True)
        self.btn_play.toggled.connect(self._play)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, self.maxl.value())
        self.slider.setValue(3)
        self.slider.setTickPosition(QSlider.TicksBelow)
        self.slider.valueChanged.connect(self._layer_changed)
        self.lbl_layer = QLabel()
        self.lbl_layer.setMinimumWidth(150)
        sl.addWidget(self.btn_play)
        sl.addWidget(self.slider, 1)
        sl.addWidget(self.lbl_layer)
        col.addLayout(sl)

        self.capture = QWidget()             # what Export PNG saves
        cap = QVBoxLayout(self.capture)
        cap.setContentsMargins(0, 0, 0, 0)
        cap.setSpacing(2)
        self.chart = SpectrumChart()
        self.strip = SwatchStrip()
        self.strip.picked.connect(lambda i: self.slider.setValue(min(i, self.slider.maximum())))
        cap.addWidget(self.chart, 1)
        cap.addWidget(self.strip)
        col.addWidget(self.capture, 1)
        self.info = QLabel()
        theme.mono(self.info)
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.RichText)
        col.addWidget(self.info)
        col.addWidget(self._build_stack())
        return w

    def _build_stack(self):
        g = QGroupBox("Stack builder")
        g.setCheckable(True)
        g.setChecked(False)
        g.toggled.connect(lambda *_: self.refresh())
        self.stack_box = g
        v = QVBoxLayout(g)
        v.addWidget(theme.hint("Top of the stack first; the last group sits on the base above. Drawn "
                               "dashed in the chart, with its colour under every light."))
        self.rows_lay = QVBoxLayout()
        v.addLayout(self.rows_lay)
        h = QHBoxLayout()
        add = QPushButton("+ Add layers")
        add.clicked.connect(lambda: self.add_row())
        h.addWidget(add)
        h.addStretch(1)
        self.lbl_stack = QLabel()
        self.lbl_stack.setTextFormat(Qt.RichText)
        h.addWidget(self.lbl_stack)
        v.addLayout(h)
        return g

    # ---- data ---------------------------------------------------------------------------
    def _project_layer(self) -> float:
        try:
            lh = float(self.project.get("layer_height")) if self.project else 0.0
        except (TypeError, ValueError):
            lh = 0.0
        return lh if lh > 0 else 0.08

    def reload(self, path=None):
        if not self.demo:
            db_path = path or (self.project.get("db") if self.project else None) or DEFAULT_DB
            try:
                self.db = DB(db_path)
            except SystemExit as exc:
                QMessageBox.critical(self, APP, str(exc))
                self.db = None
        fils = sp.demo_filaments() if self.demo else \
            (list(self.db.filaments.values()) if self.db else [])
        self.pool = {f.id: f for f in fils}
        prev = self.checked()
        cal = sorted((f for f in fils if sp.is_calibrated(f)), key=lambda f: f.label())
        other = sorted((f for f in fils if not sp.is_calibrated(f)), key=lambda f: f.label())
        self.list.blockSignals(True)
        self.list.clear()
        for f in cal + other:
            ok = f in cal
            it = QListWidgetItem(swatch_icon(sp.colour_of(f) if ok else f.color),
                                 f.name if ok else f"{f.name}  (no spectral calibration)")
            it.setData(Qt.UserRole, f.id)
            it.setFlags((it.flags() | Qt.ItemIsUserCheckable) if ok else
                        (it.flags() & ~Qt.ItemIsEnabled))
            if ok:
                sc = f.spectral
                it.setToolTip(f"{f.label()}\nK/S {sc.get('source') or ''} {sc.get('measured_at') or ''}".strip())
            else:
                it.setToolTip(f"{f.label()}\nno spectral calibration: print a wedge set over white and "
                              f"black, measure it, then stackforge-spectral fit")
            on = f.id in prev if prev else (self.demo and f.id in ("demo-orange", "demo-blue"))
            it.setCheckState(Qt.Checked if (ok and on) else Qt.Unchecked)
            if not ok:
                it.setForeground(QColor(theme.FG_DIM))
            self.list.addItem(it)
        self.list.blockSignals(False)

        cur = self.base.currentData()
        self.base.blockSignals(True)
        self.base.clear()
        self.base.addItem("ideal white (R = 0.9)", "white")
        self.base.addItem("ideal black (R = 0.03)", "black")
        for f in cal:
            self.base.addItem(swatch_icon(sp.colour_of(f)), f.label(), f.id)
        want = cur if cur is not None and self.base.findData(cur) >= 0 else \
            ("demo-white" if self.demo else "white")
        self.base.setCurrentIndex(max(0, self.base.findData(want)))
        self.base.blockSignals(False)

        # Rebuild the stack builder against the new pool, keeping the stack being worked on.
        keep = [(r.combo.currentData(), r.count.value()) for r in self.rows]
        for r in list(self.rows):
            self.remove_row(r, refresh=False)
        ids = {f.id for f in cal}
        keep = [(fid, n) for fid, n in keep if fid in ids]
        if not keep and cal:
            pick = [f.id for f in cal]
            keep = [(next((i for i in pick if "orange" in i), pick[0]), 2),
                    (next((i for i in pick if "blue" in i), pick[-1]), 3)]
        for fid, n in keep:
            self.add_row(fid, n, refresh=False)
        self.btn_demo.setText("Back to my library" if self.demo else "Load demo spectra")
        self._apply_filter()
        self.refresh()

    def toggle_demo(self):
        self.demo = not self.demo
        self.reload()

    def checked(self) -> list:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]

    def calibrated(self) -> list:
        return [f for f in self.pool.values() if sp.is_calibrated(f)]

    def _apply_filter(self):
        needle = self.filter.text().strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            f = self.pool.get(it.data(Qt.UserRole))
            it.setHidden(bool(needle) and f is not None and needle not in f.label().lower())

    def base_spectrum(self):
        key = self.base.currentData()
        if key in IDEAL:
            return IDEAL[key], self.base.currentText()
        f = self.pool.get(key)
        if f is None or not sp.is_calibrated(f):
            return IDEAL["white"], "ideal white"
        return sp.r_inf(*sp.ks(f)), f.label()

    # ---- stack builder ------------------------------------------------------------------
    def add_row(self, fid=None, n=2, refresh=True):
        cal = self.calibrated()
        if not cal:
            return
        row = StackRow(self, cal, fid, n)
        self.rows.append(row)
        self.rows_lay.addWidget(row)
        if refresh:
            self.refresh()

    def remove_row(self, row, refresh=True):
        if row in self.rows:
            self.rows.remove(row)
            row.setParent(None)
            row.deleteLater()
        if refresh:
            self.refresh()

    def built_stack(self):
        """[(fil, n)] bottom-up from the builder rows (listed top first)."""
        out = []
        for r in reversed(self.rows):
            f = self.pool.get(r.combo.currentData())
            if f is not None and sp.is_calibrated(f):
                out.append((f, r.count.value()))
        return out

    # ---- interaction --------------------------------------------------------------------
    def _max_changed(self, v):
        self.slider.setRange(0, v)
        self.refresh()

    def _layer_changed(self, *_):
        self.refresh()

    def _play(self, on):
        self.btn_play.setText("❚❚ Pause" if on else "▶ Play")
        if on:
            if self.slider.value() >= self.slider.maximum():
                self.slider.setValue(0)
            self._timer.start()
        else:
            self._timer.stop()

    def _tick(self):
        v = self.slider.value() + 1
        if v > self.slider.maximum():
            self.btn_play.setChecked(False)
            return
        self.slider.setValue(v)

    def _load_readings(self):
        path, _ = QFileDialog.getOpenFileName(self, "Wedge readings", "", "Readings (*.readings.json *.json)")
        if path:
            self.load_readings(path)

    def load_readings(self, path):
        try:
            data = wedgesheet.load_readings(path)
            every = [r for s in data["strips"] for r in s.get("readings", [])] + \
                    [s["base_reading"] for s in data["strips"] if s.get("base_reading")]
            scale = sp.spectrum_scale(every)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, APP, f"{path}: {exc}")
            return False
        try:
            wedge_lh = float(data.get("layer_height") or 0)
        except (TypeError, ValueError):
            wedge_lh = 0.0
        self.readings = (path, data, scale, wedge_lh)
        ids = sorted({s["filament"]["id"] for s in data["strips"]})
        self.lbl_readings.setText(f"readings: {len(data['strips'])} wedge(s) of {', '.join(ids)}")
        self.refresh()
        return True

    def _clear_readings(self):
        self.readings = None
        self.lbl_readings.setText("")
        self.refresh()

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export chart", "spectra.png", "PNG (*.png)")
        if path:
            self.export_png(path)

    def export_png(self, path) -> bool:
        return self.capture.grab().save(path)

    # ---- rendering ----------------------------------------------------------------------
    def refresh(self):
        if not hasattr(self, "strip"):
            return
        illum = self.illum.currentText()
        view = self.view.currentData()
        lh, N, layer = self.lh.value(), self.maxl.value(), self.slider.value()
        Rb, bname = self.base_spectrum()
        fils = [self.pool[i] for i in self.checked() if i in self.pool]
        series, rows, info = [], [], []

        for f in fils:
            K, S = sp.ks(f)
            col = visible(sp.colour_of(f, illum))
            R = sp.stack_spectra(f, Rb, N, lh)
            rows.append((f.name, [sp.spectrum_to_hex(r, illum) for r in R]))
            if view == "reflectance":
                curves = list(R)
            elif view == "transmittance":
                curves = [np.ones(sp.NB)] + [sp.transmittance(K, S, n * lh) for n in range(1, N + 1)]
            else:
                curves = [K / S]
            series.append(Series(f.name, col, curves, ghosts=view != "ks"))
            info.append(self._info_line(f, R[min(layer, N)]))
            if self.readings and view == "reflectance":
                series += self._measured(f, layer, col)

        if self.stack_box.isChecked():
            st = self.built_stack()
            if st:
                Rs = sp.stack(Rb, [(*sp.ks(f), n * lh) for f, n in st])
                desc = " over ".join(f"{n}×{f.name}" for f, n in reversed(st))
                series.append(Series(f"stack: {desc}", QColor(theme.FG), [Rs], dashed=True, ghosts=False))
                cells = "".join(
                    f"<span style='background:{sp.spectrum_to_hex(Rs, il)};'>&nbsp;&nbsp;&nbsp;&nbsp;"
                    f"&nbsp;&nbsp;</span> {il} {sp.spectrum_to_hex(Rs, il)}&nbsp;&nbsp; " for il in sp.ILLUMINANTS)
                self.lbl_stack.setText(cells + f"<span style='color:{theme.FG_DIM}'>"
                                       f"shift under A: ΔE {self._shift(Rs):.1f}</span>")
            else:
                self.lbl_stack.setText("")

        axis = {"reflectance": Axis("reflectance"), "transmittance": Axis("transmittance"),
                "ks": Axis("K/S (log)", -3, 3, log=True)}[view]
        if view == "ks":
            title = f"K/S of {len(fils)} filament(s): high = the band is absorbed, low = sent back"
        elif view == "transmittance":
            title = f"{layer} layer(s), {layer * lh:.2f} mm: light passing straight through"
        else:
            title = f"{layer} layer(s), {layer * lh:.2f} mm, over {bname}, seen under {illum}"
        self.chart.empty = ("No spectrally calibrated filaments in this library yet.\n\n"
                            "Print a wedge set over a white and a black base, measure it with "
                            "spectra (Measure tab), then run stackforge-spectral fit "
                            "(All options) -- or press “Load demo spectra” to explore."
                            if not self.calibrated() else "Tick one or more filaments on the left.")
        self.chart.set_data(series, layer, axis, title, WATERMARK if self.demo else "")
        self.strip.set_rows(rows, layer)
        self.lbl_layer.setText(f"{layer} layers  ·  {layer * lh:.2f} mm")
        self.info.setText("<br>".join(info))

    def _info_line(self, f, R):
        illum = self.illum.currentText()
        hx = sp.spectrum_to_hex(R, illum)
        return (f"<span style='background:{hx};'>&nbsp;&nbsp;&nbsp;&nbsp;</span> "
                f"{f.name:12} {hx}  ·  shift under A: ΔE {self._shift(R):.1f}")

    @staticmethod
    def _shift(R) -> float:
        """dE between a stack in daylight and under a lamp, each with the eye adapted."""
        a = colormath.linear_to_lab(np.clip(sp.spectrum_to_linear(R, "D65"), 0, 1))
        b = colormath.linear_to_lab(np.clip(sp.spectrum_to_linear(R, "A"), 0, 1))
        return float(np.linalg.norm(a - b))

    def _measured(self, f, layer, col):
        """Measured wedge steps of `f` at this layer count, with the model over the same base.

        The model is drawn at the wedge's own layer height, not the Layer box's: step n of the
        wedge is n of *its* layers."""
        _, data, scale, wedge_lh = self.readings
        lh = wedge_lh or self.lh.value()
        out = []
        for s in data["strips"]:
            if s["filament"]["id"] != f.id or not s.get("base_reading"):
                continue
            if not (1 <= layer <= len(s.get("readings", []))):
                continue
            try:
                Rb = sp.reading_spectrum(s["base_reading"], scale)
                M = sp.reading_spectrum(s["readings"][layer - 1], scale)
            except sp.SpectralError:
                continue
            K, S = sp.ks(f)
            model = sp.layer(Rb, K, S, layer * lh)
            c = QColor(col)
            c.setAlpha(200)
            out.append(Series(f"{f.name} measured over {s['base']['label']}", c, [model],
                              points=M, dashed=True, ghosts=False))
        return out


class SpectraTab(QWidget):
    title = "Spectra"

    def __init__(self, project=None, presets=None, host=None):
        super().__init__()
        self.host = host
        self.viewer = SpectraViewer(project)
        self.tools = ToolTabs.for_tool("spectral", spectral_tool.build_parser, project=project,
                                       presets=presets, on_done=self._done)
        self.nb = QTabWidget()
        self.nb.addTab(self.viewer, "Viewer")
        self.nb.addTab(self.tools, "All options")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.nb)

    def on_show(self):
        self.viewer.reload()            # a fit in another tab may have added a calibration

    def reload_db(self, path=None):
        self.viewer.reload(path)

    def _done(self, panel, job):
        if job.returncode == 0 and panel.command == ("fit",) and panel.form.values().get("write"):
            self.viewer.reload()
            fil = self.host.tabs.get("Filaments") if self.host else None
            if fil is not None:
                fil.reload_db()

