"""What layers of a filament look like over a base: the numbers behind the editor's previews.

Same single-pass model as plaque: T = exp(-t/td) per channel, then alpha-over with
alpha = 1-T, composited in linear light. No GUI toolkit needed.
"""
from __future__ import annotations

import numpy as np

from stackforge.core import colormath
from stackforge.core.filamentdb import Filament

# What the preview composites over. Two contrasting backings, because that is exactly the pair
# that pins a td down -- a filament whose td is wrong usually still looks plausible over one.
PREVIEW_BASES = [("over white", "#F4F5F0"), ("over black", "#1A1A1C")]


def opaque_layers(fil: Filament, first_layer: float, layer: float, t_max: float = 0.01):
    """(layers, mm) of `fil` for it to pass at most `t_max` of the light: an opaque backing.

    The first layer is `first_layer` thick, every other one `layer`.
    """
    mm = float(fil.td_vec().max()) * np.log(1 / t_max)
    return 1 + int(np.ceil(max(0.0, mm - first_layer) / layer - 1e-9)), mm


def patch_rgb(fil: Filament, base_lin, n_layers: int, layer_h: float, td=None) -> np.ndarray:
    """sRGB of `n_layers` of `fil` over a base given in linear light (`td` overrides fil.td)."""
    if td is None:
        T = fil.transmittance(n_layers * layer_h)
    else:
        T = np.exp(-(n_layers * layer_h) / td)
    return colormath.linear_to_srgb(base_lin * T + fil.linear() * (1 - T))


def stack_colors(fil: Filament, base_hex: str, layers: int, layer_h: float) -> np.ndarray:
    """sRGB of 0..layers layers of `fil` laid over `base_hex`."""
    base = colormath.srgb_to_linear(np.array(colormath.parse_hex(base_hex), float))
    return np.array([patch_rgb(fil, base, n, layer_h) for n in range(layers + 1)])


def opaque_at(fil: Filament, layer_h: float, limit=64) -> int | None:
    """First layer count whose transmittance drops under 1% on every channel."""
    for n in range(1, limit + 1):
        if fil.transmittance(n * layer_h).max() < 0.01:
            return n
    return None


def best_layers(fil, base_hex, layer_h=0.08, factor=1.6, options=(1, 2, 3, 4, 6, 8, 12, 16, 24)):
    """Layer count where a wrong td shows up most against this base.

    A single layer is the sharpest test for an opaque filament and useless for a translucent
    one -- black is already opaque at one layer, while natural barely absorbs anything until it
    is millimetres thick. So the useful thickness has to be chosen per filament.
    """
    base = colormath.srgb_to_linear(np.array(colormath.parse_hex(base_hex), float))

    def lab(td, n):
        return colormath.srgb_to_lab(patch_rgb(fil, base, n, layer_h, td))

    best, best_de = options[0], -1.0
    for n in options:
        ref = lab(fil.td, n)
        de = 0.5 * (np.linalg.norm(lab(fil.td * factor, n) - ref)
                    + np.linalg.norm(lab(fil.td / factor, n) - ref))
        if de > best_de:
            best, best_de = n, float(de)
    return best, best_de


def optics_lines(fil: Filament, layer_h: float):
    """The transmittance table as (text, kind) lines; kind is head/dim/ok/warn/err or ''."""
    try:
        tdv = fil.td_vec()
    except SystemExit as exc:
        return [(str(exc), "err")]
    out = [(f"{fil.label()}   {fil.color}   td {np.round(tdv, 4).tolist()}", "head"), ("", ""),
           ("  layers      mm     transmittance R,G,B", "dim")]
    for n in (1, 2, 4, 8, 16, 24):
        tr = fil.transmittance(n * layer_h)
        opaque = tr.max() < 0.01
        out.append((f"  {n:6d}  {n * layer_h:6.2f}     {tr[0]:.4f} {tr[1]:.4f} {tr[2]:.4f}"
                    f"{'   opaque' if opaque else ''}", "ok" if opaque else ""))
    if fil.provenance != "measured":
        out += [("", ""), ("  These numbers are a guess, not a measurement. Print a wedge from "
                           "the Calibrate tab.", "warn")]
    return out
