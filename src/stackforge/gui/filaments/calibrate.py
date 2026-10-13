"""The guided calibration: pick a filament, write its step wedges, then hand the readings
file Measure wrote to the spectral fit (Spectra tab). Hosted by the Calibrate tab; the Filaments
editor and the Measure tab hand over to it (`CalibrateTab.start` / `load_readings`). There is no
fit here: the RGB td fit is not on this branch, and typed colours or photos carry no spectrum."""
from __future__ import annotations

import os

from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

from stackforge.core.spectral import is_calibrated
from stackforge.core import wedgesheet
from stackforge.core.filamentdb import DB, DEFAULT_DB
from stackforge.gui import theme
from stackforge.gui.filaments import APP
from stackforge.gui.filaments.common import scroll_page, section, spin
from stackforge.gui.pickers import swatch_icon
from stackforge.gui.settings import PresetStore
from stackforge.tools import calibrate
from stackforge.tools import spectral as spectral_tool


class CalibratePage(QWidget):
    """Reads the filament database on disk (the project's). `on_readings(path, fid)` opens the
    spectral fit on a readings file and returns whether it could."""

    def __init__(self, project=None, db_path=None, on_readings=None, presets=None):
        super().__init__()
        self.project, self.on_readings = project, on_readings
        self.presets = presets or PresetStore()
        self.db_path = db_path or (project.get("db") if project else DEFAULT_DB)
        self.db = DB(self.db_path)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        sc, col = scroll_page()
        outer.addWidget(sc, 1)

        top = section(col, "Filament to calibrate",
                      "Calibrating measures what a filament absorbs and scatters at every "
                      "wavelength, so plaque previews match the print. Three steps: print the "
                      "wedges, read them with the ColorMunki, fit.")
        self.fil_combo = QComboBox()
        self.fil_combo.setMinimumWidth(320)
        self.fil_combo.currentIndexChanged.connect(lambda *_: self._fil_changed())
        top.addRow("Filament", self.fil_combo)
        self.fil_note = theme.hint("")
        top.addRow("", self.fil_note)

        w = section(col, "1 · Print the step wedges",
                    "Staircases carrying 1 to N layers of this filament over an opaque base: one "
                    "over a light base and one over a dark base, both in one file. Two backgrounds "
                    "separate absorption from scattering, which one cannot do: the fit refuses to "
                    "save from a single base. A wedge whose base is the filament itself is left "
                    "out (white over white shows nothing), so to calibrate your white choose a "
                    "different light filament as the light base.")
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
                 "Remember the bases, steps, sizes, gap and hinge: they are "
                 "filled in every time stackforge starts."),
                ("Restore built-in", self.restore_defaults,
                 "Forget your defaults and go back to the ones stackforge ships with.")):
            d = QPushButton(text)
            d.setToolTip(tip)
            d.clicked.connect(fn)
            row.addWidget(d)
        w.addRow("", row)

        m = section(col, "2 · Measure and fit",
                    "Give the wedge sheet written in step 1 to the Measure tab (measure-wedge, on "
                    "whichever computer has the ColorMunki). It reads the bare base and every step "
                    "of every wedge, spectrum included, and writes a readings file. “Load "
                    "readings…” hands that file to the spectral fit in the Spectra tab, which "
                    "saves the filament's absorption and scattering per wavelength.")
        load = QPushButton("Load readings…")
        load.setToolTip("The *.readings.json that Measure wrote from the wedge sheet")
        load.clicked.connect(lambda: self.load_readings())
        m.addRow("Readings file", load)
        col.addStretch(1)
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
        self._fil_changed()

    def project_changed(self):
        if self.project is None:
            self.refresh_grid()
            return
        db = self.project.get("db")
        if db != self.db_path:
            self.reload(db)
        self.refresh_grid()

    def select(self, fid):
        i = self.fil_combo.findData(fid)
        if i < 0:
            return False
        self.fil_combo.setCurrentIndex(i)
        return True

    def _fil_changed(self):
        fil = self.fil()
        if fil is None:
            self.fil_note.setText("no filaments in the library yet")
            return
        self.fil_note.setText("spectrally calibrated" if is_calibrated(fil) else
                              "not spectrally calibrated yet: spectral plaques refuse it")

    # -- defaults --------------------------------------------------------------------------

    DEFAULTS = ("calibrate", ("guided",), "default")

    def default_values(self) -> dict:
        return {"wbase": self.wbase.currentText(), "wbase2": self.wbase2.currentText(),
                "steps": self.steps.value(), "base_layers": self.baselayers.value(),
                "step_width": self.stepw.value(), "step_depth": self.stepd.value(),
                "gap": self.gap.value(), "hinge": self.hinge.value()}

    def apply_values(self, v: dict):
        for key, box in (("wbase", self.wbase), ("wbase2", self.wbase2)):
            if v.get(key) and box.findText(v[key]) >= 0:      # a base gone from the library: skip
                box.setCurrentText(v[key])
        for key, sp in (("steps", self.steps), ("base_layers", self.baselayers),
                        ("step_width", self.stepw), ("step_depth", self.stepd),
                        ("gap", self.gap), ("hinge", self.hinge)):
            if isinstance(v.get(key), (int, float)):
                sp.setValue(v[key])

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

    def prepare(self, steps=None, base_id=None):
        """Preset the wedge for what the Match page is asking about."""
        if steps:
            self.steps.setValue(steps)
        if base_id and base_id in self.db.filaments:
            self.wbase.setCurrentText(base_id)

    def load_readings(self, path=None) -> bool:
        """Hand a measure readings file to the spectral fit, for the chosen filament if the
        file has it, else the file's first. False if nothing was handed over."""
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
        for s in strips:
            if s.get("reversed_steps"):
                notes.append(f"wedge {s['wedge']}: step(s) {s['reversed_steps']} ran against "
                             "the wedge when measured")
        try:
            weak = spectral_tool.signal_note(spectral_tool.datasets_from_readings(data, fil["id"])[0])
        except SystemExit:
            weak = None                          # the fit itself says what is wrong
        if weak:
            notes.append("the fit will refuse to save: " + weak)
        if not (self.on_readings and self.on_readings(path, fil["id"])):
            QMessageBox.information(
                self, APP, "Fit these readings with the spectral fit (Spectra tab > All options "
                           f"> fit), or:\n\nstackforge-spectral fit --readings {path} "
                           f"--filament {fil['id']}")
            return False
        self.status(f"{len(strips)} wedge(s) of {fil['label']} handed to the spectral fit"
                    + ("  — " + "; ".join(notes) if notes else ""),
                    theme.WARN if notes else theme.OK)
        return True

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
            "in step 2 to fit it."
            + "".join(f"\n\n{n} Choose it as a base and write the wedges again."
                      for n in ws.contrast_notes()))
