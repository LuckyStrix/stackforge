"""The guided calibration: pick a filament, write a step wedge, enter what you measured, fit
td and colour, save it to the library. Hosted by the Calibrate tab; the Filaments editor and the
Measure tab hand over to it (`CalibrateTab.start` / `set_measured`)."""
from __future__ import annotations

import os
import re
from datetime import date

import numpy as np
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                               QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from stackforge.core import colormath, wedgesheet
from stackforge.core.filamentdb import DB, DEFAULT_DB, PROVENANCE_LABEL
from stackforge.gui import theme
from stackforge.gui.filaments import APP
from stackforge.gui.filaments.common import (SharedValue, report_box, scroll_page, section,
                                             show_lines, spin)
from stackforge.gui.pickers import swatch_icon
from stackforge.gui.settings import PresetStore
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
        self.page.status(f"sampled {n} patches from {os.path.basename(p)}", theme.OK)

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
    """Works on the filament database on disk (the project's), so a fit is saved straight to
    the library; `on_saved(fid)` lets the Filaments editor pick the change up."""

    def __init__(self, project=None, db_path=None, on_saved=None, presets=None):
        super().__init__()
        self.project, self.on_saved = project, on_saved
        self.presets = presets or PresetStore()
        self.db_path = db_path or (project.get("db") if project else DEFAULT_DB)
        self.db = DB(self.db_path)
        self.layer = SharedValue(0.08)
        self._fit = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        sc, col = scroll_page()
        outer.addWidget(sc, 1)

        top = section(col, "Filament to calibrate",
                      "Calibrating measures how see-through a filament really is (its td) and its "
                      "true colour, so plaque previews match the print. Three steps: print a "
                      "wedge, read its colours, fit.")
        self.fil_combo = QComboBox()
        self.fil_combo.setMinimumWidth(320)
        self.fil_combo.currentIndexChanged.connect(lambda *_: self._fil_changed())
        top.addRow("Filament", self.fil_combo)
        self.fil_note = theme.hint("")
        top.addRow("", self.fil_note)

        w = section(col, "1 · Print the step wedges",
                    "Staircases carrying 1 to N layers of this filament over an opaque base: one "
                    "over a light base and one over a dark base, both in one file. Two backgrounds "
                    "separate the filament's colour from its opacity, which one cannot do. A wedge "
                    "whose base is the filament itself is left out (white over white shows "
                    "nothing), so calibrating your white gives one wedge, over the dark base.")
        self.wbase = QComboBox()
        self.wbase2 = QComboBox()
        self.steps = spin(3, 40, 1, 12)
        self.baselayers = spin(0, 60, 1, 0)
        self.baselayers.setSpecialValueText("auto (opaque)")
        self.stepw = spin(4, 40, 1, 14, 0)
        self.stepd = spin(4, 60, 1, 14, 0)
        self.gap = spin(0, 30, 1, 0, 0)
        self.hinge = spin(1, 29, 1, 4)
        self.flavor = QComboBox()
        self.flavor.addItems(["orca", "prusa"])
        w.addRow("Light base", self.wbase)
        w.addRow("Dark base", self.wbase2)
        w.addRow("Steps", self.steps)
        self.grid_note = theme.hint("")
        self.grid_note.setWordWrap(False)       # one line; wrapped, the form row clips it
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
        row = QHBoxLayout()
        b = QPushButton("Write wedges 3MF…")
        b.clicked.connect(self.write_wedge)
        row.addWidget(b)
        row.addStretch(1)
        for text, fn, tip in (
                ("Set as default", self.save_defaults,
                 "Remember the bases, steps, sizes, gap, hinge and per-channel choice: they are "
                 "filled in every time stackforge starts."),
                ("Restore built-in", self.restore_defaults,
                 "Forget your defaults and go back to the ones stackforge ships with.")):
            d = QPushButton(text)
            d.setToolTip(tip)
            d.clicked.connect(fn)
            row.addWidget(d)
        w.addRow("", row)

        m = section(col, "2 · Read the printed steps",
                    "Easiest: give the wedge sheet written in step 1 to the Measure tab "
                    "(measure-wedge, on whichever computer has the ColorMunki). It reads the bare "
                    "base and every step of every wedge and writes a readings file; “Load "
                    "readings…” fills everything below from it, using the measured base "
                    "colours. Otherwise type the colours, thinnest step first, comma separated, "
                    "or use a phone photo under flat indirect daylight with a white card in frame, "
                    "white-balanced against the card: crop it to just the row of steps first, "
                    "because “From photo” samples evenly across the whole image.")
        load = QPushButton("Load readings…")
        load.setToolTip("The *.readings.json that Measure wrote from the wedge sheet")
        load.clicked.connect(lambda: self.load_readings())
        m.addRow("Readings file", load)
        m.addRow("Layer height (mm)", self.layer.spin())
        m.addRow("", theme.hint("The layer height the wedge was printed at; it follows your "
                                "slicer project. A wrong one makes every td wrong."))
        self.wedge_a = WedgeInputs(self, "Wedge A", "#F4F5F0")
        self.wedge_b = WedgeInputs(self, "Wedge B (contrasting base)", "#1A1A1C")
        m.addRow(self.wedge_a)
        m.addRow(self.wedge_b)

        f = section(col, "3 · Fit and save")
        self.per_channel = QCheckBox("fit td per RGB channel (needs ≥6 steps)")
        f.addRow(self.per_channel)
        go = QHBoxLayout()
        fit = QPushButton("Fit")
        fit.setObjectName("primary")
        fit.clicked.connect(self.do_fit)
        self.apply_btn = QPushButton("Save to filament library")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self.apply_fit)
        go.addWidget(fit)
        go.addWidget(self.apply_btn)
        go.addStretch(1)
        f.addRow(go)
        self.report = report_box(260)
        col.addWidget(self.report, 1)
        self.lbl_status = QLabel()
        outer.addWidget(self.lbl_status)

        if project is not None:
            project.subscribe(self.project_changed)
        self._builtin = self.default_values()
        self.reload()
        self._builtin.update(wbase=self.wbase.currentText(), wbase2=self.wbase2.currentText())
        self.project_changed()
        self.load_defaults()

    # -- state -----------------------------------------------------------------------------

    def status(self, msg, colour=theme.FG_DIM):
        self.lbl_status.setText(msg)
        self.lbl_status.setStyleSheet(f"color:{colour}")

    def fil(self):
        return self.db.filaments.get(self.fil_combo.currentData() or "")

    def reload(self, path=None):
        """Re-read the database (it may have been edited elsewhere); keeps the selection."""
        self.db_path = path or self.db_path
        try:
            self.db = DB(self.db_path)
        except SystemExit as exc:
            QMessageBox.critical(self, APP, str(exc))
            return
        keep = self.fil_combo.currentData()
        self.fil_combo.blockSignals(True)
        self.fil_combo.clear()
        for fil in sorted(self.db.filaments.values(), key=lambda f: f.label().lower()):
            self.fil_combo.addItem(swatch_icon(fil.color, 26, 16), fil.label(), fil.id)
        i = self.fil_combo.findData(keep)
        self.fil_combo.setCurrentIndex(max(i, 0))
        self.fil_combo.blockSignals(False)
        self.refresh_bases()
        self._fil_changed(reset=False)

    def project_changed(self):
        if self.project is None:
            self.refresh_grid()
            return
        db = self.project.get("db")
        if db != self.db_path:
            self.reload(db)
        lh = self.project.layers()[0]
        if lh:
            self.layer.set(lh)
        self.refresh_grid()

    def select(self, fid):
        i = self.fil_combo.findData(fid)
        if i < 0:
            return False
        self.fil_combo.setCurrentIndex(i)
        return True

    def _fil_changed(self, reset=True):
        fil = self.fil()
        if reset:
            self.reset_fit()
        if fil is None:
            self.fil_note.setText("no filaments in the library yet")
            return
        self.fil_note.setText(
            f"td {fil.td:g} mm now, source: {PROVENANCE_LABEL.get(fil.provenance, fil.provenance)}"
            + (f" (measured {fil.measured_at})" if fil.measured_at else ""))

    # -- defaults --------------------------------------------------------------------------

    DEFAULTS = ("calibrate", ("guided",), "default")

    def default_values(self) -> dict:
        return {"wbase": self.wbase.currentText(), "wbase2": self.wbase2.currentText(),
                "steps": self.steps.value(), "base_layers": self.baselayers.value(),
                "step_width": self.stepw.value(), "step_depth": self.stepd.value(),
                "gap": self.gap.value(), "hinge": self.hinge.value(),
                "per_channel": self.per_channel.isChecked()}

    def apply_values(self, v: dict):
        for key, box in (("wbase", self.wbase), ("wbase2", self.wbase2)):
            if v.get(key) and box.findText(v[key]) >= 0:      # a base gone from the library: skip
                box.setCurrentText(v[key])
        for key, sp in (("steps", self.steps), ("base_layers", self.baselayers),
                        ("step_width", self.stepw), ("step_depth", self.stepd),
                        ("gap", self.gap), ("hinge", self.hinge)):
            if isinstance(v.get(key), (int, float)):
                sp.setValue(v[key])
        if "per_channel" in v:
            self.per_channel.setChecked(bool(v["per_channel"]))

    def load_defaults(self):
        try:
            self.apply_values(self.presets.load(*self.DEFAULTS))
        except (OSError, ValueError, AttributeError):
            pass            # none saved (or unreadable): the built-in ones stand

    def save_defaults(self):
        try:
            self.presets.save(*self.DEFAULTS, self.default_values())
        except OSError as exc:
            QMessageBox.critical(self, APP, f"Could not save the defaults:\n{exc}")
            return
        self.status("saved: these wedge settings now load every time stackforge starts", theme.OK)

    def restore_defaults(self):
        self.presets.delete(*self.DEFAULTS)
        self.apply_values(self._builtin)
        self.status("back to the built-in wedge settings", theme.OK)

    # -- wedge -----------------------------------------------------------------------------

    NO_BASE = "(none)"

    def refresh_bases(self):
        ids = sorted(self.db.filaments)
        keep = self.wbase.currentText()
        self.wbase.clear()
        self.wbase.addItems(ids)
        if keep not in ids:
            keep = next((i for i in ids if "white" in i), ids[0] if ids else "")
        self.wbase.setCurrentText(keep)
        keep2 = self.wbase2.currentText()
        self.wbase2.clear()
        self.wbase2.addItems([self.NO_BASE] + ids)
        if keep2 not in ids and keep2 != self.NO_BASE:
            dark = min(self.db.filaments.values(), key=lambda f: float(sum(f.rgb())), default=None)
            keep2 = dark.id if dark is not None else self.NO_BASE
        self.wbase2.setCurrentText(keep2)
        self.wedge_a.set_bases(ids)
        self.wedge_b.set_bases(ids)
        if self.wedge_b.base.currentText() == "#1A1A1C":
            dark = min(self.db.filaments.values(), key=lambda f: float(sum(f.rgb())), default=None)
            if dark is not None:
                self.wedge_b.base.setCurrentText(dark.id)

    def prepare(self, steps=None, base_id=None):
        """Preset the wedge for what the Match page is asking about."""
        if steps:
            self.steps.setValue(steps)
        if base_id and base_id in self.db.filaments:
            self.wbase.setCurrentText(base_id)

    def load_readings(self, path=None) -> bool:
        """Fill the wedges from a measure readings file: the filament, each wedge's readings
        against its measured base, the step count and the layer height it was printed at."""
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Wedge readings from Measure", "",
                                                  "Wedge readings (*.readings.json);;JSON (*.json)")
            if not path:
                return False
        try:
            data = wedgesheet.load_readings(path)
        except ValueError as exc:
            QMessageBox.warning(self, APP, str(exc))
            return False
        strips = wedgesheet.strips_for(data, self.fil_combo.currentData())
        fil = strips[0]["filament"]
        if not self.select(fil["id"]):
            QMessageBox.warning(self, APP, f"{fil['label']} ({fil['id']}) is not in your filament "
                                           "library: add it in the Filaments tab first.")
            return False
        notes = []
        others = {s["filament"]["label"] for s in data["strips"]} - {fil["label"]}
        if others:
            notes.append(f"the file also has {', '.join(sorted(others))}: choose that filament "
                         "and load the file again to fit it")
        for box, s in zip((self.wedge_a, self.wedge_b), strips + [None]):
            box.text.setPlainText(s["hex"] if s else "")
            if s:
                box.base.setCurrentText(s.get("base_hex") or s["base"]["id"])
                if s.get("reversed_steps"):
                    notes.append(f"wedge {s['wedge']}: step(s) {s['reversed_steps']} ran against "
                                 "the wedge when measured")
        if len(strips) > 2:
            notes.append(f"only the first two of {len(strips)} wedges are used")
        self.steps.setValue(data["steps"])
        lh = data["layer_height"]
        proj = self.project.layers()[0] if self.project is not None else None
        if proj and abs(proj - lh) > 1e-9:
            notes.append(f"printed at {lh:g} mm layers, not the project's {proj:g}: fitting at {lh:g}")
        self.layer.set(lh)
        self.reset_fit()
        measured = all(s.get("base_hex") for s in strips)
        self.status(f"loaded {len(strips[:2])} wedge(s) of {fil['label']}"
                    + (" with measured bases" if measured else "") + ": press Fit"
                    + ("  — " + "; ".join(notes) if notes else ""),
                    theme.WARN if notes else theme.OK)
        return True

    def add_measured(self, hexes: str, steps=None, base=None) -> str:
        """Put a hex list from the Measure tab into Wedge A, or B if A is filled, with its
        measured `base` hex if the bare patch was read. Returns which."""
        target = self.wedge_a if not self.wedge_a.text.toPlainText().strip() else self.wedge_b
        target.text.setPlainText(hexes)
        if base:
            target.base.setCurrentText(base)
        n = len([h for h in hexes.split(",") if h.strip()])
        if n:
            self.steps.setValue(n)
        which = "A" if target is self.wedge_a else "B"
        self.status(f"{n} measured steps put in Wedge {which}: check its base, then Fit",
                    theme.OK)
        return which

    def grid(self):
        """(layer, first layer, template) from the project bar: the wedge must sit on the
        slicer's grid, and Flash Studio needs the template to keep the extruders."""
        proj = self.project
        if proj is None:
            return None, None, None
        lh, fl = proj.layers()
        return lh, fl or lh, proj.get("template")

    def refresh_grid(self):
        lh, fl, tpl = self.grid()
        self.grid_note.setText(
            f"{lh:g} mm, first layer {fl:g} mm (from the slicer project at the top)" if lh else
            "choose your slicer project at the top first: it sets the layer height")
        if self.project is not None:
            self.flavor.setCurrentText(self.project.get("flavor") or "orca")

    def write_wedge(self):
        fil = self.fil()
        if fil is None:
            return
        bases = [self.db.filaments.get(c.currentText()) for c in (self.wbase, self.wbase2)]
        bases = [b for i, b in enumerate(bases) if b is not None and b not in bases[:i]]
        if not bases:
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
        p, _ = QFileDialog.getSaveFileName(self, "Write step wedges", f"wedge_{fil.id}.3mf", "3MF (*.3mf)")
        if not p:
            return
        if not p.lower().endswith(".3mf"):
            p += ".3mf"
        steps = self.steps.value()
        try:
            ws = calibrate.WedgeSet([fil], bases, steps, lh, fl, self.stepw.value(),
                                    self.stepd.value(), self.gap.value(),
                                    self.baselayers.value() or None, self.hinge.value())
            ws.write(p, flavor, tpl)
            sheet = ws.write_sheet(p)
        except (Exception, SystemExit) as exc:
            QMessageBox.critical(self, APP, f"Could not write the wedges:\n{exc}")
            return
        self.layer.set(lh)
        # New wedges: old readings would land in the wrong box or against the wrong base.
        # Each wedge's readings go in the box of the same letter, against its base.
        for box in (self.wedge_a, self.wedge_b):
            box.text.clear()
        for box, (b, _rows) in zip((self.wedge_a, self.wedge_b), ws.wedges):
            box.base.setCurrentText(b.id)
        self.reset_fit()
        self.status(f"wrote {os.path.basename(p)} and {os.path.basename(sheet)}", theme.OK)
        one = len(ws.wedges) == 1
        QMessageBox.information(
            self, APP,
            f"Wrote {p}\n\n{len(ws.wedges)} wedge{'' if one else 's'}, steps of 1 to {steps} layers of "
            f"{fil.label()} over {ws.base_layers} base layers.\n{ws.width:.1f} × {ws.depth:.1f} "
            f"mm; base {ws.base_h:.2f} mm thick, tallest step {ws.top:.2f} mm.\n\n" + "\n".join(ws.describe())
            + f"\n\nThe file carries the {lh:g} mm layer height and {fl:g} mm first layer: don't "
            "change them in the slicer."
            + f"\n\nAlso wrote the wedge sheet {os.path.basename(sheet)}. Take it to the "
            "ColorMunki: Measure > measure-wedge with it as the wedge sheet reads every wedge in "
            "order and writes a readings file. Bring that back and press “Load readings…” "
            "in step 2."
            + ("\n\nOnly one wedge: choose a contrasting dark base if you can — one background "
               "cannot separate colour from opacity." if one else ""))

    # -- fit -------------------------------------------------------------------------------

    def reset_fit(self):
        self._fit = None
        self.apply_btn.setEnabled(False)
        self.report.clear()

    def do_fit(self):
        fil = self.fil()
        if fil is None:
            return
        try:
            datasets = [d for d in (self.wedge_a.read(self.db), self.wedge_b.read(self.db))
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
        lh, per_channel = self.layer.value, self.per_channel.isChecked()
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
        self.status(f"fit dE mean {mean:.1f}" + ("  — not applicable" if stop else
                                                     "  — press Save to filament library"),
                       theme.ERR if stop else (theme.OK if mean <= 5 else theme.WARN))

    def apply_fit(self):
        """Write the fit into the library file (re-read first, so nothing else is lost)."""
        fid, r = self.fil_combo.currentData(), self._fit
        if not fid or not r:
            return
        try:
            db = DB(self.db_path)
        except SystemExit as exc:
            QMessageBox.critical(self, APP, str(exc))
            return
        fil = db.filaments.get(fid)
        if fil is None:
            QMessageBox.warning(self, APP, f"{fid} is not in {self.db_path} any more.")
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
        try:
            db.save()
        except OSError as exc:
            QMessageBox.critical(self, APP, f"Could not write {db.path}:\n{exc}")
            return
        self.db = db
        self.apply_btn.setEnabled(False)
        self._fil_changed(reset=False)
        self.status(f"saved: {fil.label()} td {fil.td:g}, colour {fil.color}", theme.OK)
        if self.on_saved:
            self.on_saved(fid)
