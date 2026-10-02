#!/usr/bin/env python3
"""calibrate -- measure a filament's real color and transmission distance.

Two steps, with a print in between.

  1. `wedge` builds a step-wedge 3MF: a staircase of 1..N layers of the test
     filament, laid over an opaque base. Print it, ideally over BOTH a white
     and a black base -- one wedge on each. Two backgrounds pin down the
     filament's own color and its opacity independently, which a single
     background cannot do.

  2. `fit` takes the colors you read off the printed wedge and solves for the
     td (and, if you give it enough steps, the per-channel td_rgb) that best
     explains them, then writes the result into the filament database.

Reading the colors: a spectrophotometer is ideal, but a phone photo under flat
indirect daylight with a white card in frame works well enough -- sample the
middle of each step, average a patch, and white-balance against the card.

    calibrate.py wedge --filament teal --base white -o wedge_teal.3mf
    calibrate.py chips --filament teal -o chips_teal.3mf     # transmission, see munki.py
    calibrate.py fit --filament teal --base "#F4F5F0" \
        --measured "#D8E6E4,#B4D2D0,#8FBEBC,#6FADAB,#54A09E,#3E9694,#2C8E8C,#1F8886"
    calibrate.py fit --filament teal --base "#F4F5F0" --from-image shot.png
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
from scipy.optimize import least_squares

from tdforge.core import td3mf
from tdforge.core import tdcolor
from tdforge.core.filamentdb import DB, Filament


# --------------------------------------------------------------------------
# step wedge geometry
# --------------------------------------------------------------------------


def build_wedge(steps, layer_h, base_layers, step_w, step_d, gap, rows=1, row_gap=4.0):
    """A staircase: step i carries i+1 layers of the test filament.

    `rows` puts several test filaments on one plate, one staircase each, over a
    shared base. On an independent-toolhead machine that is free -- the base
    takes one head and each test filament takes another, so a four-head printer
    calibrates three filaments per print instead of one.
    """
    base_h = base_layers * layer_h
    total_w = steps * step_w + (steps - 1) * gap
    total_d = rows * step_d + (rows - 1) * row_gap
    plate = td3mf.Item(
        "wedge_base",
        td3mf.box_verts(0, 0, 0, total_w, total_d, base_h),
        td3mf.BOX_TRIS.copy(),
    )
    decals = []
    for r in range(rows):
        bb = td3mf.BoxBuilder()
        y0 = r * (step_d + row_gap)
        for i in range(steps):
            x0 = i * (step_w + gap)
            bb.add(x0, y0, base_h, x0 + step_w, y0 + step_d,
                   base_h + (i + 1) * layer_h)
        m = bb.mesh()
        decals.append((r + 2, m[0], m[1]))
    return plate, decals, total_w, base_h


def build_chips(steps, layer_h, step_w, step_d, gap):
    """Standalone chips of 1..N layers, no base: light has to get through them.

    The reflectance wedge sits on an opaque base, which blocks the backlight, so
    transmission measurements (munki.py transmission) need the filament alone.
    """
    return [
        td3mf.Item(f"chip_{i + 1}",
                   td3mf.box_verts(i * (step_w + gap), 0, 0,
                                   i * (step_w + gap) + step_w, step_d, (i + 1) * layer_h),
                   td3mf.BOX_TRIS.copy())
        for i in range(steps)
    ]


def cmd_chips(args):
    db = DB(args.db)
    fil = db.resolve(args.filament)
    if len(fil) != 1:
        raise SystemExit("chips: give exactly one --filament")
    fil = fil[0]
    items = build_chips(args.steps, args.layer_height, args.step_width,
                        args.step_depth, args.gap)
    td3mf.get_writer(args.flavor)(args.output, items, {}, 1, "part", colors=[fil.color])
    print(f"chips: {args.steps} standalone steps, 1..{args.steps} layers of {fil.label()}, "
          f"{args.step_width:.0f} x {args.step_depth:.0f} mm each")
    print(f"wrote {args.output}")
    print(f"Slice at layer height EXACTLY {args.layer_height} mm, and remember the first "
          f"layer may be thicker: the thickness to give munki.py is what you measure "
          f"with calipers, not steps x layer height.")


def cmd_wedge(args):
    db = DB(args.db)
    fils = db.resolve(args.filament)
    base = db.get(args.base)
    plate, decals, w, base_h = build_wedge(
        args.steps, args.layer_height, args.base_layers,
        args.step_width, args.step_depth, args.gap, rows=len(fils),
    )
    td3mf.get_writer(args.flavor)(args.output, [plate], {0: decals}, 1, "part",
                                  colors=[base.color] + [f.color for f in fils])
    depth = len(fils) * args.step_depth + (len(fils) - 1) * 4.0
    print(f"wedge: {args.steps} steps, 1..{args.steps} layers, "
          f"{len(fils)} filament{'s' if len(fils) > 1 else ''}")
    print(f"  over {args.base_layers} base layers of {base.label()}")
    print(f"  {w:.1f} x {depth:.1f} mm, "
          f"{base_h:.2f}..{base_h + args.steps*args.layer_height:.2f} mm tall")
    print(f"wrote {args.output}")
    print(f"\nAssign extruder 1 = {base.label()}")
    for i, f in enumerate(fils, 2):
        print(f"         extruder {i} = {f.label()}   (row {i-1}, front to back)")
    print(f"Slice at layer height EXACTLY {args.layer_height} mm.")

    # A base that is not opaque makes every patch a measurement of the build
    # plate as much as of the filament.
    t = float(base.transmittance(base_h).max())
    if t > 0.01:
        need = int(np.ceil(base.td_vec().max() * 4.6 / args.layer_height))
        print(f"\n  ! {args.base_layers} layers of {base.name} pass {100*t:.0f}% of "
              f"the light reaching them, so these patches would be sitting on the "
              f"build plate as much as on {base.name}.")
        print(f"    Use --base-layers {need} ({need*args.layer_height:.2f} mm).")
    print("\nPrint one over white and one over black if you can -- two backgrounds")
    print("separate the filament's colour from its opacity.")


# --------------------------------------------------------------------------
# fitting
# --------------------------------------------------------------------------


def sample_image(path, n, axis="x"):
    """Average n evenly spaced patches along the wedge from a photo."""
    im = np.asarray(tdcolor.open_image(path), dtype=np.float64)
    h, w = im.shape[:2]
    out = []
    for i in range(n):
        if axis == "x":
            x0, x1 = int(w * (i + 0.30) / n), int(w * (i + 0.70) / n)
            patch = im[int(h * 0.35): int(h * 0.65), x0:x1]
        else:
            y0, y1 = int(h * (i + 0.30) / n), int(h * (i + 0.70) / n)
            patch = im[y0:y1, int(w * 0.35): int(w * 0.65)]
        out.append(patch.reshape(-1, 3).mean(0))
    return np.array(out)


def _predict(base_lin, col, td, depth):
    T = np.exp(-depth[:, None] / td[None, :])
    return base_lin[None, :] * T + col[None, :] * (1 - T)


def fit_td(datasets, layer_h, per_channel=False, fil_color=None):
    """Solve for (td, filament colour) from one or more step wedges.

    `datasets` is a list of (measured_srgb (n,3), base_srgb (3,)) -- one entry
    per background the wedge was printed over.

    Model, per step i carrying (i+1) layers:
        T_i = exp(-(i+1) * layer_h / td)
        C_i = base * T_i + colour * (1 - T_i)
    all in linear light.

    One background is often not enough. If the filament's own colour is close
    to the background, every step looks nearly the same and td trades off
    against colour -- many pairs fit the data equally well, so the solver
    returns a confident-looking number that is simply wrong. A second wedge
    over a contrasting background breaks that degeneracy, because the two
    backgrounds must be explained by ONE colour and ONE td.
    """
    prepped = []
    for meas_srgb, base_srgb in datasets:
        meas = tdcolor.srgb_to_linear(meas_srgb)
        base = tdcolor.srgb_to_linear(base_srgb)
        depth = (np.arange(len(meas)) + 1) * layer_h
        prepped.append((meas, base, depth))

    def unpack(p):
        if per_channel:
            return np.abs(p[:3]) + 1e-4, np.clip(p[3:6], 0, 1)
        return np.full(3, abs(p[0]) + 1e-4), np.clip(p[1:4], 0, 1)

    def residual(p):
        td, col = unpack(p)
        return np.concatenate(
            [(_predict(b, col, td, d) - m).ravel() for m, b, d in prepped]
        )

    guess_col = (
        tdcolor.srgb_to_linear(fil_color) if fil_color is not None else prepped[0][0][-1]
    )
    p0 = (
        np.concatenate([[0.15, 0.15, 0.15], guess_col])
        if per_channel
        else np.concatenate([[0.15], guess_col])
    )
    sol = least_squares(residual, p0, method="lm", max_nfev=20000)
    td, col = unpack(sol.x)

    preds, des = [], []
    for meas, base, depth in prepped:
        pred = _predict(base, col, td, depth)
        preds.append(pred)
        des.append(
            np.linalg.norm(
                tdcolor.linear_to_lab(pred) - tdcolor.linear_to_lab(meas), axis=-1
            )
        )
    return td, tdcolor.linear_to_srgb(col), des, preds, _conditioning(prepped)


def _conditioning(prepped):
    """How much the wedge actually varies. Near-flat wedges cannot pin td."""
    spans = []
    for meas, base, _ in prepped:
        lab = tdcolor.linear_to_lab(meas)
        spans.append(float(np.linalg.norm(lab[-1] - lab[0])))
    return max(spans), len(prepped)


def _resolve_base(db, spec):
    try:
        return np.array(tdcolor.parse_hex(spec), float)
    except ValueError:
        return db.get(spec).rgb()


def cmd_fit(args):
    db = DB(args.db)
    fil = db.get(args.filament)

    def load(measured, from_image, base):
        if from_image:
            m = sample_image(from_image, args.steps, args.axis)
            print(f"sampled {args.steps} patches from {from_image}")
        elif measured:
            m = np.array([tdcolor.parse_hex(t) for t in measured.split(",")], float)
        else:
            return None
        return m, _resolve_base(db, base)

    datasets = [load(args.measured, args.from_image, args.base)]
    if args.measured2 or args.from_image2:
        if not args.base2:
            raise SystemExit("--base2 is required alongside a second wedge")
        datasets.append(load(args.measured2, args.from_image2, args.base2))
    datasets = [d for d in datasets if d is not None]
    if not datasets:
        raise SystemExit("give either --measured or --from-image")

    for m, _ in datasets:
        if len(m) < 3:
            raise SystemExit("need at least 3 steps to fit anything meaningful")
    if args.per_channel and min(len(m) for m, _ in datasets) < 6:
        print("  ! fewer than 6 steps for a 6-parameter per-channel fit; "
              "treat the result with suspicion", file=sys.stderr)

    td, col, des, preds, (span, nsets) = fit_td(
        datasets, args.layer_height, args.per_channel, fil.rgb()
    )

    for (meas, base_rgb), pred, de in zip(datasets, preds, des):
        print(f"\n{fil.label()}  over base {tdcolor.to_hex(base_rgb)} "
              f"at {args.layer_height} mm layers")
        print(f"{'step':>4} {'layers':>6} {'measured':>9} {'model':>9} {'dE':>5}")
        for i, (m, p, d) in enumerate(zip(meas, pred, de)):
            print(f"{i+1:4d} {i+1:6d} {tdcolor.to_hex(m):>9} "
                  f"{tdcolor.to_hex(tdcolor.linear_to_srgb(p)):>9} {d:5.1f}")

    all_de = np.concatenate(des)
    print(f"\n  td      = {np.round(td, 4).tolist()}")
    print(f"  colour  = {tdcolor.to_hex(col)}  (was {fil.color})")
    print(f"  fit dE  : mean {all_de.mean():.1f}, max {all_de.max():.1f}")
    if all_de.mean() > 5:
        print("  ! poor fit. Check your lighting/white balance, confirm the wedge")
        print("    sliced at the stated layer height, and that --base is right.")

    # A good fit does not imply a trustworthy td. If the wedge barely changes
    # from thinnest to thickest step, td and colour are trading off and the
    # solver will still report a tight residual on a wrong answer.
    if nsets < 2 and span < 25:
        print(f"\n  !! WARNING: this wedge only spans dE {span:.0f} end to end.")
        print("     td and colour are under-determined at that contrast -- the fit")
        print("     above may look tight and still be badly wrong. Print a second")
        print("     wedge over a CONTRASTING base and pass --measured2/--base2.")
        if args.write:
            print("     Refusing --write on a single low-contrast wedge.")
            return

    if not args.write:
        print("\n(dry run -- pass --write to save this into the database)")
        return

    fil.color = tdcolor.to_hex(col)
    if args.per_channel:
        fil.td_rgb = [round(float(v), 4) for v in td]
        fil.td = round(float(td.mean()), 4)
    else:
        fil.td = round(float(td[0]), 4)
        fil.td_rgb = None
    fil.provenance = "measured"
    fil.layer_height_ref = args.layer_height
    from datetime import date

    fil.measured_at = date.today().isoformat()
    fil.notes = (fil.notes + " | ").lstrip(" |") + f"fit from {len(meas)}-step wedge"
    db.save()
    print(f"\nwrote {fil.id} to {db.path}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", default="filaments.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("wedge", help="generate a step-wedge 3MF to print")
    p.add_argument("--filament", required=True,
                   help="comma-separated; one staircase row per filament, each "
                        "on its own extruder")
    p.add_argument("--base", required=True, help="opaque backing filament id")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--steps", type=int, default=12)
    p.add_argument("--layer-height", type=float, default=0.08)
    p.add_argument("--base-layers", type=int, default=8)
    p.add_argument("--step-width", type=float, default=14.0,
                   help="mm; a ColorMunki samples an ~8 mm circle, so leave >= 3 mm each side")
    p.add_argument("--step-depth", type=float, default=14.0)
    p.add_argument("--gap", type=float, default=0.0)
    p.add_argument("--flavor", choices=["orca", "prusa"], default="orca")
    p.set_defaults(fn=cmd_wedge)

    p = sub.add_parser("chips", help="standalone filament-only chips, for transmission")
    p.add_argument("--filament", required=True)
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--steps", type=int, default=8)
    p.add_argument("--layer-height", type=float, default=0.08)
    p.add_argument("--step-width", type=float, default=20.0)
    p.add_argument("--step-depth", type=float, default=20.0)
    p.add_argument("--gap", type=float, default=2.0)
    p.add_argument("--flavor", choices=["orca", "prusa"], default="orca")
    p.set_defaults(fn=cmd_chips)

    p = sub.add_parser("fit", help="fit td + colour from measured patches")
    p.add_argument("--filament", required=True)
    p.add_argument("--base", required=True, help="base colour as hex, or a filament id")
    p.add_argument("--measured", help="comma-separated hex, one per step, thinnest first")
    p.add_argument("--from-image", help="photo of the wedge to sample instead")
    p.add_argument("--base2", help="second wedge's base colour (hex or filament id)")
    p.add_argument("--measured2", help="second wedge's patches, over a contrasting base")
    p.add_argument("--from-image2", help="photo of the second wedge")
    p.add_argument("--steps", type=int, default=12, help="patch count when using --from-image")
    p.add_argument("--axis", choices=["x", "y"], default="x")
    p.add_argument("--layer-height", type=float, default=0.08)
    p.add_argument("--per-channel", action="store_true",
                   help="fit td separately per RGB channel (needs >=6 steps)")
    p.add_argument("--write", action="store_true", help="save into the database")
    p.set_defaults(fn=cmd_fit)

    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
