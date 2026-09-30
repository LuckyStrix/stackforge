#!/usr/bin/env python3
"""stackforge -- full-color prints from a handful of filaments, without the
swap-minimization constraint HueForge is built around.

HueForge builds a HEIGHT MAP: one global filament order for the whole print,
and a pixel's color is decided purely by how tall the stack is there. That
architecture exists because on a single-hotend MMU every swap is expensive, so
swaps have to be global events.

With independent toolheads swaps are cheap, so this drops the height map
entirely. Every pixel gets its OWN stack of filaments, chosen independently,
and the plaque comes out FLAT -- constant thickness, color encoded in the
vertical composition at each pixel. Height is no longer carrying the image, so
there is no surface topography to catch raking light.

Two things follow from that:

  * XY dithering becomes available. At a 0.4 mm nozzle the pixels are already
    near-invisible at arm's length, so neighbouring pixels can carry different
    stacks and blend optically. A height-map approach cannot do this.
  * The gamut is the set of colors reachable by ANY stack, not just the ones
    on one monotonic ordering. Vastly larger for the same filament count.

OPTICAL MODEL
    Layers composite bottom-up in linear light. Adding a layer of filament f
    of thickness h over an existing color C:

        T = exp(-h / td_f)              (per channel)
        C' = C * T + color_f * (1 - T)

    which is alpha-over with alpha = 1 - T. This is a single-pass
    approximation -- it ignores the light that scatters back up through the
    stack a second time -- but it is the same approximation HueForge and
    friends use, and it holds up well for pigmented PLA.

USAGE
    stackforge.py photo.jpg -o plaque.3mf \
        --filaments white,black,blue,red --base white \
        --width 150 --layer-height 0.08 --max-layers 14 --dither floyd
"""

from __future__ import annotations

import argparse
import sys
import time

import itertools

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.spatial import cKDTree

import tdcolor
import td3mf
from filamentdb import DB


# --------------------------------------------------------------------------
# gamut search
# --------------------------------------------------------------------------


class Gamut:
    """All colors reachable by stacking <= max_layers of the given filaments.

    Built by breadth-first expansion in linear RGB with dedup on a quantized
    grid. The state is just the composited color -- the stack below it stops
    mattering once it is obscured -- so the search stays 3-dimensional no
    matter how deep it goes.
    """

    def __init__(self, filaments, base, layer_h, max_layers, grid=192, cap=400_000,
                 verbose=True, progress=None):
        self.filaments = filaments
        self.base = base
        self.layer_h = layer_h
        self.max_layers = max_layers
        self.n = len(filaments)

        cols = np.array([f.linear() for f in filaments])            # (n,3)
        trans = np.array([f.transmittance(layer_h) for f in filaments])  # (n,3)
        self.cols, self.trans = cols, trans

        base_lin = base.linear()

        # colors[i], parent[i], fil[i], depth[i] -- parent chain rebuilds stacks
        colors = [base_lin]
        parent = [-1]
        fil = [-1]
        depth = [0]
        seen = {self._key(base_lin, grid)}
        frontier = np.array([0])

        t0 = time.time()
        for d in range(1, max_layers + 1):
            fc = np.array([colors[i] for i in frontier])            # (m,3)
            new_c, new_p, new_f = [], [], []
            for fi in range(self.n):
                cand = fc * trans[fi] + cols[fi] * (1.0 - trans[fi])
                keys = self._keys(cand, grid)
                for j, k in enumerate(keys):
                    if k in seen:
                        continue
                    seen.add(k)
                    new_c.append(cand[j])
                    new_p.append(int(frontier[j]))
                    new_f.append(fi)
            if not new_c:
                if verbose:
                    print(f"  depth {d}: converged, no new colors")
                break
            start = len(colors)
            colors.extend(new_c)
            parent.extend(new_p)
            fil.extend(new_f)
            depth.extend([d] * len(new_c))
            frontier = np.arange(start, len(colors))
            if verbose:
                print(f"  depth {d:2d}: +{len(new_c):7d} states, {len(colors):8d} total")
            if progress:
                progress(d / max_layers, f"gamut depth {d}/{max_layers}: {len(colors)} colors")
            if len(colors) > cap:
                print(f"  ! hit --cap {cap} at depth {d}; stopping expansion early. "
                      f"Lower --grid or --max-layers for a cleaner search.", file=sys.stderr)
                break

        self.colors = np.array(colors)
        self.parent = np.array(parent)
        self.fil = np.array(fil)
        self.depth = np.array(depth)
        self.lab = tdcolor.linear_to_lab(self.colors)
        self.tree = cKDTree(self.lab)
        if verbose:
            print(f"  gamut: {len(self.colors)} colors, built in {time.time()-t0:.1f}s")

    @staticmethod
    def _key(c, grid):
        return tuple((np.clip(c, 0, 1) * grid).astype(np.int32))

    @staticmethod
    def _keys(c, grid):
        q = (np.clip(c, 0, 1) * grid).astype(np.int32)
        return [tuple(r) for r in q]

    def srgb(self) -> np.ndarray:
        return tdcolor.linear_to_srgb(self.colors)

    def stack(self, idx: int) -> list[int]:
        """Filament indices bottom-up for state `idx`, padded to max_layers.

        Short stacks pad at the BOTTOM with the base filament. That is free:
        base-colored layers sitting on the base plate are optically identical
        to the plate itself, so every pixel ends up the same total height.
        """
        out = []
        while idx > 0:
            out.append(int(self.fil[idx]))
            idx = int(self.parent[idx])
        out.reverse()
        pad = self.max_layers - len(out)
        return [self.base_index] * pad + out

    @property
    def base_index(self) -> int:
        for i, f in enumerate(self.filaments):
            if f.id == self.base.id:
                return i
        return 0

    def query(self, srgb_pixels: np.ndarray) -> np.ndarray:
        lab = tdcolor.srgb_to_lab(np.asarray(srgb_pixels, dtype=np.float64))
        flat = lab.reshape(-1, 3)
        _, idx = self.tree.query(flat, workers=-1)
        return idx.reshape(lab.shape[:-1])


# --------------------------------------------------------------------------


def solve_image(gamut, img, dither):
    """img (h,w,3) uint8 -> (h,w) gamut state index."""
    if dither == "none":
        return gamut.query(img)

    if dither == "ordered":
        h, w = img.shape[:2]
        spread = tdcolor.palette_spread(gamut.srgb()[:: max(1, len(gamut.colors) // 512)]) * 0.5
        tile = np.tile(tdcolor.bayer(8), (h // 8 + 1, w // 8 + 1))[:h, :w] - 0.5
        return gamut.query(np.clip(img + tile[:, :, None] * spread, 0, 255))

    if dither == "floyd":
        pal = gamut.srgb()
        return tdcolor.floyd_steinberg(
            img, pal, None, query=lambda c: gamut.tree.query(tdcolor.srgb_to_lab(c))[1]
        )

    raise SystemExit(f"unknown --dither {dither!r}")


def layer_labels(gamut, state_idx):
    """(h,w) state indices -> (max_layers, h, w) filament index per layer."""
    h, w = state_idx.shape
    uniq, inv = np.unique(state_idx, return_inverse=True)
    table = np.array([gamut.stack(int(u)) for u in uniq], dtype=np.int16)  # (U, L)
    return table[inv].reshape(h, w, gamut.max_layers).transpose(2, 0, 1)


def build_geometry(labels, width_mm, res, layer_h, base_h, base_index, n_fil):
    """labels (L,h,w) -> plaque mesh + one modifier volume per filament."""
    L, h, w = labels.shape
    total_h = base_h + L * layer_h
    plate_v = td3mf.box_verts(0, 0, 0, w * res, h * res, total_h)
    plate = td3mf.Item("plaque", plate_v, td3mf.BOX_TRIS.copy())

    builders = {f: td3mf.BoxBuilder() for f in range(n_fil) if f != base_index}
    for li in range(L):
        lay = labels[li]
        # Sit the box in the middle half of its layer so it unambiguously
        # belongs to that layer and cannot bleed into its neighbours.
        z0 = base_h + li * layer_h + 0.25 * layer_h
        z1 = base_h + li * layer_h + 0.75 * layer_h
        for f in np.unique(lay):
            if f == base_index:
                continue
            valid = lay == f
            for ry, cx, rh, rw, _ in td3mf.greedy_rects(lay, valid):
                builders[int(f)].add(
                    cx * res, (h - ry - rh) * res, z0,
                    (cx + rw) * res, (h - ry) * res, z1,
                )

    decals = []
    for f, b in builders.items():
        m = b.mesh()
        if m is not None:
            decals.append((f + 1, m[0], m[1]))
    return plate, decals, total_h


# --------------------------------------------------------------------------
# subset ranking
# --------------------------------------------------------------------------


def sample_pixels(img, n, seed=0):
    """A uniform sample of the image, which weights colors by the area they cover."""
    flat = img.reshape(-1, 3).astype(np.float64)
    if len(flat) <= n:
        return flat
    rng = np.random.default_rng(seed)
    return flat[rng.choice(len(flat), n, replace=False)]


def score_subset(fils, base, args, samples_rgb, samples_lab):
    """Mean/p95 dE of the best achievable match for a sampled set of colors."""
    g = Gamut(fils, base, args.layer_height, args.max_layers,
              args.grid, args.cap, verbose=False)
    idx = g.tree.query(samples_lab, workers=-1)[1]
    de = np.linalg.norm(g.lab[idx] - samples_lab, axis=-1)
    return de, g


def rank_subsets(all_fils, base, args, img, progress=None, verbose=True):
    """Score every `--slots`-sized subset that contains the base filament."""
    others = [f for f in all_fils if f.id != base.id]
    combos = list(itertools.combinations(others, args.slots - 1))
    if not combos:
        raise SystemExit(f"--slots {args.slots} needs more filaments than that")

    samples_rgb = sample_pixels(img, args.rank_samples)
    samples_lab = tdcolor.srgb_to_lab(samples_rgb)

    if verbose:
        print(f"ranking {len(combos)} combinations of {args.slots} "
              f"(base {base.name} always included), "
              f"{len(samples_rgb)} sampled pixels")
        if len(combos) > 200:
            print(f"  ! {len(combos)} combos will take a while; "
                  f"narrow --filaments or lower --grid")

    results = []
    t0 = time.time()
    for i, combo in enumerate(combos, 1):
        subset = [base] + list(combo)
        de, _ = score_subset(subset, base, args, samples_rgb, samples_lab)
        results.append({
            "fils": subset,
            "mean": float(de.mean()),
            "p95": float(np.percentile(de, 95)),
            "max": float(de.max()),
        })
        if progress:
            progress(i / len(combos),
                     f"scoring combination {i}/{len(combos)}  ({time.time()-t0:.0f}s)")
        if verbose and (i % 10 == 0 or i == len(combos)):
            print(f"  {i}/{len(combos)}  ({time.time()-t0:.0f}s)", end="\r", flush=True)
    if verbose:
        print()

    key = "mean" if args.rank_by == "mean" else "p95"
    results.sort(key=lambda r: r[key])
    return results


def render_candidates(results, base, args, img, top, progress=None):
    """Re-solve the full image for the best few so they can be looked at."""
    out = []
    for i, r in enumerate(results[:top], 1):
        g = Gamut(r["fils"], base, args.layer_height, args.max_layers,
                  args.grid, args.cap, verbose=False)
        state = g.query(img)
        out.append(dict(r, image=g.srgb()[state].astype(np.uint8)))
        if progress:
            progress(i / top, f"rendering candidate {i}/{top}")
    return out


def _font(size):
    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def contact_sheet(path, entries, target, cols=3, thumb=280, pad=14):
    """Grid of candidate renders, each with its filament swatches and error."""
    cells = [{"image": target, "label": "TARGET", "fils": None}] + [
        {
            "image": e["image"],
            "label": f"#{i+1}  dE mean {e['mean']:.1f}   p95 {e['p95']:.1f}",
            "fils": e["fils"],
        }
        for i, e in enumerate(entries)
    ]
    f_big, f_small = _font(15), _font(12)
    sw_h, txt_h = 20, 40
    cell_h = thumb + sw_h + txt_h
    rows = (len(cells) + cols - 1) // cols
    W = cols * thumb + (cols + 1) * pad
    H = rows * cell_h + (rows + 1) * pad

    sheet = Image.new("RGB", (W, H), (28, 28, 32))
    d = ImageDraw.Draw(sheet)

    for i, c in enumerate(cells):
        cx = pad + (i % cols) * (thumb + pad)
        cy = pad + (i // cols) * (cell_h + pad)
        im = Image.fromarray(c["image"]).resize((thumb, thumb), Image.LANCZOS)
        sheet.paste(im, (cx, cy))

        y = cy + thumb + 5
        if c["fils"]:
            sw_w = thumb // len(c["fils"])
            for j, fil in enumerate(c["fils"]):
                d.rectangle(
                    [cx + j * sw_w, y, cx + (j + 1) * sw_w - 2, y + sw_h - 6],
                    fill=tuple(int(v) for v in fil.rgb()),
                    outline=(70, 70, 76),
                )
        d.text((cx, y + sw_h - 2), c["label"], font=f_big, fill=(235, 235, 240))
        if c["fils"]:
            names = " + ".join(f.name for f in c["fils"])
            d.text((cx, y + sw_h + 16), names[:52], font=f_small, fill=(150, 150, 158))

    sheet.save(path)


def cmd_rank(all_fils, base, args, img):
    results = rank_subsets(all_fils, base, args, img)
    top = min(args.top, len(results))
    entries = render_candidates(results, base, args, img, top)

    print(f"\n{'rank':>4} {'mean dE':>8} {'p95':>6} {'max':>6}  filaments")
    for i, r in enumerate(results, 1):
        mark = " *" if i <= top else "  "
        print(f"{i:4d}{mark}{r['mean']:7.2f} {r['p95']:6.1f} {r['max']:6.1f}  "
              f"{', '.join(f.name for f in r['fils'])}")

    contact_sheet(args.rank_sheet, entries, img)
    print(f"\ncontact sheet -> {args.rank_sheet}  (target first, then top {top})")

    best = results[0]["fils"]
    print("\nBest combination:")
    print(f"  --filaments {','.join(f.id for f in best)} --base {base.id}")
    print("\nRe-run with that and -o to produce the plaque.")


# --------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Flat full-color plaques from per-pixel filament stacks.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("image")
    ap.add_argument("-o", "--output",
                    help="output .3mf; not needed when ranking combinations")
    ap.add_argument("--db", default="filaments.json")
    ap.add_argument("--filaments", required=True,
                    help="comma-separated filament ids or name substrings, in extruder order")
    ap.add_argument("--base",
                    help="opaque backing filament; must be one of --filaments and "
                         "OCCUPIES ONE TOOLHEAD (default: first in --filaments)")
    ap.add_argument("--width", type=float, default=150.0, help="plaque width in mm")
    ap.add_argument("--height", type=float, default=0.0, help="plaque height in mm (0 = from aspect)")
    ap.add_argument("--resolution", type=float, default=0.4,
                    help="mm per pixel; match your nozzle width")
    ap.add_argument("--layer-height", type=float, default=None,
                    help="must match what the slicer will use (default: from "
                         "--template, else 0.08)")
    ap.add_argument("--first-layer-height", type=float, default=None,
                    help="the first layer is usually thicker; it offsets every "
                         "colour layer above it (default: from --template, else "
                         "--layer-height)")
    ap.add_argument("--max-layers", type=int, default=16,
                    help="color layers above the base; the gamut stops growing "
                         "once the deepest stack goes opaque, so more is just time")
    ap.add_argument("--base-layers", type=int, default=5, help="opaque backing layers")
    ap.add_argument("--dither", choices=["none", "ordered", "floyd"], default="none",
                    help="rarely worth it here: vertical stacking already fills the "
                         "gamut densely, so dithering adds geometry without reducing "
                         "error. Try it only with 2-3 filaments")
    ap.add_argument("--fit", choices=["contain", "cover", "stretch"], default="cover")
    ap.add_argument("--grid", type=int, default=192,
                    help="gamut dedup resolution; higher = finer and slower")
    ap.add_argument("--cap", type=int, default=400_000, help="max gamut states")
    ap.add_argument("--flavor", choices=["orca", "prusa"], default="orca")
    ap.add_argument("--part-type", choices=["modifier", "part"], default="modifier")
    ap.add_argument("--no-force-solid", action="store_true",
                    help="leave the template's infill density alone. The colour "
                         "model assumes every layer is a continuous film, so only "
                         "use this if the profile is already fully solid")
    ap.add_argument("--template",
                    help="a project .3mf exported from your slicer; its printer, "
                         "filament and print settings are carried over verbatim so "
                         "the output opens as a project rather than bare geometry")
    ap.add_argument("--preview", help="PNG of the simulated printed result")
    ap.add_argument("--gamut-preview", help="PNG showing gamut coverage vs the target image")

    g = ap.add_argument_group(
        "combination ranking",
        "List more filaments than you have toolheads and this compares every "
        "subset that fits, so you can see which loadout suits the image before "
        "committing to a print.",
    )
    g.add_argument("--slots", type=int, default=4,
                   help="toolheads available, COUNTING THE BASE. --slots 4 means four "
                        "spools total, so 4 combines the base with 3 others")
    g.add_argument("--rank", action="store_true",
                   help="force ranking mode (automatic when --filaments exceeds --slots)")
    g.add_argument("--no-rank", action="store_true",
                   help="skip ranking and use the first --slots filaments as listed")
    g.add_argument("--top", type=int, default=5, help="combinations to render")
    g.add_argument("--rank-by", choices=["mean", "p95"], default="mean")
    g.add_argument("--rank-samples", type=int, default=4000,
                   help="pixels sampled when scoring; scoring every pixel is wasted effort")
    g.add_argument("--rank-sheet", default="combos.png", help="contact sheet output")
    args = ap.parse_args(argv)

    db = DB(args.db)
    fils = db.resolve(args.filaments)
    base = db.get(args.base) if args.base else fils[0]
    if base.id not in {f.id for f in fils}:
        raise SystemExit(f"--base {base.id} must also appear in --filaments")

    # Per-layer modifiers only land correctly on the grid the slicer will
    # actually use, so the template's profile wins unless overridden.
    t_lh, t_flh = td3mf.template_layer_settings(args.template) if args.template else (None, None)
    if args.layer_height is None:
        args.layer_height = t_lh or 0.08
        if t_lh:
            print(f"layer height {t_lh} mm (from template)")
    elif t_lh and abs(t_lh - args.layer_height) > 1e-9:
        print(f"  - overriding the template's {t_lh} mm layer height with "
              f"{args.layer_height} mm; the output profile is rewritten to match, "
              f"so the geometry and the settings still agree.")
    if args.first_layer_height is None:
        args.first_layer_height = t_flh or args.layer_height
        if t_flh:
            print(f"first layer {t_flh} mm (from template)")

    est = [f.id for f in fils if f.provenance != "measured"]
    print(f"filaments ({len(fils)}):")
    for i, f in enumerate(fils, 1):
        flag = "" if f.provenance == "measured" else "  <- not measured"
        print(f"  T{i}  {f.color}  td={f.td:.3f}  {f.label()}{flag}")
    print(f"base: {base.label()}")
    if est:
        print(f"\n  ! {len(est)} of {len(fils)} filaments have estimated optical data.")
        print("    Colors will be approximate until you run calibrate.py.\n")

    # --- geometry grid ---
    w_px = max(1, int(round(args.width / args.resolution)))
    im = Image.open(args.image)
    h_px = (
        max(1, int(round(args.height / args.resolution)))
        if args.height > 0
        else max(1, int(round(w_px * im.height / im.width)))
    )
    img = tdcolor.fit_image(args.image, w_px, h_px, args.fit)
    print(f"image: {w_px} x {h_px} px  ->  "
          f"{w_px*args.resolution:.1f} x {h_px*args.resolution:.1f} mm")

    # --- ranking mode ---
    ranking = args.rank or (len(fils) > args.slots and not args.no_rank)
    if ranking:
        if len(fils) <= args.slots:
            raise SystemExit(
                f"--rank needs more than --slots ({args.slots}) filaments to choose between"
            )
        cmd_rank(fils, base, args, img)
        return
    if len(fils) > args.slots:
        fils = fils[: args.slots]
        print(f"  --no-rank: using the first {args.slots} as listed")

    if not args.output:
        raise SystemExit("-o/--output is required when producing a plaque")

    # --- gamut ---
    print(f"building gamut (<= {args.max_layers} layers of {len(fils)} filaments):")
    gamut = Gamut(fils, base, args.layer_height, args.max_layers, args.grid, args.cap)

    # --- solve ---
    t0 = time.time()
    state = solve_image(gamut, img, args.dither)
    print(f"  solved {state.size} pixels in {time.time()-t0:.1f}s "
          f"({args.dither} dither)")

    achieved = gamut.srgb()[state]
    err = np.linalg.norm(
        tdcolor.srgb_to_lab(achieved.astype(np.float64)) - tdcolor.srgb_to_lab(img.astype(np.float64)),
        axis=-1,
    )
    print(f"  color error dE: mean {err.mean():.1f}, p95 {np.percentile(err, 95):.1f}, "
          f"max {err.max():.1f}")

    if args.preview:
        Image.fromarray(achieved.astype(np.uint8)).save(args.preview)
        print(f"  preview -> {args.preview}")
    if args.gamut_preview:
        _gamut_preview(args.gamut_preview, img, achieved, err)
        print(f"  gamut preview -> {args.gamut_preview}")

    # --- geometry ---
    labels = layer_labels(gamut, state)

    # Colour layers at the bottom that came out uniformly base are just extra
    # base plate -- base filament padding sitting on a base-filament plate. It
    # is optically identical to drop them, and it is that much less to print.
    trim = 0
    while trim < len(labels) - 1 and (labels[trim] == gamut.base_index).all():
        trim += 1
    if trim:
        labels = labels[trim:]
        print(f"  trimmed {trim} bottom colour layer(s) that were uniformly "
              f"{base.name} ({trim*args.layer_height:.2f} mm), optically identical")

    base_h = args.first_layer_height + (args.base_layers - 1) * args.layer_height

    # The whole colour model starts from "the base is an opaque backing". With
    # a realistic td that is not free: whites are far more transmissive than
    # they look, and a thin white base lets the build plate show through and
    # tint everything above it.
    t_base = float(base.transmittance(base_h).max())
    if t_base > 0.01:
        need = int(np.ceil(base.td_vec().max() * 4.6 / args.layer_height))
        print(f"  ! {args.base_layers} base layers of {base.name} ({base_h:.2f} mm) "
              f"still pass {100*t_base:.0f}% of the light reaching them.")
        print(f"    The gamut assumes an opaque backing, so the print will pick up "
              f"whatever is under it. Use --base-layers {need} ({need*args.layer_height:.2f} mm), "
              f"or a more opaque --base.")

    plate, decals, total_h = build_geometry(
        labels, args.width, args.resolution, args.layer_height,
        base_h, gamut.base_index, len(fils),
    )
    nbox = sum(len(t) // 12 for _, _, t in decals)
    print(f"plaque: {total_h:.2f} mm thick "
          f"({args.base_layers} base + {labels.shape[0]} color layers)")
    print(f"  {nbox} boxes in {len(decals)} modifier volumes ({nbox*12} triangles)")
    if nbox > 250_000:
        print(f"  ! that is a lot of geometry and your slicer will be slow to load it.")
        print(f"    Coarsen --resolution, drop --dither, or cut --max-layers.")

    used, counts = np.unique(labels, return_counts=True)
    tot = labels.size
    for f, c in zip(used, counts):
        print(f"  T{f+1} {fils[f].name:14} {100*c/tot:5.1f}% of layer-pixels")

    changes = sum(len(np.unique(labels[li])) for li in range(labels.shape[0]))
    print(f"  ~{changes} tool changes ({changes/labels.shape[0]:.1f} per layer)")

    td3mf.get_writer(args.flavor)(
        args.output, [plate], {0: decals}, gamut.base_index + 1, args.part_type,
        template=args.template, colors=[f.color for f in fils],
        layer_height=args.layer_height,
        first_layer_height=args.first_layer_height,
        solid=not args.no_force_solid,
        object_settings=None if args.no_force_solid else {
            "sparse_infill_density": "100%",
            "infill_combination": "0",
        },
    )
    print(f"wrote {args.output}")
    print(f"\nSlice with layer height EXACTLY {args.layer_height} mm and first "
          f"layer {args.first_layer_height} mm, or the modifiers land between "
          f"layers and colours drop out.")


def _gamut_preview(path, target, achieved, err):
    h, w = target.shape[:2]
    heat = np.clip(err / 20.0, 0, 1)
    heat_rgb = (np.stack([heat, 1 - heat, np.zeros_like(heat)], -1) * 255).astype(np.uint8)
    strip = np.concatenate([target, achieved.astype(np.uint8), heat_rgb], axis=1)
    Image.fromarray(strip).save(path)


if __name__ == "__main__":
    main()
