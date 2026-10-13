#!/usr/bin/env python3
"""calibrate -- print the step wedges a filament is calibrated from.

  `wedge` builds a step-wedge 3MF: a staircase of 1 to N layers of the test
  filament, laid over an opaque base. Give it a white AND a black base and
  the one file holds a wedge on each. Watching layers hide a background unlike
  them separates what the filament absorbs from what it scatters: a coloured
  filament needs both (each matches it somewhere), white or black needs only
  the other.

  `chips` builds standalone filament-only chips, for transmission readings.

The fit is spectral and lives elsewhere: read the printed wedge with
`stackforge-measure measure-wedge --sheet`, then `stackforge-spectral fit`.
The RGB td fit that used to be `stackforge-calibrate fit` is not on this
branch (`fit_td` stays as a library function only).

    stackforge-calibrate wedge --filament teal --base white,black --template p.3mf -o wedge_teal.3mf
    stackforge-calibrate chips --filament teal --template p.3mf -o chips_teal.3mf  # transmission
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np

from stackforge.core import threemf
from stackforge.core import colormath
from stackforge.core import optics
from stackforge.core import wedgesheet
from stackforge.core.filamentdb import DEFAULT_DB, DB


# --------------------------------------------------------------------------
# step wedge geometry
# --------------------------------------------------------------------------


def build_wedge(steps, layer_h, base_layers, step_w, step_d, gap, rows=1, row_gap=4.0,
                hinge_layers=None, first_layer_h=None, base_patch=False):
    """A staircase: step i carries i+1 layers of the test filament.

    `base_patch` leaves the first slot bare: a patch of base with no test filament,
    measured as step 0 so the fit uses the base as printed, not as the library guesses it.

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
            raise ValueError(f"hinge layers must be 1 to {base_layers - 1} "
                             f"(thinner than the {base_layers} base layers)")
    base_h = first_layer_h + (base_layers - 1) * layer_h
    first = 1 if base_patch else 0          # slot of step 1
    slots = steps + first
    total_w = slots * step_w + (slots - 1) * gap
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
            for i in range(slots):
                x0 = i * (step_w + gap)
                pb.add(x0, y0, hinge_h, x0 + step_w, y0 + step_d, base_h)
        verts, tris = pb.mesh()
        plate = threemf.Item("wedge_base", verts, tris)
    decals = []
    for r in range(rows):
        bb = threemf.BoxBuilder()
        y0 = r * (step_d + row_gap)
        for i in range(steps):
            x0 = (i + first) * (step_w + gap)
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
MIN_BASE_MM = 1.0       # thinnest automatic base, opaque or not
MIN_CHANNEL_CONTRAST = 0.25  # linear RGB, coarser than spectra so stricter; see WedgeSet.contrast_notes


def concentric_settings(flavor) -> dict:
    """Per-object overrides that lay every solid layer of a wedge concentrically.

    Line fills turn 90 degrees each layer, and the meter sees the direction: the 2026-10-12
    orange wedges zigzag ~7 dE between odd and even steps on both bases. The layers under the
    top one show through a translucent filament, so they are concentric too (Orca only: Prusa
    has no setting for them).
    """
    if flavor == "orca":
        return {"top_surface_pattern": "concentric", "internal_solid_infill_pattern": "concentric"}
    return {"top_fill_pattern": "concentric"}


class WedgeSet:
    """Every wedge a calibration needs, in one file: one wedge per base, each carrying a row
    of steps per test filament.

    Extruders: the bases that carry a wedge first, in order, then the test filaments that are
    not also one of those bases.
    A row whose filament IS its wedge's base is left out (it would be a solid block), and a
    wedge left with no rows is dropped, so calibrating white over "white,black" gives one
    wedge over black. Every wedge gets the same base height, opaque for the most
    see-through base and at least MIN_BASE_MM, unless `base_layers` is given.
    """

    def __init__(self, fils, bases, steps, layer_h, first_layer_h, step_w, step_d, gap=0.0,
                 base_layers=None, hinge_layers=None, base_patch=True):
        self.steps, self.layer_h, self.first_layer_h = steps, layer_h, first_layer_h
        self.base_patch = base_patch
        self.wedges = [(b, [f for f in fils if f.id != b.id]) for b in bases]
        self.wedges = [(b, rows) for b, rows in self.wedges if rows]
        if not self.wedges:
            raise ValueError("every test filament is its own base: nothing to measure")
        # Only bases that carry a wedge take an extruder: white over "white,black" is black
        # on 1 and white on 2, as a single-base wedge always was, not white on 1 because it
        # was offered as a base.
        used = [b for b, _ in self.wedges]
        self.slots = used + [f for f in fils if f.id not in {b.id for b in used}]
        ext = {f.id: i for i, f in enumerate(self.slots, 1)}
        if base_layers is None:
            # Black is opaque in one layer, but a 0.25 mm base is too flimsy to handle
            # and leaves no room for a hinge thinner than it.
            sturdy = 1 + int(np.ceil(max(0.0, MIN_BASE_MM - first_layer_h) / layer_h - 1e-9))
            base_layers = max([sturdy] + [optics.opaque_layers(b, first_layer_h, layer_h)[0]
                                          for b, _ in self.wedges])
        self.base_layers = base_layers
        self.hinge = None
        if gap > 0:
            if base_layers < 2:
                raise ValueError("a gap needs at least 2 base layers: the hinge between the "
                                 "steps has to be thinner than the base")
            self.hinge = min(hinge_layers or 4, base_layers - 1)
        self.items, self.decals, self.base_ext = [], {}, []
        y = 0.0
        for i, (b, rows) in enumerate(self.wedges):
            plate, decals, self.width, self.base_h = build_wedge(
                steps, layer_h, base_layers, step_w, step_d, gap, rows=len(rows),
                hinge_layers=self.hinge, first_layer_h=first_layer_h, base_patch=base_patch)
            shift = np.array([0.0, y, 0.0])
            self.items.append(threemf.Item(f"wedge_over_{b.id}", plate.verts + shift, plate.tris))
            self.decals[i] = [(ext[rows[r - 2].id], v + shift, t) for r, v, t in decals]
            self.base_ext.append(ext[b.id])
            y += len(rows) * step_d + (len(rows) - 1) * 4.0 + WEDGE_SPACING
        self.depth = y - WEDGE_SPACING
        self.colors = [f.color for f in self.slots]
        self.top = self.base_h + steps * layer_h

    @staticmethod
    def letter(n):
        return chr(ord("A") + n) if n < 26 else str(n + 1)

    def where(self, n):
        if len(self.wedges) == 1:
            return "the only wedge"
        return ("front wedge" if n == 0 else "back wedge" if n == len(self.wedges) - 1 else
                f"wedge {n + 1} from the front")

    def write(self, path, flavor, template):
        if template:
            threemf.check_template_slots(template, len(self.slots))
            bed = threemf.template_bed_size(template)
            if bed and (self.width > bed[0] or self.depth > bed[1]):
                raise SystemExit(
                    f"The wedges come to {self.width:.0f} x {self.depth:.0f} mm but the bed is "
                    f"{bed[0]:.0f} x {bed[1]:.0f} mm: use a smaller gap, narrower steps or "
                    f"fewer steps.")
        threemf.get_writer(flavor)(
            path, self.items, self.decals, self.base_ext, "part", template=template,
            colors=self.colors, layer_height=self.layer_h, first_layer_height=self.first_layer_h,
            solid=True, object_settings={**threemf.solid_object_settings(flavor, self.layer_h),
                                         **concentric_settings(flavor)})

    def sheet(self, model_path) -> dict:
        """What measure needs to walk these wedges, and calibrate to fit them afterwards."""
        strips = []
        for n, (b, rows) in enumerate(self.wedges):
            for r, f in enumerate(rows):
                where = self.where(n) + (f", row {r + 1} from the front" if len(rows) > 1 else "")
                strips.append({"wedge": self.letter(n), "where": where,
                               "base": wedgesheet.filament_ref(b),
                               "filament": wedgesheet.filament_ref(f)})
        return {"kind": wedgesheet.SHEET_KIND, "version": wedgesheet.VERSION,
                "model": os.path.basename(model_path), "layer_height": self.layer_h,
                "first_layer_height": self.first_layer_h, "steps": self.steps,
                "base_patch": self.base_patch, "strips": strips}

    def write_sheet(self, model_path) -> str:
        path = wedgesheet.sheet_path_for(model_path)
        wedgesheet.write(path, self.sheet(model_path))
        return path

    def describe(self) -> list[str]:
        """Extruder assignment and wedge order, for the user to set up the printer by."""
        out = [f"Extruder {i} = {f.label()}" for i, f in enumerate(self.slots, 1)]
        out.append("")
        for n, (b, rows) in enumerate(self.wedges):
            out.append(f"Wedge {self.letter(n)} ({self.where(n)}): over {b.label()}, "
                       + ", ".join(f.label() for f in rows))
        if self.base_patch:
            out.append("The bare patch at the left end of each wedge is the base alone: "
                       "it is measured first.")
        return out

    def contrast_notes(self) -> list[str]:
        """Test filaments whose bases look like them somewhere, judged from library colours.

        The spectral fit refuses a band where no wedge changes between its bare base and its
        thickest step (`tools/spectral.signal`). Before printing only the library colours are
        known, so this is the same test per linear RGB channel: white over black alone passes,
        orange over white alone does not (white looks like orange in red)."""
        out = []
        tested = {f.id: f for _, rows in self.wedges for f in rows}
        for f in tested.values():
            bases = [b for b, rows in self.wedges if f.id in {r.id for r in rows}]
            lin = colormath.srgb_to_linear(f.rgb())
            gap = np.max([np.abs(lin - colormath.srgb_to_linear(b.rgb())) for b in bases], axis=0)
            weak = [i for i in range(3) if gap[i] < MIN_CHANNEL_CONTRAST]
            if weak:
                want = "black" if lin[weak].mean() > 0.3 else "white"
                out.append(f"{f.label()} over {' and '.join(b.label() for b in bases)}: no base "
                           f"differs from it in {'/'.join('RGB'[i] for i in weak)}, so the spectral "
                           f"fit will likely refuse to save. Add a {want} base.")
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
    print(f"chips: {args.steps} standalone steps of 1 to {args.steps} layers of {fil.label()}, "
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
                      args.hinge_layers, base_patch=not args.no_base_patch)
    except ValueError as exc:
        raise SystemExit(f"wedge: {exc}")
    ws.write(args.output, args.flavor, args.template)
    sheet = ws.write_sheet(args.output)
    print(f"wedge: {len(ws.wedges)} wedge{'s' if len(ws.wedges) > 1 else ''} of {args.steps} "
          f"steps of 1 to {args.steps} layers")
    # Opaque, or every patch measures the build plate as much as the filament.
    print(f"  over {ws.base_layers} base layers" + (" (auto: opaque)" if auto else ""))
    if ws.hinge is not None:
        print(f"  {args.gap:g} mm gaps joined by a {ws.hinge}-layer hinge "
              f"({ws.hinge * args.layer_height:.2f} mm): flex a step flat onto the aperture")
    print(f"  {ws.width:.1f} x {ws.depth:.1f} mm; base {ws.base_h:.2f} mm thick, tallest step {ws.top:.2f} mm")
    print(f"wrote {args.output}")
    print(f"wrote {sheet}  (the wedge sheet: give it to stackforge-measure measure-wedge --sheet)\n")
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
    for note in ws.contrast_notes():
        print(f"\n  ! {note}")
    print(f"\nThen: stackforge-measure measure-wedge --sheet {sheet}")
    print(f"      stackforge-spectral fit --filament <id> "
          f"--readings {wedgesheet.readings_path_for(sheet)} --write")


# --------------------------------------------------------------------------
# fitting
# --------------------------------------------------------------------------


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
    p.add_argument("--no-base-patch", action="store_true",
                   help="leave out the bare patch of base at the start of each wedge (it is "
                        "measured so the fit uses the base as printed)")
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

    return ap


FIT_GONE = """stackforge-calibrate fit (the RGB td fit) is not on this branch: calibration is spectral only.
  stackforge-measure measure-wedge --sheet <wedge>.sheet.json
  stackforge-spectral fit --readings <wedge>.readings.json --filament <id> [--write]"""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    sub = argv[2:] if argv[:1] == ["--db"] else argv[1:] if argv[:1] and argv[0].startswith("--db=") else argv
    if sub[:1] == ["fit"]:
        raise SystemExit(FIT_GONE)
    ap = build_parser()
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
