"""FilamentEditor: the library list on the left; details, look and match on the right.

Every edit goes straight into the selected `Filament` in memory and marks the database dirty;
Save validates and writes it. Pages (look/match) refresh from the `edited` signal. Calibrating
is the Calibrate tab's job: "Calibrate this filament…" hands over through `on_calibrate`.
"""
from __future__ import annotations

import os
from copy import deepcopy

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QSplitter, QTabWidget, QVBoxLayout, QWidget)

from stackforge.core import colormath
from stackforge.core.filamentdb import (DB, PROVENANCE, PROVENANCE_LABEL, TD_GUESS, Filament,
                                        provenance_counts, seed_db, slugify)
from stackforge.gui import theme
from stackforge.gui.filaments import APP
from stackforge.gui.filaments.common import SharedValue, scroll_page, section, spin
from stackforge.gui.filaments.dialogs import PhotoPicker, SkuBrowser, download_catalog
from stackforge.gui.filaments.look import LookPage
from stackforge.gui.filaments.match import MatchPage
from stackforge.gui.pickers import _pick_color, swatch_icon
from stackforge.gui.widgets import IMAGE_FILTER
from stackforge.tools import polymaker

ABOUT = (
    "Colours and optical properties for the 3mf tools.\n\n"
    "td is the thickness in mm at which transmittance falls to 1/e (36.8%): T(t) = exp(-t/td). "
    "This is NOT HueForge's TD scale — use the converter on the Details page rather than pasting "
    "values across.\n\n"
    "An entry is only as good as its provenance. 'estimated' means nobody measured it and the "
    "colours it produces will be approximate.")

PAGES = ("details", "look", "match")


class FilamentEditor(QWidget):
    edited = Signal()                 # the selected filament changed (or another was selected)
    dirty_changed = Signal(bool)

    def __init__(self, db_path, catalog_path=polymaker.CACHE, parent=None, project=None):
        super().__init__(parent)
        self.db_path, self.catalog_path = db_path, catalog_path
        self.project = project            # the layer grid the previews assume
        self.on_calibrate = None          # (fid, steps, base_id) -> open the Calibrate tab on it
        self.db = DB(db_path)
        self._disk = deepcopy(self.db.filaments)   # as last read/written: what "unsaved" is against
        self.layer = SharedValue(0.08)    # one layer height for every page
        self.dirty = False
        self.current: str | None = None
        self._cat = None                  # Polymaker catalogue, loaded lazily
        self._loading = False             # suppress field signals while loading

        split = QSplitter()
        split.addWidget(self._build_list())
        split.addWidget(self._build_right())
        split.setStretchFactor(1, 1)
        split.setSizes([330, 900])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(split, 1)
        lay.addLayout(self._build_bar())
        self._render_list()
        if self.db.filaments:
            self.select(sorted(self.db.filaments)[0])
        else:
            self.pages["details"].setEnabled(False)
        if project is not None:
            project.subscribe(self.project_changed)
        self.project_changed()

    def project_changed(self):
        """Follow the project's layer grid: every page must assume the slicer's layer height."""
        lh = self.project.layers()[0] if self.project is not None else None
        if lh:
            self.layer.set(lh)

    def request_calibrate(self, steps=None, base_id=None):
        """Hand the selected filament to the Calibrate tab (saving first, since it reads the
        library from disk)."""
        fil = self.fil()
        if fil is None:
            return
        if self.dirty:
            r = QMessageBox.question(
                self, APP, "Save your changes to the filament library first? Calibrating works "
                           "on the saved library.", QMessageBox.Save | QMessageBox.Cancel)
            if r != QMessageBox.Save or not self.save():
                return
        if self.on_calibrate is None:
            QMessageBox.information(self, APP, "Use the Calibrate tab to calibrate a filament.")
            return
        self.on_calibrate(fil.id, steps, base_id)

    # -- left: the library ------------------------------------------------------------------

    def _build_list(self) -> QWidget:
        w = QWidget()
        col = QVBoxLayout(w)
        col.setContentsMargins(0, 0, 4, 0)
        head = QHBoxLayout()
        title = QLabel("Filaments")
        title.setStyleSheet("font-weight: bold")
        self.count = theme.hint("")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.count)
        col.addLayout(head)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(lambda *_: self._render_list())
        col.addWidget(self.filter)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda it, _p: it and self.select(it.data(Qt.UserRole)))
        col.addWidget(self.list, 1)
        self.measured_note = QLabel()
        self.measured_note.setWordWrap(True)
        col.addWidget(self.measured_note)
        row = QHBoxLayout()
        for text, fn in (("New", self.new_filament), ("Duplicate", self.duplicate), ("Delete", self.delete)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        col.addLayout(row)
        sku = QPushButton("Add from Polymaker SKU…")
        sku.clicked.connect(self._browse_skus)
        col.addWidget(sku)
        cal = QPushButton("Calibrate this filament…")
        cal.setToolTip("Measure its real td and colour with a printed step wedge (Calibrate tab)")
        cal.clicked.connect(lambda: self.request_calibrate())
        col.addWidget(cal)
        return w

    def _item_text(self, fil) -> str:
        sub = " ".join(x for x in (fil.brand, fil.series) if x)
        prov = PROVENANCE_LABEL.get(fil.provenance, fil.provenance)
        return f"{fil.name or fil.id}\n{sub}   td {fil.td:.2f} · {prov}"

    def _style_item(self, it, fil):
        it.setText(self._item_text(fil))
        it.setIcon(swatch_icon(fil.color if _ok_hex(fil.color) else "#808080", 26, 18))
        it.setForeground(QColor(theme.PROVENANCE_COLOUR.get(fil.provenance, theme.FG_DIM)))
        it.setToolTip(f"td source: {PROVENANCE_LABEL.get(fil.provenance, fil.provenance)}")

    def _render_list(self):
        needle = self.filter.text().strip().lower()
        rows = sorted(self.db.filaments.values(),
                      key=lambda f: (f.brand.lower(), f.series.lower(), f.name.lower()))
        self.list.blockSignals(True)
        self.list.clear()
        keep = None
        for fil in rows:
            if needle and needle not in (fil.label() + " " + fil.id).lower():
                continue
            it = QListWidgetItem()
            it.setData(Qt.UserRole, fil.id)
            self._style_item(it, fil)
            self.list.addItem(it)
            if fil.id == self.current:
                keep = it
        if keep:
            self.list.setCurrentItem(keep)
        self.list.blockSignals(False)
        total = len(self.db.filaments)
        self.count.setText(f"{self.list.count()}/{total}" if needle else str(total))
        n = sum(f.provenance == "measured" for f in self.db.filaments.values())
        self.measured_note.setText(
            f"td: {provenance_counts(self.db.filaments.values())}. Only measured values come "
            f"from a calibration; plaque colours from the rest are approximate until you "
            f"calibrate them (Calibrate tab)." if n < total else f"all {total} measured")
        self.measured_note.setObjectName("warn" if n < total else "hint")
        self.measured_note.style().unpolish(self.measured_note)
        self.measured_note.style().polish(self.measured_note)

    def _item(self, fid):
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(Qt.UserRole) == fid:
                return it
        return None

    # -- right: pages -----------------------------------------------------------------------

    def _build_right(self) -> QWidget:
        self.tabs = QTabWidget()
        self.pages = {"details": self._build_details()}
        self.look = LookPage(self)
        self.match = MatchPage(self)
        self.pages.update(look=self.look, match=self.match)
        for name, title in zip(PAGES, ("Details", "Look", "Match by eye", "Calibrate")):
            self.tabs.addTab(self.pages[name], title)
        return self.tabs

    def show_page(self, name):
        self.tabs.setCurrentWidget(self.pages[name])

    def _line(self, key, width=None) -> QLineEdit:
        e = QLineEdit()
        if width:
            e.setMaximumWidth(width)
        e.textChanged.connect(lambda t, k=key: self._set_attr(k, t))
        return e

    def _build_details(self) -> QWidget:
        sc, col = scroll_page()
        idn = section(col, "Identity")
        self.e_id = QLineEdit()
        self.e_id.editingFinished.connect(self._commit_id)
        reslug = QPushButton("from name")
        reslug.clicked.connect(self._reslug)
        row = QHBoxLayout()
        row.addWidget(self.e_id, 1)
        row.addWidget(reslug)
        idn.addRow("id", row)
        idn.addRow("", theme.hint("How every other tool refers to this filament. Renaming one "
                                  "breaks any command line that names it."))
        self.e_brand, self.e_series, self.e_name = self._line("brand"), self._line("series"), self._line("name")
        idn.addRow("Brand", self.e_brand)
        idn.addRow("Series", self.e_series)
        idn.addRow("Name", self.e_name)
        self.e_sku = self._line("sku", 140)
        self.e_sku.returnPressed.connect(self._lookup_sku)
        look, browse = QPushButton("Look up"), QPushButton("Browse…")
        look.clicked.connect(self._lookup_sku)
        browse.clicked.connect(self._browse_skus)
        row = QHBoxLayout()
        for w in (self.e_sku, look, browse):
            row.addWidget(w)
        row.addStretch(1)
        idn.addRow("Polymaker SKU", row)
        idn.addRow("", theme.hint("Fills the colour, and the TD where Polymaker publish one, from "
                                  "their wiki. Vendor data, never 'measured'."))

        ap = section(col, "Appearance")
        self.e_color = QLineEdit()
        self.e_color.setMaximumWidth(110)
        self.e_color.textChanged.connect(self._color_edited)
        self.chip = QLabel()
        self.chip.setFixedSize(48, 22)
        pick, photo = QPushButton("Pick…"), QPushButton("From photo…")
        pick.clicked.connect(self._pick_color)
        photo.clicked.connect(self._pick_from_photo)
        row = QHBoxLayout()
        for w in (self.e_color, self.chip, pick, photo):
            row.addWidget(w)
        row.addStretch(1)
        ap.addRow("Colour", row)
        ap.addRow("", theme.hint("The bulk colour: what a fully opaque slab of it looks like, not "
                                 "what one layer looks like over white."))
        self.c_finish = QComboBox()
        self.c_finish.addItems(sorted(TD_GUESS))
        self.c_finish.activated.connect(lambda *_: self._finish_edited())
        ap.addRow("Finish", self.c_finish)
        ap.addRow("", theme.hint("Only a bookkeeping label — but changing it offers the matching "
                                 "starter td."))

        op = section(col, "Optics")
        self.s_td = spin(0.01, 5.0, 0.01, 0.3, 4)
        self.s_td.valueChanged.connect(self._td_edited)
        guess = QPushButton("Estimate from colour")
        guess.clicked.connect(self._guess_td)
        row = QHBoxLayout()
        row.addWidget(self.s_td)
        row.addWidget(guess)
        row.addStretch(1)
        op.addRow("td (mm)", row)
        op.addRow("", theme.hint(
            "Thickness at which transmittance falls to 1/e (36.8%): T(t) = exp(-t/td). NOT "
            "HueForge's TD scale. Estimating fits this colour against every TD Polymaker publish — "
            "better than a finish-based guess, still no substitute for a wedge."))
        self.k_perch = QCheckBox("per-channel td")
        self.k_perch.toggled.connect(self._toggle_perchannel)
        op.addRow(self.k_perch)
        self.s_tdc = [spin(0.01, 5.0, 0.01, 0.3, 4) for _ in "rgb"]
        row = QHBoxLayout()
        for c, s in zip("RGB", self.s_tdc):
            s.valueChanged.connect(self._tdc_edited)
            row.addWidget(QLabel(c))
            row.addWidget(s)
        row.addStretch(1)
        op.addRow(row)
        op.addRow("", theme.hint("A red that passes red but blocks green and blue cannot be "
                                 "described by one number. If a scalar fit reports a bad residual "
                                 "on clean measurements, that is the signal to switch."))
        self.e_hf = QLineEdit()
        self.e_hf.setMaximumWidth(90)
        conv = QPushButton("Convert")
        conv.clicked.connect(self._import_hueforge)
        row = QHBoxLayout()
        row.addWidget(self.e_hf)
        row.addWidget(conv)
        row.addStretch(1)
        op.addRow("HueForge TD", row)
        op.addRow("", theme.hint("Theirs is closer to 'thickness to opacity'; this divides by 4.6 "
                                 "and marks the entry as vendor data."))

        pv = section(col, "Provenance")
        self.c_prov = QComboBox()
        self.c_prov.addItems(list(PROVENANCE))
        self.c_prov.activated.connect(lambda *_: self._set_attr("provenance", self.c_prov.currentText()))
        pv.addRow("Source", self.c_prov)
        pv.addRow("", theme.hint("'estimated' entries print approximate colours and are dimmed "
                                 "everywhere. Only a calibration fit should make one 'measured'."))
        self.e_measured = self._line("measured_at")
        pv.addRow("Measured at", self.e_measured)
        self.s_lhref = spin(0.0, 0.5, 0.01, 0.0, 2)
        self.s_lhref.valueChanged.connect(lambda v: self._set_attr("layer_height_ref", float(v)))
        pv.addRow("Layer height ref", self.s_lhref)
        self.e_tags = QLineEdit()
        self.e_tags.textChanged.connect(self._tags_edited)
        pv.addRow("Tags", self.e_tags)
        pv.addRow("", theme.hint("Comma separated."))
        self.t_notes = QPlainTextEdit()
        self.t_notes.setFixedHeight(90)
        self.t_notes.textChanged.connect(lambda: self._set_attr("notes", self.t_notes.toPlainText().strip()))
        pv.addRow("Notes", self.t_notes)
        col.addStretch(1)
        return sc

    def _build_bar(self):
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 6, 0, 0)
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        others = []
        for text, fn in (("Revert", self.revert), ("Open…", self.open_db), ("Save as…", self.save_as),
                         ("Starter set…", self.seed)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            others.append(b)
        about = QPushButton("About")
        about.clicked.connect(lambda: QMessageBox.information(self, APP, "Filament library\n\n" + ABOUT))
        self.lbl_path = theme.hint(self.db_path)
        self.lbl_status = QLabel("")
        bar.addWidget(save)
        for b in others:
            bar.addWidget(b)
        bar.addWidget(self.lbl_path, 1)
        bar.addWidget(self.lbl_status)
        bar.addWidget(about)
        self.status("ready")
        return bar

    # -- selection and field binding --------------------------------------------------------

    def fil(self) -> Filament | None:
        return self.db.filaments.get(self.current) if self.current else None

    def select(self, fid):
        if fid not in self.db.filaments:
            return
        self.current = fid
        it = self._item(fid)
        if it is not None and self.list.currentItem() is not it:
            self.list.blockSignals(True)
            self.list.setCurrentItem(it)
            self.list.blockSignals(False)
        self.pages["details"].setEnabled(True)
        self.reload_fields()

    def reload_fields(self):
        """Pull every widget's value from the selected entry (does not mark anything dirty)."""
        fil = self.fil()
        if fil is None:
            return
        self._loading = True
        try:
            for e, v in ((self.e_id, fil.id), (self.e_brand, fil.brand), (self.e_series, fil.series),
                         (self.e_name, fil.name), (self.e_color, fil.color), (self.e_sku, fil.sku),
                         (self.e_measured, fil.measured_at), (self.e_tags, ", ".join(fil.tags or []))):
                e.setText(v)
            self.c_finish.setCurrentText(fil.finish)
            self.s_td.setValue(fil.td)
            self.c_prov.setCurrentText(fil.provenance)
            self.s_lhref.setValue(fil.layer_height_ref or 0.0)
            self.t_notes.setPlainText(fil.notes or "")
            self.k_perch.setChecked(bool(fil.td_rgb))
            for i, s in enumerate(self.s_tdc):
                s.setValue(float(fil.td_rgb[i]) if fil.td_rgb else fil.td)
                s.setEnabled(bool(fil.td_rgb))
        finally:
            self._loading = False
        self._paint_chip()
        self.edited.emit()

    def _set_attr(self, key, value):
        fil = self.fil()
        if fil is None or self._loading:
            return
        setattr(fil, key, value)
        self.touch()

    def touch(self):
        """Mark unsaved and push the change into the list row and the pages."""
        self._set_dirty(True)
        fil = self.fil()
        it = self._item(self.current) if fil else None
        if it is not None:
            self._style_item(it, fil)
        self.status("unsaved changes", theme.WARN)
        self.edited.emit()

    def _set_dirty(self, on):
        if on != self.dirty:
            self.dirty = on
            self.dirty_changed.emit(on)

    def _paint_chip(self):
        fil = self.fil()
        ok = fil is not None and _ok_hex(fil.color)
        self.chip.setText("" if ok else "?")
        self.chip.setAlignment(Qt.AlignCenter)
        self.chip.setStyleSheet(f"background:{colormath.to_hex(fil.rgb()) if ok else theme.BG3};"
                                f"border:1px solid {theme.LINE}")

    def _color_edited(self, text):
        fil = self.fil()
        if fil is None or self._loading:
            return
        if not _ok_hex(text):
            self.chip.setStyleSheet(f"background:{theme.BG3};border:1px solid {theme.LINE}")
            self.chip.setText("?")
            return
        fil.color = colormath.to_hex(colormath.parse_hex(text))
        self._paint_chip()
        self.touch()

    def _td_edited(self, v):
        fil = self.fil()
        if fil is None or self._loading or v <= 0:
            return
        fil.td = round(float(v), 4)
        self.touch()

    def _tdc_edited(self, _v):
        fil = self.fil()
        if fil is None or self._loading or not self.k_perch.isChecked():
            return
        vals = [s.value() for s in self.s_tdc]
        if min(vals) <= 0:
            return
        fil.td_rgb = [round(x, 4) for x in vals]
        self.touch()

    def _tags_edited(self, text):
        self._set_attr("tags", [t.strip() for t in text.split(",") if t.strip()])

    def _finish_edited(self):
        """Changing the finish label is usually a request for its starter td."""
        fil = self.fil()
        if fil is None or self._loading:
            return
        finish = self.c_finish.currentText()
        fil.finish = finish
        self.touch()
        guess = TD_GUESS.get(finish)
        if guess is None or abs(fil.td - guess) < 1e-9 or fil.provenance == "measured":
            return                              # never overwrite a measurement
        if QMessageBox.question(
                self, APP, f"Set td to the starter value for a {finish} filament ({guess} mm)?\n\n"
                           "It is a coarse guess — enough to slice something today, not enough to "
                           "trust the colour.") == QMessageBox.Yes:
            self.s_td.setValue(guess)

    def _toggle_perchannel(self, on):
        fil = self.fil()
        for s in self.s_tdc:
            s.setEnabled(on)
        if fil is None or self._loading:
            return
        if on:                              # start from the scalar td, per channel
            fil.td_rgb = [round(fil.td, 4)] * 3
            self._loading = True
            for s in self.s_tdc:
                s.setValue(fil.td)
            self._loading = False
        else:
            fil.td_rgb = None
        self.touch()

    def _set_td(self, td):
        """Set the scalar td from a tool (estimate, convert) and drop any per-channel one."""
        fil = self.fil()
        self.k_perch.setChecked(False)
        fil.td_rgb = None
        self.s_td.setValue(td)
        fil.td = round(float(td), 4)

    def _note(self, text):
        fil = self.fil()
        if text not in (fil.notes or ""):
            fil.notes = ((fil.notes + " | ").lstrip(" |") + text) if fil.notes else text
            self.t_notes.setPlainText(fil.notes)

    # -- details actions --------------------------------------------------------------------

    def _commit_id(self):
        """Rename, keeping the dict key and the entry's own id in step."""
        fil = self.fil()
        if fil is None or self._loading:
            return
        new = slugify(self.e_id.text())
        if not new or new == fil.id:
            self.e_id.setText(fil.id)
            return
        if new in self.db.filaments:
            QMessageBox.warning(self, APP, f"{new} already exists.")
            self.e_id.setText(fil.id)
            return
        del self.db.filaments[fil.id]
        fil.id = new
        self.db.filaments[new] = fil
        self.current = new
        self._render_list()
        self._set_dirty(True)
        self.e_id.setText(new)
        self.status(f"renamed to {new}", theme.WARN)

    def _reslug(self):
        fil = self.fil()
        if fil is not None:
            self.e_id.setText(slugify(fil.brand, fil.series, fil.name))
            self._commit_id()

    def _pick_color(self):
        c = _pick_color(self, self.e_color.text())
        if c:
            self.e_color.setText(c)

    def _pick_from_photo(self):
        p, _ = QFileDialog.getOpenFileName(self, "Photo of a printed swatch", "", IMAGE_FILTER)
        if not p:
            return
        picked = PhotoPicker.ask(p, self)
        if picked:
            self.e_color.setText(picked)
            self.status(f"colour {picked} sampled from {os.path.basename(p)}", theme.OK)

    # -- Polymaker catalogue ----------------------------------------------------------------

    def _catalog(self):
        """The scraped wiki table, offering to fetch it if it is not there yet."""
        if self._cat is None:
            try:
                self._cat = polymaker.Catalog(self.catalog_path)
            except SystemExit as exc:
                QMessageBox.critical(self, APP, str(exc))
                return None
        if not self._cat.available:
            if QMessageBox.question(
                    self, APP, f"No catalogue at {self.catalog_path}.\n\nDownload Polymaker's hex and "
                               "TD table from their wiki now?") != QMessageBox.Yes:
                return None
            cat = download_catalog(self.catalog_path, self)
            if cat is None:
                return None
            self._cat = cat
            self.status(f"catalogue: {len(cat.products)} products", theme.OK)
        return self._cat

    def _apply_product(self, p, fil) -> bool:
        """Write one catalogue row into `fil` and refresh everything showing it."""
        try:
            _, warnings = polymaker.to_filament(p, fil, fid=fil.id, fetched_at=self._cat.fetched_at)
        except SystemExit as exc:
            QMessageBox.warning(self, APP, str(exc))
            return False
        self.reload_fields()
        self.touch()
        note = f"{p.sku}: {p.name} {fil.color}"
        note += f", td {fil.td} from TD {p.td}" if p.td else ", no TD published"
        if fil.id != slugify(fil.brand, fil.series, fil.name):
            note += f"  ·  id is still {fil.id}"
        self.status(note, theme.WARN if warnings else theme.OK)
        for w in warnings:
            QMessageBox.information(self, APP, w)
        return True

    def _lookup_sku(self):
        fil, sku = self.fil(), self.e_sku.text().strip()
        if fil is None:
            return
        if not sku:
            self._browse_skus()
            return
        cat = self._catalog()
        if cat is None:
            return
        try:
            p = cat.get(sku)
        except SystemExit as exc:
            QMessageBox.warning(self, APP, str(exc))
            return
        self._apply_product(p, fil)

    def _browse_skus(self):
        cat = self._catalog()
        if cat is None:
            return
        p = SkuBrowser.ask(cat, self)
        if p is None:
            return
        # An id derived from the product name is how the CLI import addresses the same filament.
        fid = slugify("Polymaker", p.series(), p.name)
        fil = self.fil()
        if fil is not None and fil.id != fid and QMessageBox.question(
                self, APP, f"{p.sku} is {p.product} — {p.name}.\n\nAdd it as a new entry ({fid})?\n\n"
                           f"No overwrites the selected entry, {fil.label()}, instead.") != QMessageBox.Yes:
            self._apply_product(p, fil)
            return
        created = fid not in self.db.filaments
        if not created:
            if QMessageBox.question(self, APP, f"{fid} already exists. Update it from the catalogue?"
                                    ) != QMessageBox.Yes:
                return
            if (self.db.filaments[fid].provenance == "measured" and QMessageBox.question(
                    self, APP, f"{fid} is marked measured — a wedge was printed for it.\n\n"
                               "Replace those numbers with vendor data?") != QMessageBox.Yes):
                return
            target = self.db.filaments[fid]
        else:
            target = Filament(id=fid)
            self.db.filaments[fid] = target
        self.current = fid
        if self._apply_product(p, target):
            self._render_list()
            self.select(fid)
            self.show_page("details")
        elif created:
            del self.db.filaments[fid]          # never leave a blank entry behind
            self.current = None
            self._render_list()

    def _guess_td(self):
        fil = self.fil()
        cat = self._catalog() if fil is not None else None
        if cat is None:
            return
        try:
            est = cat.estimate_td(fil.color, fil.finish)
        except SystemExit as exc:
            QMessageBox.warning(self, APP, str(exc))
            return
        if fil.provenance == "measured" and QMessageBox.question(
                self, APP, f"{fil.label()} is marked measured — a wedge was printed for it.\n\n"
                           f"Replace td {fil.td} with the guess {est.td}?") != QMessageBox.Yes:
            return
        self._set_td(est.td)
        self._note(f"td estimated from {est.n} published Polymaker {est.group} TDs by {est.method} "
                   f"(typically off by ~{est.typical_factor}x)")
        self.touch()
        name, hx, de, td = est.nearest
        self.status(f"td {est.td} from {est.n} {est.group} filaments ({est.method}); nearest "
                    f"{name} {hx} dE {de:.0f}, TD {td}", theme.WARN)
        self.show_page("look")

    def _import_hueforge(self):
        fil = self.fil()
        if fil is None:
            return
        try:
            hf = float(self.e_hf.text())
        except ValueError:
            QMessageBox.warning(self, APP, "Enter HueForge's TD number first.")
            return
        if hf <= 0:
            return
        td = round(hf / 4.6, 4)
        self._set_td(td)
        fil.provenance = "vendor"
        self.c_prov.setCurrentText("vendor")
        self._note(f"td converted from HueForge TD {hf}")
        self.touch()
        self.status(f"td {td} from HueForge TD {hf}", theme.OK)

    # -- library actions --------------------------------------------------------------------

    def _unique_id(self, stem):
        fid, n = stem, 2
        while fid in self.db.filaments:
            fid, n = f"{stem}-{n}", n + 1
        return fid

    def new_filament(self):
        fid = self._unique_id("new-filament")
        self.db.filaments[fid] = Filament(
            id=fid, brand="Polymaker", series="PLA Pro", name="New filament", color="#808080",
            td=TD_GUESS["opaque"], finish="opaque", provenance="estimated")
        self._set_dirty(True)
        self.filter.clear()
        self._render_list()
        self.select(fid)
        self.show_page("details")
        self.e_name.clear()
        self.e_name.setFocus()
        self.status("new entry — name it, then set its colour", theme.WARN)

    def duplicate(self):
        fil = self.fil()
        if fil is None:
            return
        copy = deepcopy(fil)
        copy.id = self._unique_id(fil.id + "-copy")
        copy.name = (fil.name + " copy").strip()
        self.db.filaments[copy.id] = copy
        self._set_dirty(True)
        self._render_list()
        self.select(copy.id)
        self.status(f"duplicated {fil.id}", theme.WARN)

    def delete(self):
        fil = self.fil()
        if fil is None or QMessageBox.question(
                self, APP, f"Delete {fil.label()} ({fil.id})?\n\nAny command line naming it will "
                           "stop working.") != QMessageBox.Yes:
            return
        order = sorted(self.db.filaments)
        i = order.index(fil.id)
        del self.db.filaments[fil.id]
        order.pop(i)
        self._set_dirty(True)
        self.current = None
        self._render_list()
        if order:
            self.select(order[min(i, len(order) - 1)])
        else:
            self.pages["details"].setEnabled(False)
            self.edited.emit()
        self.status(f"deleted {fil.id}", theme.WARN)

    def seed(self):
        brand, ok = QInputDialog.getText(self, APP, "Brand for the starter set:", text="Polymaker")
        if not ok:
            return
        series, ok = QInputDialog.getText(self, APP, "Series:", text="PLA Pro")
        if not ok:
            return
        n = seed_db(self.db, brand, series)
        if n:
            self._set_dirty(True)
        self._render_list()
        self.status(f"added {n} starter entries (all estimated)", theme.OK if n else theme.FG_DIM)

    # -- persistence ------------------------------------------------------------------------

    def problems(self) -> list[str]:
        bad = []
        for fil in self.db.filaments.values():
            if not _ok_hex(fil.color):
                bad.append(f"{fil.id}: colour {fil.color!r} is not a hex colour")
            if fil.td <= 0:
                bad.append(f"{fil.id}: td must be positive")
            if fil.td_rgb and (len(fil.td_rgb) != 3 or min(fil.td_rgb) <= 0):
                bad.append(f"{fil.id}: td_rgb must be three positive numbers")
        return bad

    def save(self) -> bool:
        bad = self.problems()
        if bad:
            QMessageBox.critical(self, APP, "Not saved:\n\n" + "\n".join(bad))
            return False
        try:
            self.db.save()
        except OSError as exc:
            QMessageBox.critical(self, APP, f"Could not write {self.db.path}:\n{exc}")
            return False
        self._set_dirty(False)
        self._disk = deepcopy(self.db.filaments)
        self.status(f"saved {len(self.db.filaments)} filaments to {os.path.basename(self.db.path)}",
                    theme.OK)
        return True

    def save_as(self):
        p, _ = QFileDialog.getSaveFileName(self, "Save filament database",
                                           os.path.basename(self.db.path), "JSON (*.json)")
        if p:
            self.db.path = self.db_path = p
            self.save()

    def revert(self):
        if self.dirty and QMessageBox.question(
                self, APP, "Discard unsaved edits and reload from disk?") != QMessageBox.Yes:
            return
        self.load(self.db_path)

    def load(self, path):
        try:
            db = DB(path)
        except SystemExit as exc:
            QMessageBox.critical(self, APP, str(exc))
            return
        self.db, self.db_path = db, path
        self._disk = deepcopy(db.filaments)
        self._set_dirty(False)
        self.current = None
        self._render_list()
        if db.filaments:
            self.select(sorted(db.filaments)[0])
        else:
            self.pages["details"].setEnabled(False)
            self.edited.emit()
            self.status("empty database — New, or Starter set…", theme.WARN)
        self.lbl_path.setText(path)

    def merge_from_disk(self) -> list[str]:
        """Re-read the file another tab just wrote, keeping this editor's unsaved edits.

        Each entry the editor changed (or added/deleted) since it last read or wrote the
        file is carried over onto the new file; where the other writer changed that same
        entry, the file wins, since it is the newer measurement. Returns those ids.
        Saving the stale in-memory copy instead would silently undo the other write.
        """
        try:
            disk = DB(self.db_path)
        except SystemExit as exc:
            QMessageBox.critical(self, APP, str(exc))
            return []
        lost = []
        for fid in set(self.db.filaments) | set(self._disk):
            mine, was = self.db.filaments.get(fid), self._disk.get(fid)
            if mine == was:
                continue                                  # not edited here
            if disk.filaments.get(fid) != was:
                lost.append(fid)                          # changed on both sides
            elif mine is None:
                disk.filaments.pop(fid, None)
            else:
                disk.filaments[fid] = mine
        self._disk = deepcopy(DB(self.db_path).filaments)
        self.db.filaments = disk.filaments
        cur = self.current if self.current in self.db.filaments else None
        self.current = None
        self._render_list()
        if cur or self.db.filaments:
            self.select(cur or sorted(self.db.filaments)[0])
        self._set_dirty(self.db.filaments != self._disk)
        return lost

    def open_db(self):
        if not self.confirm_discard():
            return
        p, _ = QFileDialog.getOpenFileName(self, "Open filament database", "", "JSON (*.json)")
        if p:
            self.load(p)

    def confirm_discard(self) -> bool:
        """True if it is safe to throw the current state away."""
        if not self.dirty:
            return True
        r = QMessageBox.question(self, APP, "Save changes to the filament database first?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Cancel:
            return False
        return self.save() if r == QMessageBox.Save else True

    def status(self, msg, colour=theme.FG_DIM):
        self.lbl_status.setText(msg)
        self.lbl_status.setStyleSheet(f"color:{colour}")
        self.lbl_path.setText(self.db_path)


def _ok_hex(s) -> bool:
    try:
        colormath.parse_hex(str(s))
        return True
    except ValueError:
        return False
