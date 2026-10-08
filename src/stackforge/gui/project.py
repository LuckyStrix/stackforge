"""Project: the settings the project bar edits, as the one source for project-bound fields.

Layer height / first layer are derived from the template (threemf.template_layer_settings)
unless explicitly overridden, so no tab has its own layer-height box unless unlocked: the
layer grid must match the slicer profile.
"""
from __future__ import annotations

import os

from stackforge.core import threemf
from stackforge.core.filamentdb import DEFAULT_DB
from stackforge.core.paths import data_path
from stackforge.gui.settings import Settings

KEYS = ("db", "template", "catalog", "flavor", "part_type", "layer_height", "first_layer")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt(x) -> str:
    return f"{x:g}"


def _ver(app):
    v = threemf._version_of(app)
    try:
        return tuple(int(p) for p in v.split(".")) if v else None
    except ValueError:
        return None


class Project:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self._subs = []
        self._cache = (None, None)      # (template path, mtime) -> derived

    # ---- change notification ----------------------------------------------------------
    def subscribe(self, cb):
        self._subs.append(cb)

    def set(self, key, value):
        self.settings.set(key, value)
        for cb in list(self._subs):
            cb()

    # ---- derived values ---------------------------------------------------------------
    def derived_layers(self):
        """(layer_height, first_layer) read from the template, or (None, None)."""
        t = self.settings.get("template")
        if not t or not os.path.exists(t):
            return None, None
        return threemf.template_layer_settings(t)

    @property
    def override(self) -> bool:
        return bool(self.settings.get("layer_override"))

    def layers(self):
        """(layer_height, first_layer) in effect: the override if on, else the template's."""
        if self.override:
            return _num(self.settings.get("layer_height")), _num(self.settings.get("first_layer"))
        return self.derived_layers()

    def get(self, key):
        """Value for a project-bound field as text, or None to leave the field to the user."""
        if key == "db":
            return self.settings.get("db") or os.path.abspath(DEFAULT_DB)
        if key == "catalog":
            return self.settings.get("catalog") or os.path.abspath(
                os.environ.get("POLYMAKER_CATALOG") or data_path("polymaker_catalog.json"))
        if key in ("template", "flavor", "part_type"):
            return self.settings.get(key) or None
        if key in ("layer_height", "first_layer"):
            lh, fl = self.layers()
            v = lh if key == "layer_height" else fl
            return _fmt(v) if v is not None else None
        return None

    def warning(self):
        """A short note when the template looks unusable or older than the slicer we know."""
        t = self.settings.get("template")
        if not t:
            if (self.settings.get("flavor") or "orca") == "orca" and self.layers()[0] is None:
                return ("Choose your Flash Studio project file above (\u201cChoose…\u201d). "
                        "It sets the layer height; nothing can be exported without it.")
            return None
        if not os.path.exists(t):
            return "Your slicer project file was moved or deleted: choose it again."
        try:
            threemf.check_template(t)
        except SystemExit:
            return ("That file is not a slicer project: in Flash Studio use File > Save Project "
                    "As…, not Export.")
        app = threemf._template_app(t)
        have, want = _ver(app), _ver(threemf.ORCA_APP)
        if have is None:
            return "template has no readable Application version: slicer may drop the parts"
        if want and have < want:
            return (f"template written by {app}, older than {threemf.ORCA_APP}: re-export it "
                    "from your current slicer or it may load geometry only")
        return None
