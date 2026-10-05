"""Paint tab: colour an existing 3D model. Three modes over the generated forms.

*Realistic colour* is paint with the model's own colours (GLB texture) or a wrapped
image, dithered onto your filaments. *Project from above* is top_paint (an image on the
top-visible surface). *Decorative pattern* is the rest of paint (checkers, stripes,
expressions): it makes no attempt to look like anything. Which parameters a pattern uses is
a table in argform.overrides.
"""
from __future__ import annotations

from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QRadioButton, QStackedWidget, QVBoxLayout, QWidget

from stackforge.gui.argform.spec import introspect
from stackforge.gui import theme
from stackforge.gui.panel import ToolPanel
from stackforge.tools import paint, top_paint

REALISTIC = ("texture", "image-spherical", "image-cylindrical", "image-planar")


def _surface_parser(realistic: bool):
    """paint's parser with --pattern narrowed to the choices for this mode."""
    def build():
        ap = paint.build_parser()
        act = next(a for a in ap._actions if a.dest == "pattern")
        if realistic:
            act.choices, act.default = list(REALISTIC), "texture"
        else:
            act.choices = [c for c in act.choices if c not in REALISTIC]
            act.required = True
        return ap
    return build


MODES = (("paint", "Realistic colour", _surface_parser(True)),
         ("top_paint", "Project image from above", top_paint.build_parser),
         ("decorative", "Decorative pattern", _surface_parser(False)))

HINTS = {"paint": "GLB: uses the model's own texture or vertex colours, dithered onto your filaments",
         "top_paint": "paints one image onto whatever faces up; ignores the model's own colours",
         "decorative": "checkers, stripes and expressions; not meant to look like anything"}


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
            rb.setToolTip(HINTS[key])
            self.group.addButton(rb, i)
            bar.addWidget(rb)
            tool = "top_paint" if key == "top_paint" else "paint"
            self.panels[key] = ToolPanel(introspect(builder(), tool), tool, project=project, presets=presets)
            self.stack.addWidget(self.panels[key])
        self.group.button(0).setChecked(True)
        self.group.idToggled.connect(lambda i, on: on and self.stack.setCurrentIndex(i))
        bar.addStretch(1)
        bar.addWidget(theme.hint("--expr is evaluated as Python: trusted input only"))
        lay.addLayout(bar)
        lay.addWidget(self.stack, 1)

    def set_mode(self, key: str):
        self.group.button([m[0] for m in MODES].index(key)).setChecked(True)
