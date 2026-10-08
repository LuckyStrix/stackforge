#!/usr/bin/env python3
"""calibrate -- measure a filament's real color and transmission distance.

Two steps, with a print in between.

  1. `wedge` builds a step-wedge 3MF: a staircase of 1..N layers of the test
     filament, laid over an opaque base. Give it a white AND a black base and
     the one file holds a wedge on each. Two backgrounds pin down the
     filament's own color and its opacity independently, which a single
     background cannot do.

  2. `fit` takes the colors you read off the printed wedge and solves for the
     td (and, if you give it enough steps, the per-channel td_rgb) that best
     explains them, then writes the result into the filament database.

Reading the colors: a spectrophotometer is ideal, but a phone photo under flat
indirect daylight with a white card in frame works well enough -- sample the
middle of each step, average a patch, and white-balance against the card.

    stackforge-calibrate wedge --filament teal --base white,black --template p.3mf -o wedge_teal.3mf
    stackforge-calibrate chips --filament teal --template p.3mf -o chips_teal.3mf  # transmission
    stackforge-calibrate fit --filament teal --base "#F4F5F0" --template p.3mf \
        --measured "#D8E6E4,#B4D2D0,#8FBEBC,#6FADAB,#54A09E,#3E9694,#2C8E8C,#1F8886"
    stackforge-calibrate fit --filament teal --base "#F4F5F0" --template p.3mf --from-image shot.png
"""

from __future__ import annotations

import argparse
import sys

import numpy as np

from stackforge.core import threemf
from stackforge.core import colormath
from stackforge.core import optics
from stackforge.core.filamentdb import DEFAULT_DB, DB


# --------------------------------------------------------------------------
# step wedge geometry
# --------------------------------------------------------------------------


def build_wedge(steps, layer_h, base_layers, step_w, step_d, gap, rows=1, row_gap=4.0,
                hinge_layers=None, first_layer_h=None):
    """A staircase: step i carries i+1 layers of the test filament.

    `rows` puts several test filaments on one plate, one staircase each, over a
    shared base. On an independent-toolhead machine that is free -- the base
    takes one head and each test filament takes another, so a four-head printer
    calibrates three filaments per print instead of one.

    `hinge_layers` (needs `gap` > 0) thins the base between the steps to that many
    layers, so each step can be flexed flat against an instrument aperture while the
    wedge stays one piece. The pad under each step keeps the full `base_layers`,
    which is all the measurement sees, so opacity is unaffected.

    `first_layer_h` (default `layer_h`) is the slicer's first layer. Layer tops sit at
    `first + k*layer`, so the base is `first + (base_layers-1)*layer` tall and each
    step adds whole layers on top; a base that ignored a thicker first layer would
    put every step edge on a layer mid-plane, where the slicer rounds it at random.
    """
    first_layer_h = layer_h if first_layer_h is None else first_layer_h
    if hinge_layers is not None:
        if gap <= 0:
            raise ValueError("a hinge needs a gap between the steps (--gap > 0)")
        if not 1 <= hinge_layers < base_layers:
            raise ValueError(f"hinge layers must be 1..{base_layers - 1} "
                             f"(thinner than the {base_layers} base layers)")
    base_h = first_layer_h + (base_layers - 1) * layer_h
    total_w = steps * step_w + (steps - 1) * gap
    total_d = rows * step_d + (rows - 1) * row_gap
    if hinge_layers is None:
        plate = threemf.Item(
            "wedge_base",
            threemf.box_verts(0, 0, 0, total_w, total_d, base_h),
            threemf.BOX_TRIS.copy(),
        )
    else:
        hinge_h = first_layer_h + (hinge_layers - 1) * layer_h
        pb = threemf.BoxBuilder()
        pb.add(0, 0, 0, total_w, total_d, hinge_h)
        for r in range(rows):
            y0 = r * (step_d + row_gap)
            for i in range(steps):
                x0 = i * (step_w + gap)
                pb.add(x0, y0, hinge_h, x0 + step_w, y0 + step_d, base_h)
        verts, tris = pb.mesh()
        plate = threemf.Item("wedge_base", verts, tris)
    decals = []
    for r in range(rows):
        bb = threemf.BoxBuilder()
        y0 = r * (step_d + row_gap)
        for i in range(steps):
            x0 = i * (step_w + gap)
            bb.add(x0, y0, base_h, x0 + step_w, y0 + step_d,
                   base_h + (i + 1) * layer_h)
        m = bb.mesh()
        decals.append((r + 2, m[0], m[1]))
    return plate, decals, total_w, base_h


def build_chips(steps, layer_h, step_w, step_d, gap, first_layer_h=None):
    """Standalone chips of 1..N layers, no base: light has to get through them.
    Chip n is `first + (n-1)*layer` tall (the slicer's grid), not `n*layer`.

    The reflectance wedge sits on an opaque base, which blocks the backlight, so
    transmission measurements (stackforge-measure transmission) need the filament alone.
    """
    first_layer_h = layer_h if first_layer_h is None else first_layer_h
    return [
        threemf.Item(f"chip_{i + 1}",
                   threemf.box_verts(i * (step_w + gap), 0, 0,
                                   i * (step_w + gap) + step_w, step_d,
                                   first_layer_h + i * layer_h),
                   threemf.BOX_TRIS.copy())
        for i in range(steps)
    ]


WEDGE_SPACING = 8.0     # mm between the wedges of a set, front to back


class WedgeSet:
    """Every wedge a calibration needs, in one file: one wedge per base, each carrying a row
    of steps per test filament.

    Extruders: the bases first, in order, then the test filaments that are not also a base.
    A row whose filament IS its wedge's base is left out (it would be a solid block), and a
    wedge left with no rows is dropped, so calibrating white over "white,black" gives one
    wedge over black. Every wedge gets the same base height, opaque for the most
    see-through base unless `base_layers` is given.
    """

    def __init__(self, fils, bases, steps, layer_h, first_layer_h, step_w, step_d, gap=0.0,
                 base_layers=None, hinge_layers=None):
        self.slots = list(bases) + [f for f in fils if f.id not in {b.id for b in bases}]
        ext = {f.id: i for i, f in enumerate(self.slots, 1)}
        self.wedges = [(b, [f for f in fils if f.id != b.id]) for b in bases]
        self.wedges = [(b, rows) for b, rows in self.wedges if rows]
        if not self.wedges:
            raise ValueError("every test filament is its own base: nothing to measure")
        if base_layers is None:
            base_layers = max(optics.opaque_layers(b, first_layer_h, layer_h)[0]
                              for b, _ in self.wedges)
        self.base_layers = base_layers
        self.hinge = None
        if gap > 0:
            self.hinge = min(hinge_layers or 4, base_layers - 1)
        self.items, self.decals, self.base_ext = [], {}, []
        y = 0.0
        for i, (b, rows) in enumerate(self.wedges):
            plate, decals, self.width, self.base_h = build_wedge(
                steps, layer_h, base_layers, step_w, step_d, gap, rows=len(rows),
                hinge_layers=self.hinge, first_layer_h=first_layer_h)
            shift = np.array([0.0, y, 0.0])
            self.items.append(threemf.Item(f"wedge_over_{b.id}", plate.verts + shift, plate.tris))
            self.decals[i] = [(ext[rows[r - 2].id], v + shift, t) for r, v, t in decals]
            self.base_ext.append(ext[b.id])
            y += len(rows) * step_d + (len(rows) - 1) * 4.0 + WEDGE_SPACING
        self.depth = y - WEDGE_SPACING
        self.colors = [f.color for f in self.slots]
        self.top = self.base_h + steps * layer_h

    def write(self, path, flavor, template, layer_h, first_layer_h):
        if template:
            threemf.check_template_slots(template, len(self.slots))
        threemf.get_writer(flavor)(
            path, self.items, self.decals, self.base_ext, "part", template=template,
            colors=self.colors, layer_height=layer_h, first_layer_height=first_layer_h,
            solid=True, object_settings=threemf.solid_object_settings(flavor, layer_h))

    def describe(self) -> list[str]:
        """Extruder assignment and wedge order, for the user to set up the printer by."""
        out = [f"Extruder {i} = {f.label()}" for i, f in enumerate(self.slots, 1)]
        out.append("")
        for n, (b, rows) in enumerate(self.wedges):
            where = ("front" if n == 0 else "back" if n == len(self.wedges) - 1 else "middle")
            out.append(f"Wedge {'AB'[n] if n < 2 else n + 1} ({where}): over {b.label()}, "
                       + ", ".join(f.label() for f in rows))
        return out


def resolve_grid(args):
    """Fill args.layer_height / first_layer_height from --template (the profile that
    will slice the file), else the explicit flags; refuse to guess for orca."""
    threemf.require_layer_grid(args.flavor, args.template, args.layer_height)
    if args.template:
        threemf.check_template(args.template)
    t_lh, t_flh = threemf.template_layer_settings(args.template) if args.template else (None, None)
    if args.layer_height is None:
        args.layer_height = t_lh or 0.08
        if t_lh:
            print(f"layer height {t_lh} mm (from template)")
    elif t_lh and abs(t_lh - args.layer_height) > 1e-9:
        print(f"  - overriding the template's {t_lh} mm layer height with {args.layer_height} mm")
    if args.first_layer_height is None:
        args.first_layer_height = t_flh or args.layer_height
        if t_flh:
            print(f"first layer {t_flh} mm (from template)")


def _grid_note(args):
    return (f"Slice at layer height EXACTLY {args.layer_height} mm with first layer "
            f"{args.first_layer_height} mm, or the step edges fall between layers.")


def cmd_chips(args):
    resolve_grid(args)
    db = DB(args.db)
    fil = db.resolve(args.filament)
    if len(fil) != 1:
        raise SystemExit("chips: give exactly one --filament")
    fil = fil[0]
    items = build_chips(args.steps, args.layer_height, args.step_width,
                        args.step_depth, args.gap, args.first_layer_height)
    # Solid, like the plaque: the td model assumes a continuous film, and a
    # stock profile leaves everything under the top shell as sparse infill.
    threemf.get_writer(args.flavor)(args.output, items, {}, 1, "part", template=args.template,
                                    colors=[fil.color], layer_height=args.layer_height,
                                    first_layer_height=args.first_layer_height, solid=True,
                                    object_settings=threemf.solid_object_settings(
                                        args.flavor, args.layer_height))
    print(f"chips: {args.steps} standalone steps, 1..{args.steps} layers of {fil.label()}, "
          f"{args.step_width:.0f} x {args.step_depth:.0f} mm each")
    print(f"wrote {args.output}")
    heights = [args.first_layer_height + i * args.layer_height for i in range(args.steps)]
    print(_grid_note(args))
    print("Chip thicknesses (mm) for stackforge-measure, check with calipers: "
          + ",".join(f"{h:.3f}" for h in heights))


def cmd_wedge(args):
    resolve_grid(args)
    db = DB(args.db)
    fils = db.resolve(args.filament)
    bases = [db.get(b.strip()) for b in args.base.split(",") if b.strip()]
    auto = args.base_layers is None
    try:
        ws = WedgeSet(fils, bases, args.steps, args.layer_height, args.first_layer_height,
                      args.step_width, args.step_depth, args.gap, args.base_layers,
                      args.hinge_layers)
    except ValueError as exc:
        raise SystemExit(f"wedge: {exc}")
    ws.write(args.output, args.flavor, args.template, args.layer_height, args.first_layer_height)
    print(f"wedge: {len(ws.wedges)} wedge{'s' if len(ws.wedges) > 1 else ''} of {args.steps} "
          f"steps, 1..{args.steps} layers")
    # Opaque, or every patch measures the build plate as much as the filament.
    print(f"  over {ws.base_layers} base layers" + (" (auto: opaque)" if auto else ""))
    if ws.hinge is not None:
        print(f"  {args.gap:g} mm gaps joined by a {ws.hinge}-layer hinge "
              f"({ws.hinge * args.layer_height:.2f} mm): flex a step flat onto the aperture")
    print(f"  {ws.width:.1f} x {ws.depth:.1f} mm, {ws.base_h:.2f}..{ws.top:.2f} mm tall")
    print(f"wrote {args.output}\n")
    print("\n".join(ws.describe()))
    print(_grid_note(args))

    for b, _ in ws.wedges:
        t = float(b.transmittance(ws.base_h).max())
        if t > 0.01:
            need = optics.opaque_layers(b, args.first_layer_height, args.layer_height)[0]
            print(f"\n  ! {ws.base_layers} layers of {b.name} pass {100*t:.0f}% of "
                  f"the light reaching them, so these patches would be sitting on the "
                  f"build plate as much as on {b.name}.")
            print(f"    Use --base-layers {need} "
                  f"({args.first_layer_height + (need-1)*args.layer_height:.2f} mm).")
    if len(ws.wedges) < 2:
        print("\nOne base only: give a contrasting second one (--base white,black) -- two "
              "backgrounds separate the filament's colour from its opacity.")
    fit = f"\nThen: stackforge-calibrate fit --filament <id> --base {ws.wedges[0][0].id} "
    fit += "--measured \"<hex per step>\""
    if len(ws.wedges) > 1:
        fit += f" --base2 {ws.wedges[1][0].id} --measured2 \"<hex per step>\""
    print(fit + f" --layer-height {args.layer_height:g} --write")


# --------------------------------------------------------------------------
# fitting
# --------------------------------------------------------------------------


def sample_image(path, n, axis="x"):
    """Average n evenly spaced patches along the wedge from a photo."""
    im = np.asarray(colormath.open_image(path), dtype=np.float64)
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

    Model, per step i carrying (i+1) layers of filament:
        T_i = exp(-(i + 1) * layer_h / td)
        C_i = base * T_i + colour * (1 - T_i)
    all in linear light. The slicer's thicker first layer belongs to the base
    the steps sit on (build_wedge), so every step layer is `layer_h` thick.

    One background is often not enough. If the filament's own colour is close
    to the background, every step looks nearly the same and td trades off
    against colour -- many pairs fit the data equally well, so the solver
    returns a confident-looking number that is simply wrong. A second wedge
    over a contrasting background breaks that degeneracy, because the two
    backgrounds must be explained by ONE colour and ONE td.
    """
    from scipy.optimize import least_squares    # slow import; only the fit needs it
    prepped = []
    for meas_srgb, base_srgb in datasets:
        meas = colormath.srgb_to_linear(meas_srgb)
        base = colormath.srgb_to_linear(base_srgb)
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
        colormath.srgb_to_linear(fil_color) if fil_color is not None else prepped[0][0][-1]
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
                colormath.linear_to_lab(pred) - colormath.linear_to_lab(meas), axis=-1
            )
        )
    return td, colormath.linear_to_srgb(col), des, preds, _conditioning(prepped)


def _conditioning(prepped):
    """How much the wedge actually varies. Near-flat wedges cannot pin td."""
    spans = []
    for meas, base, _ in prepped:
        lab = colormath.linear_to_lab(meas)
        spans.append(float(np.linalg.norm(lab[-1] - lab[0])))
    return max(spans), len(prepped)


def _resolve_base(db, spec):
    try:
        return np.array(colormath.parse_hex(spec), float)
    except ValueError:
        return db.get(spec).rgb()


def fit_layer_height(args):
    """The wedge's layer height: --layer-height, else --template's. Never a guess:
    a wrong one scales every td by the same wrong factor and the fit still looks tight."""
    if args.layer_height is None and args.template:
        threemf.check_template(args.template)
        args.layer_height = threemf.template_layer_settings(args.template)[0]
        if args.layer_height:
            print(f"layer height {args.layer_height:g} mm (from template)")
    if args.layer_height is None:
        raise SystemExit("fit needs the layer height the wedge was printed at: give "
                         "--template (your slicer project) or --layer-height")


def cmd_fit(args):
    fit_layer_height(args)
    db = DB(args.db)
    fil = db.get(args.filament)

    def load(measured, from_image, base):
        if from_image:
            m = sample_image(from_image, args.steps, args.axis)
            print(f"sampled {args.steps} patches from {from_image}")
        elif measured:
            m = np.array([colormath.parse_hex(t) for t in measured.split(",")], float)
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
        print(f"\n{fil.label()}  over base {colormath.to_hex(base_rgb)} "
              f"at {args.layer_height} mm layers")
        print(f"{'step':>4} {'layers':>6} {'measured':>9} {'model':>9} {'dE':>5}")
        for i, (m, p, d) in enumerate(zip(meas, pred, de)):
            print(f"{i+1:4d} {i+1:6d} {colormath.to_hex(m):>9} "
                  f"{colormath.to_hex(colormath.linear_to_srgb(p)):>9} {d:5.1f}")

    all_de = np.concatenate(des)
    print(f"\n  td      = {np.round(td, 4).tolist()}")
    print(f"  colour  = {colormath.to_hex(col)}  (was {fil.color})")
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

    fil.color = colormath.to_hex(col)
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
    ap.add_argument("--db", default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("wedge", help="generate a step-wedge 3MF to print")
    p.add_argument("--filament", required=True,
                   help="comma-separated; one staircase row per filament, each "
                        "on its own extruder")
    p.add_argument("--base", required=True,
                   help="opaque backing filament id(s), comma-separated: one wedge per base, all "
                        "in one file (e.g. white,black; a test filament is skipped over itself)")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--steps", type=int, default=12)
    p.add_argument("--layer-height", type=float, default=None,
                   help="must match the slicer (default: from --template, else 0.08)")
    p.add_argument("--first-layer-height", type=float, default=None,
                   help="the slicer's first layer; step edges sit on first + n*layer "
                        "(default: from --template, else --layer-height)")
    p.add_argument("--template", help="a project .3mf from your slicer: supplies the layer grid "
                   "and is carried over so Flash Studio opens the file as a project")
    p.add_argument("--base-layers", type=int, default=None,
                   help="layers of base under the steps (default: auto, enough to be opaque)")
    p.add_argument("--step-width", type=float, default=14.0,
                   help="mm; a ColorMunki samples an ~8 mm circle, so leave >= 3 mm each side")
    p.add_argument("--step-depth", type=float, default=14.0)
    p.add_argument("--gap", type=float, default=0.0,
                   help="mm between steps; with a gap the base between them becomes a thin "
                        "hinge so one step at a time can be flexed flat onto the instrument "
                        "(try 8)")
    p.add_argument("--hinge-layers", type=int, default=None,
                   help="base layers left in the gap (default 4 when --gap > 0; the pads "
                        "under the steps keep --base-layers)")
    p.add_argument("--flavor", choices=["orca", "prusa"], default="orca")
    p.set_defaults(fn=cmd_wedge)

    p = sub.add_parser("chips", help="standalone filament-only chips, for transmission")
    p.add_argument("--filament", required=True)
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--steps", type=int, default=8)
    p.add_argument("--layer-height", type=float, default=None,
                   help="must match the slicer (default: from --template, else 0.08)")
    p.add_argument("--first-layer-height", type=float, default=None,
                   help="the slicer's first layer; step edges sit on first + n*layer "
                        "(default: from --template, else --layer-height)")
    p.add_argument("--template", help="a project .3mf from your slicer: supplies the layer grid "
                   "and is carried over so Flash Studio opens the file as a project")
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
    p.add_argument("--layer-height", type=float, default=None,
                   help="the layer height the wedge was printed at (default: from --template)")
    p.add_argument("--template", help="the slicer project the wedge was printed with; "
                   "supplies the layer height")
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
