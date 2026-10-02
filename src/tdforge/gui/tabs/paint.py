"""Paint tab: colour an existing 3MF. Two modes over the generated forms.

*Project from above* is topdeco (an image on the top-visible surface); *Pattern or wrapped
image* is surfacecolor. Which parameters a pattern uses is a table in argform.overrides.
"""
from __future__ import annotations

from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QRadioButton, QStackedWidget, QVBoxLayout, QWidget

from tdforge.gui.argform.spec import introspect
from tdforge.gui import theme
from tdforge.gui.panel import ToolPanel
from tdforge.tools import surfacecolor, topdeco

MODES = (("topdeco", "Project from above", topdeco.build_parser),
         ("surfacecolor", "Pattern or wrapped image", surfacecolor.build_parser))


class PaintTab(QWidget):
    title = "Paint"

    def __init__(self, project=None, presets=None, host=None):
        super().__init__()
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.stack = QStackedWidget()
        self.panels = {}
        self.group = QButtonGroup(self)
        for i, (key, label, builder) in enumerate(MODES):
            rb = QRadioButton(label)
            self.group.addButton(rb, i)
            bar.addWidget(rb)
            self.panels[key] = ToolPanel(introspect(builder(), key), key, project=project, presets=presets)
            self.stack.addWidget(self.panels[key])
        self.group.button(0).setChecked(True)
        self.group.idToggled.connect(lambda i, on: on and self.stack.setCurrentIndex(i))
        bar.addStretch(1)
        bar.addWidget(theme.hint("--expr is evaluated as Python: trusted input only"))
        lay.addLayout(bar)
        lay.addWidget(self.stack, 1)

    def set_mode(self, key: str):
        self.group.button([m[0] for m in MODES].index(key)).setChecked(True)
