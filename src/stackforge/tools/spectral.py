#!/usr/bin/env python3
"""stackforge-spectral -- per-wavelength calibration (Kubelka-Munk K and S) and spectra plots.

The ColorMunki reads a full reflectance spectrum (380-730 nm, 10 nm) for every wedge step and
for the bare-base patch; `stackforge-measure measure-wedge --sheet` already saves them. This
fits each filament's absorption K and scattering S per band from those spectra, so
`stackforge-plaque --optics spectral` can predict stacks wavelength by wavelength instead of
with one td (or three).

    stackforge-spectral fit --readings wedges.readings.json --filament <id> --write
    stackforge-spectral show --filaments <id>,<id> --base <id> --layers 8 --preview spectra.png
    stackforge-spectral demo-readings -o demo.readings.json     # synthetic, for trying it out

WHAT A FIT NEEDS
    The same filament printed as a wedge over two CONTRASTING bases (a white and a black),
    each with its bare-base patch measured: over white a layer's scattering shows, over black
    its absorption does, and only the pair separates K from S. One base, or two similar ones,
    is refused for --write -- the fit would look tight and be wrong.

WHAT A FIT IS NOT
    K and S are effective values for stacks printed the way the wedge was (solid infill, the
    same layer grid, the same top finish), surface reflection included. Kubelka-Munk assumes
    diffuse, homogeneous layers; extruded lines with air gaps may not be. The held-out check
    (each step predicted by a fit that never saw it) is the honest number to look at.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

import numpy as np
from PIL import Image, ImageDraw
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

from stackforge.core import colormath
from stackforge.core import spectral as sp
from stackforge.core import wedgesheet
from stackforge.core.filamentdb import DEFAULT_DB, DB
from stackforge.tools import plaque

MIN_STEPS = 4
MIN_BASE_CONTRAST = 0.25     # mean |R_white - R_black| over the bands
SMOOTH = 0.01                # weight of the second-difference penalty on log K, log S
SCALE_TOL = (0.8, 1.25)      # reading Y / spectrum Y outside this: the scale guess is wrong


# --------------------------------------------------------------------------
# readings -> datasets
# --------------------------------------------------------------------------


def datasets_from_readings(readings: dict, filament_id: str):
    """[(base_id, Rbase (36,), measured (n,36), layers (n,))] for one filament's strips."""
    strips = [s for s in readings["strips"] if s["filament"]["id"] == filament_id]
    if not strips:
        have = sorted({s["filament"]["id"] for s in readings["strips"]})
        raise SystemExit(f"no wedge of {filament_id} in the readings; it has: {', '.join(have)}")
    every = [r for s in readings["strips"] for r in s.get("readings", [])] + \
            [s["base_reading"] for s in readings["strips"] if s.get("base_reading")]
    try:
        scale = sp.spectrum_scale(every)
    except sp.SpectralError as exc:
        raise SystemExit(f"readings have no usable spectra: {exc}. Measure with "
                         f"stackforge-measure (it runs spotread with -s).")
    out, warnings = [], []
    for s in strips:
        title = wedgesheet.strip_title(s)
        if not s.get("base_reading"):
            raise SystemExit(f"{title}: no bare-base patch was measured. The fit needs the base's "
                             f"own spectrum: print the wedge with a base patch (the default).")
        try:
            Rb = sp.reading_spectrum(s["base_reading"], scale)
            M = np.array([sp.reading_spectrum(r, scale) for r in s["readings"]])
        except sp.SpectralError as exc:
            raise SystemExit(f"{title}: {exc}")
        ratio = sp.scale_check(s["base_reading"], scale)
        if ratio is not None and not (SCALE_TOL[0] <= ratio <= SCALE_TOL[1]):
            warnings.append(f"{title}: the reading's own Y is {ratio:.2f}x the Y of its spectrum "
                            f"(expected ~1). The spectrum scale was guessed as 0..{scale:g}; "
                            f"if that is wrong every K and S is wrong.")
        out.append((s["base"]["id"], Rb, M, np.arange(1, len(M) + 1, dtype=float)))
    return out, scale, warnings


# --------------------------------------------------------------------------
# the fit
# --------------------------------------------------------------------------


def predict(K, S, Rb, layers, layer_h):
    R0, T = sp.layer_rt(K[None, :], S[None, :], layers[:, None] * layer_h)
    return sp.over(Rb[None, :], R0, T)


def _initial(datasets):
    thick = np.mean([M[-1] for _, _, M, _ in datasets], axis=0)
    S0 = np.full(sp.NB, 3.0)
    K0 = np.maximum(S0 * sp.ks_ratio(np.clip(thick, 0.01, 0.95)), 1e-4)
    return np.log(np.concatenate([K0, S0]))


def _sparsity(datasets, smooth):
    """Each band's residuals depend on that band's K and S only (plus the smoothing rows)."""
    nb = sp.NB
    rows = sum(len(M) for _, _, M, _ in datasets) * nb + (2 * (nb - 2) if smooth else 0)
    J = lil_matrix((rows, 2 * nb), dtype=int)
    r = 0
    for _, _, M, _ in datasets:
        for _i in range(len(M)):
            for b in range(nb):
                J[r + b, b] = 1
                J[r + b, nb + b] = 1
            r += nb
    if smooth:
        for half in (0, nb):
            for j in range(nb - 2):
                J[r, half + j:half + j + 3] = 1
                r += 1
    return J


def fit_ks(datasets, layer_h, smooth=SMOOTH, x0=None):
    """K, S (36,) each by least squares on reflectance over every step of every strip."""
    nb = sp.NB

    def unpack(p):
        return np.exp(p[:nb]), np.exp(p[nb:])

    def resid(p):
        K, S = unpack(p)
        out = [(predict(K, S, Rb, L, layer_h) - M).ravel() for _, Rb, M, L in datasets]
        if smooth:
            d2 = lambda v: v[2:] - 2 * v[1:-1] + v[:-2]  # noqa: E731
            out += [smooth * d2(p[:nb]), smooth * d2(p[nb:])]
        return np.concatenate(out)

    lo = np.concatenate([np.full(nb, np.log(1e-6)), np.full(nb, np.log(1e-4))])
    hi = np.full(2 * nb, np.log(1e4))
    p0 = np.clip(_initial(datasets) if x0 is None else x0, lo + 1e-9, hi - 1e-9)
    res = least_squares(resid, p0, bounds=(lo, hi), method="trf",
                        jac_sparsity=_sparsity(datasets, smooth), x_scale="jac")
    K, S = unpack(res.x)
    return K, S, res.x


def step_de(K, S, datasets, layer_h):
    """Per strip: dE (CIELAB under D50, the meter's convention) of every step's prediction."""
    out = []
    for _, Rb, M, L in datasets:
        P = predict(K, S, Rb, L, layer_h)
        out.append(np.linalg.norm(sp.spectrum_to_lab(P, "D50") - sp.spectrum_to_lab(M, "D50"), axis=-1))
    return out


def held_out_de(datasets, layer_h, smooth, x0):
    """Each step predicted by a fit that never saw it: the honest accuracy."""
    out = []
    for i, (bid, Rb, M, L) in enumerate(datasets):
        des = []
        for j in range(len(M)):
            keep = np.arange(len(M)) != j
            sub = [d if k != i else (bid, Rb, M[keep], L[keep]) for k, d in enumerate(datasets)]
            K, S, _ = fit_ks(sub, layer_h, smooth, x0)
            P = predict(K, S, Rb, L[j:j + 1], layer_h)
            des.append(float(np.linalg.norm(sp.spectrum_to_lab(P[0], "D50")
                                            - sp.spectrum_to_lab(M[j], "D50"))))
        out.append(np.array(des))
    return out


def base_contrast(datasets) -> float:
    if len(datasets) < 2:
        return 0.0
    bases = [Rb for _, Rb, _, _ in datasets]
    return max(float(np.abs(a - b).mean()) for i, a in enumerate(bases) for b in bases[i + 1:])


# --------------------------------------------------------------------------
# plotting (PIL, so the CLI needs no GUI toolkit)
# --------------------------------------------------------------------------


def plot_curves(path, curves, title, swatches=None, note=None, points=None):
    """curves: [(label, R (36,), rgb tuple, width)]; swatches: [(label, [hex...], current)]."""
    W, H, L, R, T, B = 900, 560, 70, 20, 50, 70
    sw_h = 26 * len(swatches or [])
    img = Image.new("RGB", (W, H + sw_h + 10), (30, 30, 34))
    d = ImageDraw.Draw(img)
    f, fs = plaque._font(16), plaque._font(12)
    pw, ph = W - L - R, H - T - B
    x = lambda nm: L + (nm - sp.NM_FROM) / (sp.NM_TO - sp.NM_FROM) * pw  # noqa: E731
    y = lambda v: T + (1 - v) * ph  # noqa: E731
    d.text((L, 14), title, fill=(232, 232, 238), font=f)
    for v in np.linspace(0, 1, 6):
        d.line([(L, y(v)), (L + pw, y(v))], fill=(58, 58, 68))
        d.text((L - 40, y(v) - 7), f"{v:.1f}", fill=(154, 154, 166), font=fs)
    for nm in range(400, 731, 50):
        d.text((x(nm) - 12, T + ph + 26), str(nm), fill=(154, 154, 166), font=fs)
    band = sp.wavelength_srgb(np.linspace(sp.NM_FROM, sp.NM_TO, pw))
    for i, c in enumerate(band):
        d.line([(L + i, T + ph + 6), (L + i, T + ph + 18)], fill=tuple(int(v) for v in c))
    d.text((L + pw / 2 - 40, T + ph + 44), "wavelength, nm", fill=(154, 154, 166), font=fs)
    d.text((8, T + ph / 2), "R", fill=(154, 154, 166), font=f)
    for label, Rv, rgb, width in curves:
        pts = [(x(nm), y(min(max(v, 0), 1))) for nm, v in zip(sp.GRID, Rv)]
        d.line(pts, fill=rgb, width=width, joint="curve")
    for rgb, Rv in points or []:
        for nm, v in zip(sp.GRID, Rv):
            d.ellipse([x(nm) - 2.5, y(v) - 2.5, x(nm) + 2.5, y(v) + 2.5], outline=rgb)
    yy = H + 4
    for label, hexes, cur in swatches or []:
        d.text((8, yy + 4), label[:10], fill=(154, 154, 166), font=fs)
        cw = (W - 100) / max(len(hexes), 1)
        for i, hx in enumerate(hexes):
            box = [90 + i * cw, yy, 90 + (i + 1) * cw - 2, yy + 22]
            d.rectangle(box, fill=hx, outline=(255, 255, 255) if i == cur else None,
                        width=2 if i == cur else 0)
        yy += 26
    if note:
        d.text((W - R - d.textlength(note, font=f) - 4, 14), note, fill=(214, 91, 91), font=f)
    img.save(path)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_fit(args):
    try:
        readings = wedgesheet.load_readings(args.readings)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc))
    layer_h = args.layer_height or float(readings.get("layer_height") or 0)
    if layer_h <= 0:
        raise SystemExit("the readings carry no layer height; pass --layer-height")
    data, scale, warnings = datasets_from_readings(readings, args.filament)
    synthetic = "SYNTHETIC" in str((readings.get("meter") or {}).get("note", ""))

    print(f"{args.filament}: {len(data)} wedge(s), "
          f"{', '.join(f'{len(M)} steps over {bid}' for bid, _, M, _ in data)}; "
          f"layer {layer_h:g} mm; spectra on a 0..{scale:g} scale")
    if synthetic:
        print("  (SYNTHETIC readings from demo-readings: this is a test of the fit, not a calibration)")
    note = sp.scale_note(scale)
    if note:
        print(f"  ! {note}")
    for w in warnings:
        print(f"  ! {w}")

    K, S, x = fit_ks(data, layer_h, args.smooth)
    des = step_de(K, S, data, layer_h)
    allde = np.concatenate(des)
    rinf = sp.r_inf(K, S)
    print(f"\nfit: bulk colour {sp.spectrum_to_hex(rinf)} (D65)")
    for (bid, Rb, M, L), de in zip(data, des):
        print(f"  over {bid} ({sp.spectrum_to_hex(Rb)}): dE per step "
              + " ".join(f"{v:.1f}" for v in de))
    print(f"  in-sample dE (D50): mean {allde.mean():.2f}, max {allde.max():.2f}")
    print("  band    K/mm     S/mm    R_inf")
    for b in range(0, sp.NB, 5):
        print(f"  {sp.GRID[b]:4.0f}  {K[b]:7.3f}  {S[b]:7.3f}   {rinf[b]:.3f}")

    ho = None
    if not args.no_holdout:
        print("\nheld-out check (each step predicted by a fit that never saw it)...")
        ho = np.concatenate(held_out_de(data, layer_h, args.smooth, x))
        print(f"  held-out dE: mean {ho.mean():.2f}, max {ho.max():.2f}"
              + ("   <- the fit does not generalise; re-measure or check the wedge" if ho.mean() > 3 else ""))

    contrast = base_contrast(data)
    refuse = []
    if len(data) < 2:
        refuse.append("only one base: K and S cannot be told apart. Print the wedge over a white "
                      "AND a black base")
    elif contrast < MIN_BASE_CONTRAST:
        refuse.append(f"the two bases differ by only {contrast:.2f} in reflectance (need "
                      f"{MIN_BASE_CONTRAST}): use a white and a black base")
    if min(len(M) for _, _, M, _ in data) < MIN_STEPS:
        refuse.append(f"fewer than {MIN_STEPS} steps on a wedge")
    if warnings:
        refuse.append("the spectrum scale does not match the readings' own XYZ (see above)")

    if args.preview:
        curves, pts = [], []
        for (bid, Rb, M, L) in data:
            P = predict(K, S, Rb, L, layer_h)
            for i, Pi in enumerate(P):
                col = tuple(colormath.parse_hex(sp.spectrum_to_hex(Pi)))
                curves.append((f"{i}", Pi, col, 1))
                pts.append((col, M[i]))
        curves.append(("R_inf", rinf, (232, 232, 238), 2))
        plot_curves(args.preview, curves, f"{args.filament}: fitted model (lines) vs measured (dots)",
                    note="SYNTHETIC" if synthetic else None, points=pts)
        print(f"\nplot -> {args.preview}")

    if refuse:
        for r in refuse:
            print(f"\n  !! {r}")
        if args.write:
            print("  Refusing --write.")
        return 1
    if not args.write:
        print("\n(dry run -- pass --write to save K and S into the database)")
        return 0
    db = DB(args.db)
    fil = db.get(args.filament)
    fil.spectral = sp.record(
        K, S, measured_at=date.today().isoformat(), source=os.path.basename(args.readings),
        layer_height_ref=layer_h, n_steps=[len(M) for _, _, M, _ in data],
        bases=[bid for bid, _, _, _ in data], de_mean=round(float(allde.mean()), 3),
        de_held_out=round(float(ho.mean()), 3) if ho is not None else None,
        **({"synthetic": True} if synthetic else {}))
    # `color` is an input of the RGB model; a spectral fit leaves it alone.
    db.save()
    print(f"\nwrote the spectral calibration of {fil.id} to {db.path}")
    return 0


def _resolve(db, ids, demo):
    if demo:
        pool = {f.id: f for f in sp.demo_filaments()}
        missing = [i for i in ids if i not in pool]
        if missing:
            raise SystemExit(f"--demo filaments are {', '.join(pool)}; not {', '.join(missing)}")
        return [pool[i] for i in ids], pool
    fils = [db.get(i) for i in ids]
    sp.require_calibrated(fils, "show")
    return fils, db.filaments


def _base_spectrum(arg, pool):
    if arg in (None, "white"):
        return np.full(sp.NB, 0.9), "ideal white"
    if arg == "black":
        return np.full(sp.NB, 0.03), "ideal black"
    f = pool.get(arg)
    if f is None:
        try:
            rgb = colormath.parse_hex(arg)
        except ValueError:
            raise SystemExit(f"--base {arg}: not a filament, a hex colour, or white / black")
        # A colour has no spectrum: take it as a neutral grey of the same lightness.
        y = float(colormath.srgb_to_linear(np.array(rgb, float)) @ colormath._M_RGB2XYZ[1])
        return np.full(sp.NB, y), f"neutral grey matching {arg} (Y {y:.2f})"
    sp.require_calibrated([f], "--base")
    return sp.r_inf(*sp.ks(f)), f.label()


def cmd_show(args):
    ids = [i.strip() for i in args.filaments.split(",") if i.strip()]
    if not ids:
        if not args.demo:
            raise SystemExit("--filaments is required (or pass --demo)")
        ids = ["demo-orange", "demo-blue"]
    db = None if args.demo else DB(args.db)
    fils, pool = _resolve(db, ids, args.demo)
    Rb, bname = _base_spectrum(args.base, pool if args.demo else (db.filaments if db else {}))
    curves, swatches = [], []
    print(f"over {bname}, {args.layer_height:g} mm layers, seen under {args.illuminant}:")
    for f in fils:
        R = sp.stack_spectra(f, Rb, args.layers, args.layer_height)
        hexes = [sp.spectrum_to_hex(r, args.illuminant) for r in R]
        print(f"  {f.label():32} " + " ".join(hexes))
        col = tuple(colormath.parse_hex(sp.colour_of(f, args.illuminant)))
        for i, r in enumerate(R[1:], 1):
            curves.append((f"{f.name} {i}", r, tuple(int(c * (0.35 + 0.65 * i / args.layers)) for c in col),
                           2 if i == args.layers else 1))
        swatches.append((f.name, hexes, args.layers))
    if args.preview:
        plot_curves(args.preview, curves, f"{args.layers} layers over {bname} ({args.illuminant})",
                    swatches, note="SYNTHETIC" if args.demo else None)
        print(f"plot -> {args.preview}")


def demo_readings(filament_id="demo-orange", steps=10, layer_h=0.08, noise=0.003, seed=0,
                  bases=("demo-white", "demo-black")) -> dict:
    """A readings file in measure's --sheet format, from synthetic K/S plus meter noise."""
    pool = {f.id: f for f in sp.demo_filaments()}
    if filament_id not in pool:
        raise SystemExit(f"demo filaments are {', '.join(pool)}")
    rng = np.random.default_rng(seed)
    fil = pool[filament_id]
    K, S = sp.ks(fil)

    def reading(R):
        R = np.clip(R + rng.normal(0, noise, sp.NB), 0.001, 1.2)
        xyz = sp.spectrum_to_xyz(R, "D50") * 100
        srgb = sp.spectrum_to_srgb(R, "D50")
        return {"xyz": [round(float(v), 4) for v in xyz],
                "spectrum": {"nm_from": sp.NM_FROM, "nm_to": sp.NM_TO,
                             "values": [round(float(v) * 100, 4) for v in R]},
                "srgb": [round(float(v), 1) for v in srgb]}

    strips = []
    for k, bid in enumerate(bases):
        Rb = sp.r_inf(*sp.ks(pool[bid]))
        base = reading(Rb)
        rs = [reading(predict(K, S, Rb, np.array([float(n)]), layer_h)[0]) for n in range(1, steps + 1)]
        to_hex = lambda r: "#%02X%02X%02X" % tuple(int(round(v)) for v in np.clip(r["srgb"], 0, 255))  # noqa: E731
        strips.append({
            "wedge": "AB"[k], "where": ("front wedge", "back wedge")[k],
            "base": wedgesheet.filament_ref(pool[bid]), "filament": wedgesheet.filament_ref(fil),
            "hex": ",".join(to_hex(r) for r in rs), "readings": rs, "reversed_steps": [],
            "base_hex": to_hex(base), "base_reading": base})
    return {"kind": wedgesheet.READINGS_KIND, "version": wedgesheet.VERSION,
            "model": "synthetic", "layer_height": layer_h, "first_layer_height": 0.2,
            "steps": steps, "base_patch": True, "sheet": "synthetic", "strips": strips,
            "meter": {"tool": "stackforge-spectral demo-readings", "mode": "reflective",
                      "note": "SYNTHETIC: generated from demo K/S, not measured", "at": ""}}


def cmd_demo_readings(args):
    data = demo_readings(args.filament, args.steps, args.layer_height, args.noise, args.seed)
    wedgesheet.write(args.output, data)
    print(f"wrote {args.output}: SYNTHETIC wedges of {args.filament} over demo white and black, "
          f"{args.steps} steps, noise {args.noise}")
    print(f"Try: stackforge-spectral fit --readings {args.output} --filament {args.filament} --preview fit.png")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Spectral calibration (Kubelka-Munk K and S per 10 nm band) and spectra plots.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fit", help="fit K and S from a measured wedge readings file",
                       formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--readings", required=True,
                   help="*.readings.json from stackforge-measure measure-wedge --sheet")
    p.add_argument("--filament", required=True, help="filament id whose wedges to fit")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--layer-height", type=float, default=None,
                   help="default: the readings file's layer height")
    p.add_argument("--smooth", type=float, default=SMOOTH,
                   help="how strongly neighbouring bands are tied together (0 = independent)")
    p.add_argument("--no-holdout", action="store_true", help="skip the held-out check (faster)")
    p.add_argument("--preview", "--plot", dest="preview", help="PNG of model vs measured spectra")
    p.add_argument("--write", action="store_true", help="save K and S into the database")
    p.set_defaults(func=cmd_fit)

    p = sub.add_parser("show", help="spectra of 0..N layers of calibrated filaments",
                       formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--filaments", default="",
                   help="comma-separated spectrally calibrated filament ids "
                        "(with --demo, default: demo-orange,demo-blue)")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--base", default="white",
                   help="what the layers sit on: a calibrated filament id, white / black, or a "
                        "hex colour (taken as a neutral grey of its lightness)")
    p.add_argument("--layers", type=int, default=8)
    p.add_argument("--layer-height", type=float, default=0.08)
    p.add_argument("--illuminant", choices=list(sp.ILLUMINANTS), default="D65")
    p.add_argument("--preview", "--plot", dest="preview", help="PNG of the spectra and swatches")
    p.add_argument("--demo", action="store_true",
                   help="use the built-in SYNTHETIC filaments (demo-white, demo-orange, ...)")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("demo-readings", help="write a SYNTHETIC readings file to try the fit on",
                       formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--filament", default="demo-orange")
    p.add_argument("--steps", type=int, default=10)
    p.add_argument("--layer-height", type=float, default=0.08)
    p.add_argument("--noise", type=float, default=0.003, help="reflectance noise per band")
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(func=cmd_demo_readings)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    for path in (getattr(args, "preview", None), getattr(args, "output", None)):
        if path and not os.path.isdir(os.path.dirname(os.path.abspath(path))):
            raise SystemExit(f"{path}: directory does not exist")
    rc = args.func(args)
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    sys.exit(main())
