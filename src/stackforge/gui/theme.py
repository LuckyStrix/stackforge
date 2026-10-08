"""Dark Fusion theme and a few small layout helpers for the Qt GUI."""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QWidget

BG = "#1e1e22"
BG2 = "#26262c"
BG3 = "#2e2e36"
FG = "#e8e8ee"
FG_DIM = "#9a9aa6"
ACCENT = "#5b8dd6"
WARN = "#d6a25b"
ERR = "#d65b5b"
OK = "#6dbd7a"
LINE = "#3a3a44"

# Filament provenance (filamentdb.PROVENANCE): bright when measured, dim when guessed.
PROVENANCE_COLOUR = {"measured": FG, "matched": OK, "vendor": ACCENT, "estimated": FG_DIM}

STYLE = f"""
QToolTip {{ background: {BG3}; color: {FG}; border: 1px solid {LINE}; }}
QGroupBox {{ border: 1px solid {LINE}; border-radius: 4px; margin-top: 14px; padding: 8px 6px 6px 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {ACCENT}; }}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {BG3}; border: 1px solid {LINE}; border-radius: 3px; padding: 3px 5px;
    selection-background-color: {ACCENT}; }}
QLineEdit:read-only {{ color: {FG_DIM}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {FG_DIM}; background: {BG2}; }}
QPushButton {{ background: {BG3}; border: 1px solid {LINE}; border-radius: 3px; padding: 4px 12px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {FG_DIM}; background: {BG2}; }}
QPushButton#primary {{ background: {ACCENT}; color: #0d1420; border-color: {ACCENT}; }}
QPushButton#primary:disabled {{ background: {BG3}; color: {FG_DIM}; border-color: {LINE}; }}
QTabWidget::pane {{ border: 1px solid {LINE}; top: -1px; }}
QTabBar::tab {{ background: {BG2}; padding: 6px 14px; border: 1px solid {LINE}; border-bottom: none; }}
QTabBar::tab:selected {{ background: {BG}; color: {FG}; }}
QTabBar::tab:!selected {{ color: {FG_DIM}; }}
QToolButton#section {{ border: none; color: {ACCENT}; font-weight: bold; text-align: left; padding: 4px 2px; }}
QLabel#hint {{ color: {FG_DIM}; font-size: 11px; }}
QLabel#warn {{ color: {WARN}; }}
QScrollArea {{ border: none; }}
QSplitter::handle {{ background: {LINE}; }}
"""


def apply(app: QApplication):
    app.setStyle("Fusion")
    p = QPalette()
    for role, col in ((QPalette.Window, BG), (QPalette.Base, BG3), (QPalette.AlternateBase, BG2),
                      (QPalette.Button, BG3), (QPalette.WindowText, FG), (QPalette.Text, FG),
                      (QPalette.ButtonText, FG), (QPalette.ToolTipBase, BG3),
                      (QPalette.ToolTipText, FG), (QPalette.Highlight, ACCENT),
                      (QPalette.HighlightedText, "#0d1420"), (QPalette.PlaceholderText, FG_DIM)):
        p.setColor(role, QColor(col))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(FG_DIM))
    app.setPalette(p)
    app.setStyleSheet(STYLE)


def hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("hint")
    lbl.setWordWrap(True)
    return lbl


def hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet(f"color: {LINE}")
    return f


def mono(widget: QWidget, size: int = 9):
    from PySide6.QtGui import QFontDatabase
    f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    f.setPointSize(size)
    widget.setFont(f)
