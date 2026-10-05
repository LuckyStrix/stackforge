"""Modal helpers: sample a colour from a photo, browse and download Polymaker's catalogue."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QEventLoop, QThread, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QProgressDialog, QPushButton, QVBoxLayout)

from stackforge.core import colormath
from stackforge.core.filamentdb import hueforge_td
from stackforge.gui import theme
from stackforge.gui.filaments import APP
from stackforge.gui.imageview import to_qimage
from stackforge.gui.pickers import swatch_icon
from stackforge.tools import polymaker


def readable_on(rgb) -> str:
    """Black or white text, whichever reads on this background."""
    r, g, b = (float(v) for v in rgb)
    return "#000000" if 0.299 * r + 0.587 * g + 0.114 * b > 140 else "#ffffff"


class _Photo(QLabel):
    """A scaled photo that reports clicks in source-pixel coordinates."""
    picked = Signal(int, int)

    def __init__(self, src: np.ndarray, max_w=880, max_h=620):
        super().__init__()
        h, w = src.shape[:2]
        self.scale = min(max_w / w, max_h / h, 1.0)
        pm = QPixmap.fromImage(to_qimage(src.astype(np.uint8))).scaled(
            max(1, int(w * self.scale)), max(1, int(h * self.scale)), Qt.KeepAspectRatio,
            Qt.SmoothTransformation)
        self.setPixmap(pm)
        self.setFixedSize(pm.size())
        self.setCursor(Qt.CrossCursor)

    def _emit(self, ev):
        p = ev.position()
        self.picked.emit(int(p.x() / self.scale), int(p.y() / self.scale))

    mousePressEvent = mouseMoveEvent = _emit


class PhotoPicker(QDialog):
    """Click a photo of a printed swatch to lift its colour.

    Averages a small patch rather than the pixel under the cursor, because print texture and
    JPEG noise make one pixel meaningless.
    """

    def __init__(self, path, parent=None, patch=7):
        super().__init__(parent)
        self.setWindowTitle("Pick a colour")
        self.result_hex = None
        self.patch = patch
        self.src = np.asarray(colormath.open_image(path), dtype=np.float64)
        lay = QVBoxLayout(self)
        photo = _Photo(self.src)
        photo.picked.connect(self._pick)
        lay.addWidget(photo)
        bar = QHBoxLayout()
        self.chip = QLabel("  click the swatch  ")
        self.chip.setMinimumWidth(150)
        self.chip.setAlignment(Qt.AlignCenter)
        bar.addWidget(self.chip)
        bar.addWidget(theme.hint(f"averages a {patch}x{patch} px patch"), 1)
        self.ok = QPushButton("Use this colour")
        self.ok.setObjectName("primary")
        self.ok.setEnabled(False)
        self.ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        bar.addWidget(cancel)
        bar.addWidget(self.ok)
        lay.addLayout(bar)

    def _pick(self, x, y):
        h, w = self.src.shape[:2]
        r = self.patch // 2
        y0, y1, x0, x1 = max(0, y - r), min(h, y + r + 1), max(0, x - r), min(w, x + r + 1)
        if y1 <= y0 or x1 <= x0:
            return
        rgb = self.src[y0:y1, x0:x1].reshape(-1, 3).mean(0)
        self.result_hex = colormath.to_hex(rgb)
        self.chip.setText(self.result_hex)
        self.chip.setStyleSheet(f"background:{self.result_hex};color:{readable_on(rgb)};padding:4px")
        self.ok.setEnabled(True)

    @classmethod
    def ask(cls, path, parent=None) -> str | None:
        d = cls(path, parent)
        return d.result_hex if d.exec() == QDialog.Accepted else None


class _Refresh(QThread):
    done = Signal(object, str)

    def __init__(self, path):
        super().__init__()
        self.path = path

    def run(self):
        try:
            self.done.emit(polymaker.refresh(self.path, verbose=False), "")
        except BaseException as exc:      # SystemExit included
            self.done.emit(None, str(exc))


class SkuBrowser(QDialog):
    """Search Polymaker's published hex/TD table and pick a product from it."""

    def __init__(self, catalog, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Polymaker catalogue")
        self.resize(720, 560)
        self.cat = catalog
        self.result_product = None
        self._worker = None
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("search SKU, product or colour")
        self.q.textChanged.connect(self._render)
        refresh = QPushButton("Refresh from wiki")
        refresh.clicked.connect(self._refresh)
        top.addWidget(self.q, 1)
        top.addWidget(refresh)
        lay.addLayout(top)
        self.note = theme.hint("")
        lay.addWidget(self.note)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._select)
        self.list.itemDoubleClicked.connect(lambda *_: self._accept())
        lay.addWidget(self.list, 1)
        self.status = QLabel("")
        lay.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.use = self.buttons.addButton("Use this product", QDialogButtonBox.AcceptRole)
        self.use.setObjectName("primary")
        self.use.setEnabled(False)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._render()
        self.q.setFocus()

    @staticmethod
    def usable(p) -> bool:
        return bool(p.hexes) and not p.dual

    def _render(self):
        self.note.setText(
            f"{len(self.cat.products)} products scraped {self.cat.fetched_at or 'at some point'}. "
            "Polymaker publish a TD for only some of them; the rest give you a colour and leave "
            "the td a guess.")
        q = self.q.text().strip()
        hits = self.cat.search(q, limit=200) if q else list(self.cat.products.values())[:200]
        self.list.clear()
        for p in hits:
            td = f"TD {p.td}" if p.td else "TD —"
            txt = f"{p.sku}   {p.product} — {p.name}   ·   {td}"
            if p.dual:
                txt += "   (two colours, not importable)"
            elif not p.hexes:
                txt += "   (no hex published)"
            it = QListWidgetItem(txt)
            if p.hexes:
                it.setIcon(swatch_icon(p.hex, 26, 16))
            if not self.usable(p):
                it.setForeground(Qt.gray)
            it.setData(Qt.UserRole, p)
            self.list.addItem(it)
        self.status.setText(f"{len(hits)} shown" + ("  (first 200)" if len(hits) == 200 else ""))

    def _select(self, item, _prev=None):
        p = item.data(Qt.UserRole) if item else None
        self.use.setEnabled(p is not None and self.usable(p))
        if p is not None:
            td = (f"TD {p.td} → td {hueforge_td(p.td):.4f} mm" if p.td
                  else "no TD published — td stays a guess")
            self.status.setText(f"{p.sku}  {p.hex or 'no hex'}   {td}")

    def _accept(self):
        item = self.list.currentItem()
        p = item.data(Qt.UserRole) if item else None
        if p is not None and self.usable(p):
            self.result_product = p
            self.accept()

    def _refresh(self):
        self.status.setText("downloading the wiki page…")
        self.use.setEnabled(False)
        self._worker = _Refresh(self.cat.path)
        self._worker.done.connect(self._refreshed)
        self._worker.start()

    def _refreshed(self, cat, err):
        if err:
            self.status.setText("refresh failed")
            QMessageBox.critical(self, APP, err)
            return
        self.cat = cat
        self._render()
        self.status.setText(f"{len(cat.products)} products, fetched {cat.fetched_at}")

    def done(self, r):
        if self._worker is not None:
            self._worker.wait(2000)
        super().done(r)

    @classmethod
    def ask(cls, catalog, parent=None):
        d = cls(catalog, parent)
        return d.result_product if d.exec() == QDialog.Accepted else None


def download_catalog(path, parent=None):
    """Fetch Polymaker's table on a worker behind a busy dialog; the Catalog, or None on failure."""
    dlg = QProgressDialog("Downloading the Polymaker wiki…", None, 0, 0, parent)
    dlg.setWindowTitle(APP)
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setMinimumDuration(0)
    box = {}
    worker = _Refresh(path)
    loop = QEventLoop()

    def finished(cat, err):
        box["cat"], box["err"] = cat, err
        loop.quit()

    worker.done.connect(finished)
    worker.start()
    dlg.show()
    loop.exec()
    worker.wait(2000)
    dlg.close()
    if box.get("err"):
        QMessageBox.critical(parent, APP, f"Could not fetch the catalogue:\n{box['err']}")
        return None
    return box.get("cat")
