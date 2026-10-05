"""ImageView: one image scaled to fit, aspect kept, with a placeholder when empty."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from stackforge.gui import theme


def to_qimage(img) -> QImage:
    """numpy (h, w, 3|4) uint8 or a PIL image -> QImage (copied, so the source can go)."""
    if not isinstance(img, np.ndarray):
        img = np.asarray(img.convert("RGBA"))
    img = np.ascontiguousarray(img.astype(np.uint8))
    h, w, c = img.shape
    fmt = QImage.Format_RGB888 if c == 3 else QImage.Format_RGBA8888
    return QImage(img.data, w, h, w * c, fmt).copy()


class ImageView(QWidget):
    def __init__(self, placeholder="nothing yet", parent=None):
        super().__init__(parent)
        self._pix: QPixmap | None = None
        self._placeholder = placeholder
        self.setMinimumSize(120, 90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_image(self, img):
        self._pix = None if img is None else QPixmap.fromImage(to_qimage(img))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.BG))
        if self._pix is None:
            p.setPen(QColor(theme.FG_DIM))
            p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, self._placeholder)
            return
        box = self.rect().adjusted(8, 8, -8, -8)
        pw, ph = self._pix.width(), self._pix.height()
        scale = min(box.width() / pw, box.height() / ph)
        w, h = max(1, int(pw * scale)), max(1, int(ph * scale))
        target = QRectF(box.x() + (box.width() - w) / 2, box.y() + (box.height() - h) / 2, w, h)
        p.setRenderHint(QPainter.SmoothPixmapTransform, scale < 2)
        p.drawPixmap(target, self._pix, QRectF(self._pix.rect()))
