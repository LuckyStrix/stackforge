"""Semantic widget kinds argparse cannot express.

`resolve_kind(tool, command, field)` returns one of KINDS. Order: explicit table, then
filename heuristics, then the plain kind implied by argparse (entry / spin / combo / check /
list / repeat), so an unlisted new flag always gets a sensible widget.
"""
from __future__ import annotations

import re

from stackforge.gui.argform.spec import FieldSpec

# semantic kinds (hand-built widgets live in gui/pickers)
SEMANTIC = {"file_in", "file_out", "image", "model_3mf", "filament_ids", "filament_id",
            "filament_or_hex", "hex_list", "color", "hide"}
# plain kinds derived from argparse itself
PLAIN = {"entry", "int", "float", "check", "combo", "list", "repeat"}
PROJECT_PREFIX = "project:"
KINDS = SEMANTIC | PLAIN

# (tool, command path or "*", dest) -> section title, for flags argparse does not group
GROUPS: dict = {
    ("measure", "*", "nospos"): "Instrument",
    ("measure", "*", "spotread_arg"): "Instrument",
}

# (tool, command path "a b" or "*", dest) -> kind
OVERRIDES: dict = {
    ("plaque", "*", "image"): "image",
    ("plaque", "*", "filaments"): "filament_ids",
    ("plaque", "*", "base"): "filament_id",
    ("top_paint", "*", "model"): "model_3mf",
    ("top_paint", "*", "image"): "image",
    ("top_paint", "*", "palette"): "hex_list",
    ("top_paint", "*", "filaments"): "filament_ids",
    ("paint", "*", "model"): "model_3mf",
    ("paint", "*", "image"): "image",
    ("paint", "*", "palette"): "hex_list",
    ("paint", "*", "filaments"): "filament_ids",
    ("calibrate", "*", "filament"): "filament_id",
    ("calibrate", "wedge", "base"): "filament_id",
    ("calibrate", "fit", "base"): "filament_or_hex",
    ("calibrate", "fit", "base2"): "filament_or_hex",
    ("calibrate", "fit", "measured"): "hex_list",
    ("calibrate", "fit", "measured2"): "hex_list",
    ("calibrate", "fit", "from_image"): "image",
    ("calibrate", "fit", "from_image2"): "image",
    ("dither_compare", "*", "image"): "image",
    ("dither_compare", "*", "filaments"): "filament_ids",
    ("dither_compare", "*", "base"): "filament_id",
    ("measure", "verify-plaque", "predicted"): "hex_list",
    ("filamentdb", "show", "id"): "filament_id",
    ("filamentdb", "set", "id"): "filament_id",
    ("filamentdb", "rm", "id"): "filament_id",
    ("filamentdb", "import-hueforge", "id"): "filament_id",
    ("filamentdb", "add", "color"): "color",
    ("filamentdb", "set", "color"): "color",
    ("polymaker", "guess-td", "filament"): "filament_ids",
    ("make_samples", "*", "out_dir"): "file_out",
}

# (tool, dest) -> (controlling dest, values of it for which this field applies). argparse does
# not encode that --scale only matters to checker3d, so it is a table here.
VISIBLE_WHEN: dict = {
    ("paint", "scale"): ("pattern", {"checker3d"}),
    ("paint", "lat"): ("pattern", {"checker-sphere"}),
    ("paint", "lon"): ("pattern", {"checker-sphere"}),
    ("paint", "axis"): ("pattern", {"stripes", "gradient", "image-planar"}),
    ("paint", "period"): ("pattern", {"stripes"}),
    ("paint", "image"): ("pattern", {"image-planar", "image-cylindrical", "image-spherical"}),
    ("paint", "lon_offset"): ("pattern", {"image-cylindrical", "image-spherical"}),
    ("paint", "expr"): ("pattern", {"expr"}),
    ("paint", "dither"): ("pattern", {"texture", "image-planar", "image-cylindrical", "image-spherical"}),
    ("paint", "dither_strength"): ("pattern", {"texture", "image-planar", "image-cylindrical", "image-spherical"}),
}

# dest -> project binding key; filled from the project bar, excluded from presets
PROJECT_BOUND = {
    "db": "db", "template": "template", "flavor": "flavor", "part_type": "part_type",
    "layer_height": "layer_height", "first_layer_height": "first_layer",
    "catalog": "catalog",
}

_OUT_HINTS = ("output", "preview", "sheet")
_IN_HINTS = ("template", "model")


# dest -> label, where "Capitalised words" from the flag would read badly
LABELS = {
    "db": "Database", "id": "ID", "td": "td (mm)", "td_rgb": "Per-channel td (R G B)",
    "output": "Output file", "preview": "Preview image", "gamut_preview": "Gamut preview image",
    "rank_sheet": "Ranking sheet", "sheet": "Contact sheet", "model": "3D model (3MF or GLB)",
    "image": "Image", "filaments": "Filaments", "palette": "Palette (colours)",
    "base": "Base filament", "base2": "Second base", "template": "Template project",
    "first_layer_height": "First layer height (mm)", "layer_height": "Layer height (mm)",
    "resolution": "Resolution (mm)", "depth": "Depth (mm)", "width": "Width (mm)",
    "height": "Height (mm)", "scale": "Cell size (mm)", "period": "Stripe period (mm)",
    "lon_offset": "Rotation (degrees)", "scale_to": "Scale to size (mm, GLB)",
    "dither": "Dither", "dither_strength": "Dither strength", "expr": "Expression (trusted input only)",
    "sku": "Polymaker SKU", "nospos": "No dial check (patched Argyll)",
    "spotread_arg": "Extra spotread arguments", "measured_at": "Measured on (date)",
    "from_image": "Photo of the wedge", "from_image2": "Photo of the second wedge",
    "measured": "Measured colours (thinnest first)", "measured2": "Second wedge colours",
    "per_channel": "Fit each colour channel", "write": "Save the fit to the database",
}


def label_for(f: FieldSpec) -> str:
    """A readable field label; the raw flag is the tooltip."""
    if f.dest in LABELS:
        return LABELS[f.dest]
    return f.dest.replace("_", " ").capitalize()


def clean_help(text: str) -> str:
    """Help text without argparse's "(default: ...)": the field already shows its default."""
    return re.sub(r"\s*\(default: [^)]*\)", "", text).strip()


def group_for(tool: str, command: str, f: FieldSpec) -> str:
    for key in ((tool, command, f.dest), (tool, "*", f.dest)):
        if key in GROUPS:
            return GROUPS[key]
    return f.group


def project_key(field: FieldSpec):
    return PROJECT_BOUND.get(field.dest)


def resolve_kind(tool: str, command: str, f: FieldSpec) -> str:
    for key in ((tool, command, f.dest), (tool, "*", f.dest)):
        if key in OVERRIDES:
            return OVERRIDES[key]
    pk = project_key(f)
    if pk:
        return PROJECT_PREFIX + pk
    if f.kind == "str":
        if any(h in f.dest for h in _OUT_HINTS):
            return "file_out"
        if f.dest in _IN_HINTS:
            return "file_in"
    return plain_kind(f)


def plain_kind(f: FieldSpec) -> str:
    if f.kind == "bool":
        return "check"
    if f.kind == "choice":
        return "combo"
    if f.kind == "append":
        return "repeat"
    if f.nargs is not None:
        return "list"
    if f.kind in ("int", "float"):
        return f.kind
    return "entry"


def is_project_kind(kind: str) -> bool:
    return kind.startswith(PROJECT_PREFIX)


def kind_known(kind: str) -> bool:
    return kind in KINDS or is_project_kind(kind)
