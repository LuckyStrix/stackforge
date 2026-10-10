"""Spectral colour: CIE colorimetry and a Kubelka-Munk layer model, per wavelength band.

The RGB model in plaque treats a layer as grey see-through paint with one td (or three). Real
filament is a coloured filter: orange passes red and stops blue, so orange over blue goes olive,
not purple. Here every filament carries an absorption K and a scattering S per 10 nm band
(380-730 nm, the ColorMunki/spotread grid), fitted from measured wedge spectra
(`stackforge-spectral fit`), and a stack is a reflectance *spectrum* until the last moment, when
it is integrated against the CIE 1931 observer under a chosen illuminant.

KUBELKA-MUNK, TWO-FLUX
    A layer of thickness h (mm) with K, S (1/mm) over a background of reflectance Rg reflects

        R = R0 + T^2 Rg / (1 - R0 Rg)

    where R0 and T are the layer's own reflectance and transmittance over black:

        a = 1 + K/S,  b = sqrt(a^2 - 1),  x = b S h
        R0 = sinh x / (a sinh x + b cosh x),   T = b / (a sinh x + b cosh x)

    The state of a stack is its reflectance spectrum alone: what lies below matters only
    through Rg. No surface (Saunderson) term: K and S are fitted to what the meter reads, so
    they are effective values that already include the surface, and they are only valid for
    stacks measured the same way (solid infill, same layer grid, same finish).

Pure numpy. No GUI toolkit.
"""
from __future__ import annotations

import csv
import functools

import numpy as np

from stackforge.core import colormath
from stackforge.core.paths import packaged

NM_FROM, NM_TO, NM_STEP = 380.0, 730.0, 10.0
GRID = np.arange(NM_FROM, NM_TO + NM_STEP / 2, NM_STEP)        # (36,)
NB = len(GRID)
ILLUMINANTS = ("D65", "D50", "A")
MODEL = "km-2flux"

# Smallest S used anywhere: S -> 0 is the pure-absorber limit, reached continuously.
S_MIN = 1e-9


class SpectralError(ValueError):
    """A spectrum or a spectral record that cannot be used."""


# --------------------------------------------------------------------------
# CIE tables
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _table(name: str) -> np.ndarray:
    rows = []
    with open(packaged(f"cie/{name}"), newline="", encoding="utf-8") as fh:
        for r in csv.reader(fh):
            if r and r[0].strip():
                rows.append([float(v) if v.strip().lower() != "nan" else 0.0 for v in r])
    return np.array(rows)


def _on_grid(t: np.ndarray) -> np.ndarray:
    """Rows of a 1 nm table at GRID (exact ordinates; the tables are 1 nm)."""
    out = np.empty((NB, t.shape[1] - 1))
    for k in range(1, t.shape[1]):
        out[:, k - 1] = np.interp(GRID, t[:, 0], t[:, k])
    return out


@functools.lru_cache(maxsize=None)
def cmf() -> np.ndarray:
    """CIE 1931 2 degree colour-matching functions at GRID, (36, 3)."""
    return _on_grid(_table("CIE_xyz_1931_2deg.csv"))


@functools.lru_cache(maxsize=None)
def illuminant(name: str) -> np.ndarray:
    """Relative spectral power of an illuminant at GRID, (36,)."""
    if name == "D65":
        return _on_grid(_table("CIE_std_illum_D65.csv"))[:, 0]
    if name == "D50":
        return _on_grid(_table("CIE_std_illum_D50.csv"))[:, 0]
    if name == "A":                                  # CIE 015: Planck at 2856 K, 100 at 560 nm
        c2 = 1.4388e7                                 # nm K
        lam = GRID
        return 100.0 * (560.0 / lam) ** 5 * np.expm1(c2 / (560.0 * 2856.0)) / np.expm1(c2 / (lam * 2856.0))
    raise SpectralError(f"unknown illuminant {name!r}; choose {', '.join(ILLUMINANTS)}")


@functools.lru_cache(maxsize=None)
def _xyz_weights(illum: str) -> np.ndarray:
    """(36, 3): R @ W is XYZ with the perfect white at Y = 1 under `illum`."""
    s = illuminant(illum)
    w = s[:, None] * cmf()
    return w / w[:, 1].sum()


def white_xyz(illum: str = "D65") -> np.ndarray:
    return np.ones(NB) @ _xyz_weights(illum)


_BRADFORD = np.array([[0.8951, 0.2664, -0.1614],
                      [-0.7502, 1.7135, 0.0367],
                      [0.0389, -0.0685, 1.0296]])


def _adapt(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Bradford chromatic adaptation matrix taking white `src` to white `dst`."""
    cs, cd = _BRADFORD @ src, _BRADFORD @ dst
    return np.linalg.inv(_BRADFORD) @ np.diag(cd / cs) @ _BRADFORD


@functools.lru_cache(maxsize=None)
def _linear_matrix(illum: str) -> np.ndarray:
    """(36, 3): reflectance -> linear sRGB, as seen by an eye adapted to `illum`.

    The illuminant's own white is adapted onto colormath's D65 white, so a perfect reflector
    is exactly (1, 1, 1) under every illuminant. What changes between lights is everything
    that is not neutral -- which is what a metamerism check wants to see.
    """
    m = colormath._M_XYZ2RGB @ _adapt(white_xyz(illum), colormath.D65)
    return _xyz_weights(illum) @ m.T


def spectrum_to_xyz(R, illum: str = "D65") -> np.ndarray:
    """Reflectance (..., 36) -> XYZ under `illum`, white at Y = 1 (no adaptation)."""
    return np.asarray(R, dtype=np.float64) @ _xyz_weights(illum)


def spectrum_to_linear(R, illum: str = "D65") -> np.ndarray:
    """Reflectance (..., 36) -> linear sRGB (D65-adapted), unclipped."""
    return np.asarray(R, dtype=np.float64) @ _linear_matrix(illum)


def spectrum_to_srgb(R, illum: str = "D65") -> np.ndarray:
    return colormath.linear_to_srgb(spectrum_to_linear(R, illum))


def spectrum_to_hex(R, illum: str = "D65") -> str:
    return colormath.to_hex(np.round(spectrum_to_srgb(R, illum)))


def spectrum_to_lab(R, illum: str = "D65") -> np.ndarray:
    """CIELAB relative to `illum`'s own white -- the meter's convention under D50."""
    xyz = spectrum_to_xyz(R, illum)
    t = xyz / white_xyz(illum)
    eps, k = 216 / 24389, 24389 / 27
    f = np.where(t > eps, np.cbrt(np.clip(t, 0, None)), (k * t + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])], axis=-1)


def wavelength_srgb(nm) -> np.ndarray:
    """Display colour of monochromatic light (for axis bands): clipped, max-normalised sRGB."""
    nm = np.atleast_1d(np.asarray(nm, dtype=np.float64))
    t = _table("CIE_xyz_1931_2deg.csv")
    xyz = np.stack([np.interp(nm, t[:, 0], t[:, k]) for k in (1, 2, 3)], -1)
    lin = np.clip(xyz @ colormath._M_XYZ2RGB.T, 0, None)
    lin = lin / np.maximum(lin.max(-1, keepdims=True), 1e-9)
    # Fade the ends of the visible range rather than showing them at full brightness.
    fade = np.clip(np.minimum((nm - 380) / 40, (730 - nm) / 40), 0.15, 1.0)[:, None]
    return colormath.linear_to_srgb(lin * fade)


# --------------------------------------------------------------------------
# Kubelka-Munk
# --------------------------------------------------------------------------


def layer_rt(K, S, h):
    """(R0, T) of a layer of thickness h over black, per band. Stable at both limits."""
    K = np.maximum(np.asarray(K, dtype=np.float64), 0.0)
    S = np.maximum(np.asarray(S, dtype=np.float64), S_MIN)
    h = np.asarray(h, dtype=np.float64)
    q = K / S
    a = 1.0 + q
    b = np.sqrt(q * (q + 2.0))
    x = b * S * h
    th = np.tanh(x)
    # tanh(x)/b -> S h as b -> 0 (the pure-scatterer limit), so never divide by a tiny b.
    tb = np.where(b > 1e-7, th / np.where(b > 1e-7, b, 1.0), S * h)
    sech = 2.0 * np.exp(-x) / (1.0 + np.exp(-2.0 * x))
    den = a * tb + 1.0
    return tb / den, sech / den


def over(Rg, R0, T):
    """Reflectance of a layer (R0, T) laid over a background of reflectance Rg."""
    Rg = np.asarray(Rg, dtype=np.float64)
    return R0 + T * T * Rg / (1.0 - R0 * Rg)


def layer(Rg, K, S, h):
    return over(Rg, *layer_rt(K, S, h))


def r_inf(K, S):
    """Reflectance of an infinitely thick layer."""
    q = np.maximum(np.asarray(K, float), 0.0) / np.maximum(np.asarray(S, float), S_MIN)
    return 1.0 + q - np.sqrt(q * (q + 2.0))


def ks_ratio(R):
    """K/S of a layer whose infinite-thickness reflectance is R."""
    R = np.clip(np.asarray(R, dtype=np.float64), 1e-4, 1.0)
    return (1.0 - R) ** 2 / (2.0 * R)


def stack(Rbase, layers):
    """Reflectance of `layers` [(K, S, h), ...] laid bottom-up over `Rbase`."""
    R = np.asarray(Rbase, dtype=np.float64)
    for K, S, h in layers:
        R = layer(R, K, S, h)
    return R


def transmittance(K, S, h):
    return layer_rt(K, S, h)[1]


def backing(K, S, h):
    """How much of what lies under a layer still shows, per band: the reflectance over a
    perfect white backing minus over a black one, T^2 / (1 - R0).

    This is what the RGB model's `T <= 1%` opacity rule means (its td is the reflectance-fit
    kind, so the round trip is already in it). KM's own T is one pass; the backing shows
    through T twice and bounces off the layer's underside, so testing T alone is far stricter.
    """
    R0, T = layer_rt(K, S, h)
    return T * T / (1.0 - R0)


IDEAL_BASES = {"white": 0.9, "black": 0.03}


def ideal_base(name: str) -> np.ndarray:
    """A flat reflectance standing in for a white or black backing (`IDEAL_BASES`)."""
    return np.full(NB, IDEAL_BASES[name])


# --------------------------------------------------------------------------
# filament records
# --------------------------------------------------------------------------


def record(K, S, **meta) -> dict:
    """The `Filament.spectral` block for fitted K, S on GRID."""
    K, S = np.asarray(K, float), np.asarray(S, float)
    if K.shape != (NB,) or S.shape != (NB,):
        raise SpectralError(f"K and S must have {NB} bands")
    return {"model": MODEL, "nm_from": NM_FROM, "nm_step": NM_STEP,
            "K": [round(float(v), 6) for v in K], "S": [round(float(v), 6) for v in S], **meta}


def is_calibrated(fil) -> bool:
    try:
        ks(fil)
    except SpectralError:
        return False
    return True


def ks(fil):
    """(K, S) arrays of a filament's spectral record. SpectralError if it has none."""
    sp = getattr(fil, "spectral", None)
    if not sp:
        raise SpectralError(f"{fil.id} has no spectral calibration")
    if sp.get("model", MODEL) != MODEL:
        raise SpectralError(f"{fil.id}: unknown spectral model {sp.get('model')!r}")
    if abs(float(sp.get("nm_from", NM_FROM)) - NM_FROM) > 1e-6 or \
            abs(float(sp.get("nm_step", NM_STEP)) - NM_STEP) > 1e-6:
        raise SpectralError(f"{fil.id}: spectral record is not on the {NM_FROM:g}-{NM_TO:g} nm "
                            f"/ {NM_STEP:g} nm grid")
    K, S = np.asarray(sp.get("K", []), float), np.asarray(sp.get("S", []), float)
    if K.shape != (NB,) or S.shape != (NB,) or not (np.isfinite(K).all() and np.isfinite(S).all()) \
            or (K < 0).any() or (S <= 0).any():
        raise SpectralError(f"{fil.id}: spectral record needs {NB} finite K >= 0 and S > 0")
    return K, S


def require_calibrated(fils, what="--optics spectral"):
    """SystemExit naming every filament without a spectral calibration."""
    missing = [f for f in fils if not is_calibrated(f)]
    if missing:
        names = "\n".join(f"    {f.id}  ({f.label()})" for f in missing)
        raise SystemExit(
            f"{what} only works with spectrally calibrated filaments; these have no "
            f"calibration:\n{names}\n  Print a wedge set over a white and a black base, "
            f"measure it with `stackforge-measure measure-wedge --sheet`, then run "
            f"`stackforge-spectral fit --readings <file> --filament <id> --write`.")


def colour_of(fil, illum="D65") -> str:
    """Hex of a filament's infinitely thick (bulk) colour under `illum`."""
    return spectrum_to_hex(r_inf(*ks(fil)), illum)


def stack_spectra(fil, Rbase, n_layers: int, layer_h: float) -> np.ndarray:
    """(n_layers + 1, 36): 0..n layers of `fil` over `Rbase`."""
    K, S = ks(fil)
    R0, T = layer_rt(K, S, layer_h)
    out = [np.asarray(Rbase, float)]
    for _ in range(n_layers):
        out.append(over(out[-1], R0, T))
    return np.array(out)


def opaque_layers(fil, first_layer: float, layer_h: float, t_max: float = 0.01, max_mm=50.0):
    """(layers, mm) for what is under `fil` to show by at most `t_max` in its worst band
    (`backing`, the same meaning as the RGB rule)."""
    K, S = ks(fil)
    lo, hi = 0.0, max_mm
    if backing(K, S, hi).max() > t_max:
        mm = float("inf")
    else:
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            lo, hi = (mid, hi) if backing(K, S, mid).max() > t_max else (lo, mid)
        mm = hi
    if not np.isfinite(mm):
        return 10 ** 6, mm
    return 1 + int(np.ceil(max(0.0, mm - first_layer) / layer_h - 1e-9)), mm


# --------------------------------------------------------------------------
# meter readings
# --------------------------------------------------------------------------


def raw_spectrum(reading):
    """(wavelengths, values) of a reading's spectrum, or SpectralError."""
    sp = (reading or {}).get("spectrum")
    if not sp:
        raise SpectralError("reading has no spectrum (was spotread run with -s?)")
    try:
        vals = np.asarray(sp.get("values", []), dtype=np.float64)
        lo, hi = float(sp["nm_from"]), float(sp["nm_to"])
    except (AttributeError, KeyError, TypeError, ValueError):
        raise SpectralError("reading's spectrum is malformed (needs nm_from, nm_to and values)")
    if vals.ndim != 1 or len(vals) < 2 or not np.isfinite(vals).all():
        raise SpectralError("reading's spectrum is malformed")
    wl = np.linspace(lo, hi, len(vals))
    if wl[0] > NM_FROM + 0.5 or wl[-1] < NM_TO - 0.5:
        raise SpectralError(f"spectrum covers {wl[0]:g}-{wl[-1]:g} nm; need "
                            f"{NM_FROM:g}-{NM_TO:g}")
    return wl, vals


def spectrum_scale(readings) -> float:
    """100 if these readings are on spotread's 0..100 scale, else 1.

    Argyll documents reflective spectra as percent (0..100), so that is expected; 1 is taken only
    when no value in the whole set exceeds 1.5, and `scale_note` then says so. Decided over the
    whole set, never per patch: a very dark patch on the 0..100 scale reads below 1 and would
    otherwise be taken as a 0..1 value 100 times too bright. Unverified on hardware, so the
    readings' own XYZ is used as a cross-check (`scale_check`).
    """
    top = max(float(raw_spectrum(r)[1].max()) for r in readings)
    return 100.0 if top > 1.5 else 1.0


def readings_scale(data) -> float:
    """`spectrum_scale` over every step and bare-base reading of a readings file."""
    every = [r for s in data["strips"] for r in s.get("readings", [])] + \
            [s["base_reading"] for s in data["strips"] if s.get("base_reading")]
    return spectrum_scale(every)


def scale_note(scale: float):
    """A warning when the readings are not on spotread's documented 0..100 scale, else None."""
    if scale == 100.0:
        return None
    return ("no spectrum value exceeds 1.5, so these readings were taken as 0..1, not the 0..100 "
            "percent spotread documents for reflective spectra")


def layer_mismatch(fils, layer_h: float):
    """Why each filament's K/S may not hold at `layer_h` (fitted on another layer grid)."""
    out = []
    for f in fils:
        ref = (getattr(f, "spectral", None) or {}).get("layer_height_ref")
        if ref and abs(float(ref) - layer_h) > 1e-6:
            out.append(f"{f.id} was calibrated on {float(ref):g} mm layers, not {layer_h:g} mm: "
                       f"its K and S are effective values for that grid")
    return out


def reading_spectrum(reading, scale: float) -> np.ndarray:
    """A reading's reflectance on GRID, 0..1."""
    wl, vals = raw_spectrum(reading)
    R = np.interp(GRID, wl, vals) / scale
    if not (R > 0).any():
        raise SpectralError("spectrum is all zero (a stale meter reads zeros)")
    return np.clip(R, 0.0, 1.5)


def scale_check(reading, scale: float):
    """Ratio of the reading's own Y (D50, /100) to Y from its spectrum: ~1 when consistent."""
    xyz = reading.get("xyz")
    if not xyz:
        return None
    y_spec = float(spectrum_to_xyz(reading_spectrum(reading, scale), "D50")[1])
    return (float(xyz[1]) / 100.0) / y_spec if y_spec > 1e-6 else None


# --------------------------------------------------------------------------
# synthetic filaments (demo and tests only; never written to a library)
# --------------------------------------------------------------------------


def _sig(lo, hi, centre, width):
    return lo + (hi - lo) / (1.0 + np.exp(-(GRID - centre) / width))


def _bump(lo, hi, centre, width):
    return lo + (hi - lo) * np.exp(-0.5 * ((GRID - centre) / width) ** 2)


# (id, name, bulk reflectance R_inf, scattering S per mm). S is set so a 0.08 mm layer is
# as see-through as printed PLA: about 60% of red light still passes 0.24 mm of orange,
# the owl plaque's photo fit (2026-10-10). White scatters harder, as TiO2 does.
_DEMO = [
    ("demo-white", "White", lambda: _sig(0.80, 0.90, 420, 15), 15.0),
    ("demo-black", "Black", lambda: np.full(NB, 0.03), 12.0),
    ("demo-orange", "Orange", lambda: _sig(0.04, 0.88, 585, 12), 2.5),
    ("demo-blue", "Blue", lambda: np.maximum(_bump(0.04, 0.55, 455, 30), _sig(0.0, 0.10, 700, 10)), 2.5),
    ("demo-yellow", "Yellow", lambda: _sig(0.06, 0.88, 510, 10), 2.5),
    ("demo-red", "Red", lambda: _sig(0.04, 0.85, 610, 9), 2.5),
    ("demo-green", "Green", lambda: _bump(0.05, 0.50, 530, 35), 2.5),
    ("demo-magenta", "Magenta", lambda: 0.05 + _bump(0, 0.45, 430, 25) + _sig(0, 0.75, 620, 12), 2.5),
]


def demo_filaments():
    """Synthetic spectrally calibrated filaments -- clearly labelled, for demo and tests."""
    from stackforge.core.filamentdb import Filament

    out = []
    for fid, name, rinf, s in _DEMO:
        R = np.clip(rinf(), 0.01, 0.95)
        S = np.full(NB, s)
        K = S * ks_ratio(R)
        f = Filament(id=fid, brand="SYNTHETIC", series="demo", name=name,
                     color=spectrum_to_hex(R), td=0.3, provenance="estimated",
                     notes="synthetic demo spectrum, not a measurement", tags=["synthetic"])
        f.spectral = record(K, S, source="synthetic demo", measured_at="")
        out.append(f)
    return out
