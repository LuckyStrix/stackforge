"""Calibrate page: write a step wedge, enter what you measured, fit td and colour."""
from __future__ import annotations

import os
import re
from datetime import date

import numpy as np
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                               QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from stackforge.core import optics, threemf, colormath
from stackforge.gui import theme
from stackforge.gui.filaments import APP
from stackforge.gui.filaments.common import report_box, scroll_page, section, show_lines, spin
from stackforge.gui.widgets import IMAGE_FILTER
from stackforge.tools import calibrate


def blocked(span, nsets) -> bool:
    """A single low-contrast wedge cannot separate colour from opacity."""
    return nsets < 2 and span < 25


def fit_lines(fil, datasets, lh, per_channel, fit):
    """The fit report as (text, kind) lines. `fit` is calibrate.fit_td's result."""
    td, col, des, preds, (span, nsets) = fit
    out = []
    for (meas, base_rgb), pred, de in zip(datasets, preds, des):
        out.append((f"{fil.label()}  over base {colormath.to_hex(base_rgb)} at {lh} mm layers", "head"))
        out.append((" step  layers   measured      model     dE", "dim"))
        for i, (m, p, d) in enumerate(zip(meas, pred, de)):
            out.append((f" {i + 1:4d}  {i + 1:6d}   {colormath.to_hex(m):>8}   "
                        f"{colormath.to_hex(colormath.linear_to_srgb(p)):>8}  {d:5.1f}",
                        "warn" if d > 5 else ""))
        out.append(("", ""))
    all_de = np.concatenate(des)
    out += [(f"  td      = {np.round(td, 4).tolist()}", ""),
            (f"  colour  = {colormath.to_hex(col)}   (was {fil.color})", ""),
            (f"  fit dE  : mean {all_de.mean():.1f}, max {all_de.max():.1f}",
             "ok" if all_de.mean() <= 5 else "warn")]
    if per_channel and min(len(m) for m, _ in datasets) < 6:
        out += [("", ""), ("  ! fewer than 6 steps for a 6-parameter per-channel fit; treat this "
                           "with suspicion.", "warn")]
    if all_de.mean() > 5:
        out += [("", ""), ("  ! poor fit. Check lighting and white balance, confirm the wedge "
                           "sliced at the stated layer height, and that the base colour is right.",
                           "warn")]
    if blocked(span, nsets):
        out += [("", ""),
                (f"  !! This wedge only spans dE {span:.0f} end to end.", "err"),
                ("     td and colour are under-determined at that contrast — the fit above", "err"),
                ("     may look tight and still be badly wrong. Print a second wedge over a", "err"),
                ("     CONTRASTING base and fill in Wedge B.", "err"), ("", ""),
                ("     Refusing to apply a fit from a single low-contrast wedge.", "err")]
    return out


class WedgeInputs(QWidget):
    """One wedge's base colour and measured patch list."""

    def __init__(self, page, title, default_base):
        super().__init__()
        self.page = page
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 4, 0, 4)
        head = QLabel(title)
        head.setStyleSheet("font-weight: bold")
        col.addWidget(head)
        top = QHBoxLayout()
        top.addWidget(QLabel("Base"))
        self.base = QComboBox()
        self.base.setEditable(True)
        self.base.setMinimumWidth(200)
        self.base.setCurrentText(default_base)
        top.addWidget(self.base)
        top.addWidget(theme.hint("hex, or a filament id"), 1)
        col.addLayout(top)
        self.text = QPlainTextEdit()
        self.text.setFixedHeight(64)
        theme.mono(self.text)
        col.addWidget(self.text)
        btns = QHBoxLayout()
        photo = QPushButton("From photo…")
        photo.clicked.connect(self._from_photo)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.text.clear)
        btns.addWidget(photo)
        btns.addWidget(clear)
        btns.addStretch(1)
        col.addLayout(btns)

    def set_bases(self, ids):
        keep = self.base.currentText()
        self.base.blockSignals(True)
        self.base.clear()
        self.base.addItems(ids)
        self.base.setCurrentText(keep)
        self.base.blockSignals(False)

    def _from_photo(self):
        p, _ = QFileDialog.getOpenFileName(self, "Photo of the printed wedge", "", IMAGE_FILTER)
        if not p:
            return
        n = self.page.steps.value()
        axis = "x" if QMessageBox.question(
            self, APP, "Do the steps run left to right?\n\nNo = top to bottom.") == QMessageBox.Yes else "y"
        try:
            patches = calibrate.sample_image(p, n, axis)
        except Exception as exc:
            QMessageBox.critical(self, APP, f"Could not sample that image:\n{exc}")
            return
        self.text.setPlainText(",".join(colormath.to_hex(c) for c in patches))
        self.page.ed.status(f"sampled {n} patches from {os.path.basename(p)}", theme.OK)

    def read(self, db):
        """(measured (n,3), base rgb), or None if the patch list is empty."""
        raw = self.text.toPlainText().strip()
        if not raw:
            return None
        meas = np.array([colormath.parse_hex(t) for t in re.split(r"[,\s]+", raw) if t], float)
        spec = self.base.currentText().strip()
        try:
            base = np.array(colormath.parse_hex(spec), float)
        except ValueError:
            if spec not in db.filaments:
                raise ValueError(f"base {spec!r} is neither a hex colour nor a filament id")
            base = db.filaments[spec].rgb()
        return meas, base


class CalibratePage(QWidget):
    def __init__(self, editor):
        super().__init__()
        self.ed = editor
        self._fit = None
        sc, col = scroll_page()
        QVBoxLayout(self).addWidget(sc)

        w = section(col, "1 · Print a step wedge",
                    "A staircase carrying 1..N layers of this filament over an opaque base. Print "
                    "one over white AND one over black if you can: two backgrounds separate the "
                    "filament's colour from its opacity, which a single background cannot do.")
        self.wbase = QComboBox()
        self.steps = spin(3, 40, 1, 12)
        self.baselayers = spin(0, 60, 1, 0)
        self.baselayers.setSpecialValueText("auto (opaque)")
        self.stepw = spin(4, 40, 1, 14, 0)
        self.stepd = spin(4, 60, 1, 14, 0)
        self.gap = spin(0, 30, 1, 0, 0)
        self.hinge = spin(1, 29, 1, 4)
        self.flavor = QComboBox()
        self.flavor.addItems(["orca", "prusa"])
        w.addRow("Base filament", self.wbase)
        w.addRow("Steps", self.steps)
        self.grid_note = theme.hint("")
        w.addRow("Layers", self.grid_note)
        w.addRow("Base layers", self.baselayers)
        w.addRow("Step width (mm)", self.stepw)
        w.addRow("Step depth (mm)", self.stepd)
        w.addRow("Gap between steps (mm)", self.gap)
        w.addRow("Hinge layers", self.hinge)
        w.addRow("", theme.hint("With a gap, the base between steps thins to the hinge layers so each "
                                "step flexes flat onto the instrument (try 8 mm). Gap 0 = one solid wedge."))
        self.gap.valueChanged.connect(lambda v: self.hinge.setEnabled(v > 0))
        self.hinge.setEnabled(False)
        w.addRow("Slicer flavour", self.flavor)
        w.addRow("", theme.hint("After printing: read each step with the Measure tab (or take a "
                                "photo, cropped to just the steps) and fill in step 2 below."))
        b = QPushButton("Write wedge 3MF…")
        b.clicked.connect(self.write_wedge)
        w.addRow("", b)

        m = section(col, "2 · Measured patches",
                    "Thinnest step first, comma separated. A spectrophotometer is ideal; a phone "
                    "photo under flat indirect daylight with a white card in frame, white-balanced "
                    "against the card, works well enough. Crop the photo to just the row of "
                    "steps first: \u201cFrom photo\u201d samples evenly across the whole image.")
        m.addRow("Layer height (mm)", editor.layer.spin())
        m.addRow("", theme.hint("The layer height the wedge was printed at; it follows your "
                                "slicer project. A wrong one makes every td wrong."))
        self.wedge_a = WedgeInputs(self, "Wedge A", "#F4F5F0")
        self.wedge_b = WedgeInputs(self, "Wedge B (contrasting base)", "#1A1A1C")
        m.addRow(self.wedge_a)
        m.addRow(self.wedge_b)
        self.per_channel = QCheckBox("fit td per RGB channel (needs ≥6 steps)")
        m.addRow(self.per_channel)
        go = QHBoxLayout()
        fit = QPushButton("Fit")
        fit.setObjectName("primary")
        fit.clicked.connect(self.do_fit)
        self.apply_btn = QPushButton("Apply to filament")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self.apply_fit)
        go.addWidget(fit)
        go.addWidget(self.apply_btn)
        go.addStretch(1)
        m.addRow(go)
        self.report = report_box(260)
        col.addWidget(self.report, 1)

    # -- wedge -----------------------------------------------------------------------------

    def refresh_bases(self):
        ids = sorted(self.ed.db.filaments)
        keep = self.wbase.currentText()
        self.wbase.clear()
        self.wbase.addItems(ids)
        if keep not in ids:
            keep = next((i for i in ids if "white" in i), self.ed.current or "")
        self.wbase.setCurrentText(keep)
        self.wedge_a.set_bases(ids)
        self.wedge_b.set_bases(ids)

    def prepare(self, steps=None, base_id=None):
        """Preset the wedge for what the Match page is asking about."""
        if steps:
            self.steps.setValue(steps)
        if base_id and base_id in self.ed.db.filaments:
            self.wbase.setCurrentText(base_id)

    def grid(self):
        """(layer, first layer, template) from the project bar: the wedge must sit on the
        slicer's grid, and Flash Studio needs the template to keep the extruders."""
        proj = self.ed.project
        if proj is None:
            return None, None, None
        lh, fl = proj.layers()
        return lh, fl or lh, proj.get("template")

    def refresh_grid(self):
        lh, fl, tpl = self.grid()
        self.grid_note.setText(
            f"{lh:g} mm, first layer {fl:g} mm (from the slicer project at the top)" if lh else
            "choose your slicer project at the top first: it sets the layer height")
        if self.ed.project is not None:
            self.flavor.setCurrentText(self.ed.project.get("flavor") or "orca")

    def write_wedge(self):
        fil = self.ed.fil()
        if fil is None:
            return
        base = self.ed.db.filaments.get(self.wbase.currentText())
        if base is None:
            QMessageBox.warning(self, APP, "Choose a base filament.")
            return
        lh, fl, tpl = self.grid()
        flavor = self.flavor.currentText()
        if tpl and not os.path.exists(tpl):
            QMessageBox.warning(self, APP, "Your slicer project file was moved or deleted; choose "
                                           "it again in the bar at the top.")
            return
        if lh is None or (flavor == "orca" and not tpl):
            QMessageBox.warning(self, APP, "First choose your Flash Studio project file in the bar "
                                           "at the top. The wedge has to be built on its layer "
                                           "height, and Flash Studio needs it to keep the "
                                           "extruder assignments.")
            return
        p, _ = QFileDialog.getSaveFileName(self, "Write step wedge", f"wedge_{fil.id}.3mf", "3MF (*.3mf)")
        if not p:
            return
        if not p.lower().endswith(".3mf"):
            p += ".3mf"
        steps = self.steps.value()
        bl = self.baselayers.value() or optics.opaque_layers(base, fl, lh)[0]
        sw, sd = self.stepw.value(), self.stepd.value()
        try:
            gap = self.gap.value()
            hinge = min(self.hinge.value(), bl - 1) if gap > 0 else None
            plate, decals, w, base_h = calibrate.build_wedge(
                steps, lh, bl, sw, sd, gap, hinge_layers=hinge, first_layer_h=fl)
            threemf.get_writer(flavor)(
                p, [plate], {0: decals}, 1, "part", template=tpl, colors=[base.color, fil.color],
                layer_height=lh, first_layer_height=fl, solid=True,
                object_settings=threemf.solid_object_settings(flavor, lh))
        except (Exception, SystemExit) as exc:
            QMessageBox.critical(self, APP, f"Could not write the wedge:\n{exc}")
            return
        self.ed.layer.set(lh)
        self.ed.status(f"wrote {os.path.basename(p)}", theme.OK)
        QMessageBox.information(
            self, APP,
            f"Wrote {p}\n\n{steps} steps, 1..{steps} layers of {fil.label()} over {bl} base layers "
            f"of {base.label()}.\n{w:.1f} × {sd:.1f} mm, {base_h:.2f}..{base_h + steps * lh:.2f} mm "
            f"tall.\n\nExtruder 1 = {base.label()}\nExtruder 2 = {fil.label()}\n\n"
            f"The file carries the {lh:g} mm layer height and {fl:g} mm first layer: don't change "
            "them in the slicer.\n\nPrint a second wedge over a contrasting "
            "base if you can — one background cannot separate colour from opacity.")

    # -- fit -------------------------------------------------------------------------------

    def reset_fit(self):
        self._fit = None
        self.apply_btn.setEnabled(False)
        self.report.clear()

    def do_fit(self):
        fil = self.ed.fil()
        if fil is None:
            return
        try:
            datasets = [d for d in (self.wedge_a.read(self.ed.db), self.wedge_b.read(self.ed.db))
                        if d is not None]
        except ValueError as exc:
            QMessageBox.warning(self, APP, str(exc))
            return
        if not datasets:
            QMessageBox.warning(self, APP, "Type or sample the measured patches first.")
            return
        steps = min(len(m) for m, _ in datasets)
        if steps < 3:
            QMessageBox.warning(self, APP, "Need at least 3 steps to fit anything meaningful.")
            return
        lh, per_channel = self.ed.layer.value, self.per_channel.isChecked()
        try:
            fit = calibrate.fit_td(datasets, lh, per_channel, fil.rgb())
        except Exception as exc:
            QMessageBox.critical(self, APP, f"Fit failed:\n{exc}")
            return
        td, col, des, _preds, (span, nsets) = fit
        show_lines(self.report, fit_lines(fil, datasets, lh, per_channel, fit))
        mean = float(np.concatenate(des).mean())
        stop = blocked(span, nsets)
        self._fit = None if stop else {"td": td, "color": colormath.to_hex(col), "per_channel": per_channel,
                                       "layer_height": lh, "steps": int(steps), "nsets": nsets}
        self.apply_btn.setEnabled(not stop)
        self.ed.status(f"fit dE mean {mean:.1f}" + ("  — not applicable" if stop else ""),
                       theme.ERR if stop else (theme.OK if mean <= 5 else theme.WARN))

    def apply_fit(self):
        fil, r = self.ed.fil(), self._fit
        if fil is None or not r:
            return
        fil.color = r["color"]
        if r["per_channel"]:
            fil.td_rgb = [round(float(v), 4) for v in r["td"]]
            fil.td = round(float(np.mean(r["td"])), 4)
        else:
            fil.td, fil.td_rgb = round(float(r["td"][0]), 4), None
        fil.provenance = "measured"
        fil.layer_height_ref = r["layer_height"]
        fil.measured_at = date.today().isoformat()
        fil.notes = (fil.notes + " | ").lstrip(" |") + (
            f"fit from {r['steps']}-step wedge" + (" over two bases" if r["nsets"] > 1 else ""))
        self.ed.reload_fields()
        self.ed.touch()
        self.apply_btn.setEnabled(False)
        self.ed.status("applied — press Save to write it to disk", theme.WARN)
        self.ed.show_page("look")        # show what those numbers look like
