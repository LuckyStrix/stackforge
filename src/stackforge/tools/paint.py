#!/usr/bin/env python3
"""stackforge-paint -- colour an existing 3D model from a pattern or a wrapped image.

stackforge-plaque makes a flat plaque; stackforge-top-paint paints what faces upward. This colours
*any* 3MF, anywhere on its surface, by partitioning space rather than
unwrapping the mesh: the model is voxelised on the slicer's own layer grid, a
pattern function assigns a filament to every voxel near the surface, and each
layer's voxels become boxes emitted as per-extruder modifier volumes. The
slicer does the intersection with the real geometry, so the mesh is never
modified and needs no UVs. Design notes: docs/plans/surfacecolor.md.

    stackforge-paint badge.3mf -o out.3mf --filaments white,black \\
        --pattern checker3d --scale 6
    stackforge-paint globe.3mf -o mars.3mf --filaments white,red,orange,black \\
        --pattern image-spherical --image mars.jpg
    stackforge-paint vase.3mf -o out.3mf --filaments white,black \\
        --pattern expr --expr "sin(z/3 + theta*4) > 0"

Coordinates given to patterns (all mm, relative to the model's bounding box
unless noted): x, y, z from the box's minimum corner; r, theta (azimuth about
z, -pi..pi) and phi (elevation, -pi/2..pi/2) about its centre.
"""

from __future__ import annotations

import argparse
import ast
import heapq
import itertools
import math
import os
import time

import numpy as np
from PIL import Image
from scipy.ndimage import binary_erosion, distance_transform_edt

from stackforge.core import threemf
from stackforge.core.filamentdb import DEFAULT_DB
from stackforge.core import colormath, pose, spectral
from stackforge.tools import paint_budget, paint_stack

MAX_VOXELS = 60_000_000   # same ceiling top_paint applies to its raster
MIN_FEATURE_MM = 0.6      # narrowest region that gets plastic at a 0.4 mm nozzle (see CLAUDE.md)
EPS_JITTER = 1.7e-7   # pixel centres are nudged off exact triangle edges (see voxelize)

# --------------------------------------------------------------------------
# voxelisation on the slicer's layer grid
# --------------------------------------------------------------------------


def is_glb_path(path):
    from stackforge.core import glb
    return glb.is_glb_path(path)


def layer_edges(z_max, first, lh):
    """Layer boundaries: layer 0 is [0, first], layer k is [first+(k-1)lh, first+k*lh]."""
    n = max(1, int(math.ceil((z_max - first) / lh - 1e-9)) + 1)
    return np.concatenate([[0.0], first + np.arange(n) * lh])


def voxelize(item, bounds, res, edges):
    """Boolean occupancy (nz, ny, nx), sampled at each layer's mid-height.

    Casts a +Z ray up from every layer's mid-height through every pixel centre and
    sums the signed surface crossings above it (+1 where a triangle faces up,
    -1 where it faces down): a winding number, non-zero = inside. Not plain
    inside/outside parity, because 3MFs often hold overlapping shells that were
    never boolean-unioned (the badge fixture is a dome sitting in a plate, with
    an open bottom), and parity flips wrongly inside the overlap. Counting from
    above rather than below is what lets an open-bottomed shell resting on
    another shell still read as solid. Pixel centres are
    nudged by ~1e-7 mm so a ray never lands exactly on an edge shared by two
    triangles (which would count that crossing twice and flip the parity for the
    whole column). Assumes triangle normals are consistently oriented.
    """
    x0, y0, x1, y1 = bounds
    nx = max(1, int(math.ceil((x1 - x0) / res)))
    ny = max(1, int(math.ceil((y1 - y0) / res)))
    nz = len(edges) - 1
    mids = (edges[:-1] + edges[1:]) / 2
    if len(item.tris) == 0:
        return np.zeros((nz, ny, nx), dtype=bool)
    toggles = np.zeros((nz + 1, ny, nx), dtype=np.int32)

    px = x0 + (np.arange(nx) + 0.5) * res + EPS_JITTER
    py = y0 + (np.arange(ny) + 0.5) * res + EPS_JITTER * 1.3
    v, t = item.verts, item.tris
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    area2 = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    for i in np.nonzero(np.abs(area2) > 1e-12)[0]:
        p0, p1, p2 = a[i], b[i], c[i]
        cx0 = max(0, int(math.floor((min(p0[0], p1[0], p2[0]) - x0) / res)))
        cx1 = min(nx, int(math.ceil((max(p0[0], p1[0], p2[0]) - x0) / res)) + 1)
        cy0 = max(0, int(math.floor((min(p0[1], p1[1], p2[1]) - y0) / res)))
        cy1 = min(ny, int(math.ceil((max(p0[1], p1[1], p2[1]) - y0) / res)) + 1)
        if cx0 >= cx1 or cy0 >= cy1:
            continue
        gx, gy = px[cx0:cx1][None, :], py[cy0:cy1][:, None]
        w0 = ((p1[0] - gx) * (p2[1] - gy) - (p1[1] - gy) * (p2[0] - gx)) / area2[i]
        w1 = ((p2[0] - gx) * (p0[1] - gy) - (p2[1] - gy) * (p0[0] - gx)) / area2[i]
        w2 = 1.0 - w0 - w1
        hit = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not hit.any():
            continue
        z = (w0 * p0[2] + w1 * p1[2] + w2 * p2[2])[hit]
        k = np.searchsorted(mids, z)              # number of layer mids below the crossing
        yy, xx = np.nonzero(hit)
        np.add.at(toggles, (k, yy + cy0, xx + cx0), 1 if area2[i] > 0 else -1)
    cum = np.cumsum(toggles, axis=0)
    above = cum[-1][None] - cum[:nz]      # signed crossings above each layer's mid-height
    return above != 0


def shell_mask(occ, res, lh, depth):
    """Voxels within `depth` mm of the surface, by true distance to the outside.

    A distance transform, not "depth below the top", because on a steep wall
    Z-depth is not depth into the material. Distances are anisotropic (layers
    are lh tall, pixels res wide), which `sampling` accounts for. depth <= 0
    keeps everything.
    """
    if depth <= 0:
        return occ.copy()
    return occ & (surface_distance(occ, res, lh) <= depth)


def surface_distance(occ, res, lh):
    """Distance in mm from each voxel to the nearest outside voxel's centre (float32)."""
    padded = np.pad(occ, 1)          # the bounding box edge counts as outside
    return distance_transform_edt(padded, sampling=(lh, res, res))[1:-1, 1:-1, 1:-1].astype(np.float32)


# --------------------------------------------------------------------------
# patterns: fn(coords, args, n) -> int filament index per voxel (any int; taken mod n)
# --------------------------------------------------------------------------


class Coords:
    """Flat arrays of voxel-centre coordinates, plus derived polar ones."""

    def __init__(self, x, y, z, size, item=None, res=1.0, origin=(0.0, 0.0), cells=None):
        self.x, self.y, self.z, self.size = x, y, z, np.asarray(size, float)
        self.item, self.res, (self.x0, self.y0), self.cells = item, res, origin, cells
        c = self.size / 2
        dx, dy, dz = x - c[0], y - c[1], z - c[2]
        self.r = np.sqrt(dx**2 + dy**2 + dz**2)
        self.theta = np.arctan2(dy, dx)
        self.phi = np.arcsin(np.clip(dz / np.maximum(self.r, 1e-9), -1, 1))
        self.dx, self.dy, self.dz = dx, dy, dz


def _axis(cd, name):
    return {"x": cd.x, "y": cd.y, "z": cd.z}[name]


def p_checker3d(cd, a, n):
    s = a.scale
    return (np.floor(cd.x / s) + np.floor(cd.y / s) + np.floor(cd.z / s)).astype(int) % 2


def p_checker_sphere(cd, a, n):
    lat = np.floor((cd.phi + math.pi / 2) / math.pi * a.lat)
    lon = np.floor((cd.theta + math.pi) / (2 * math.pi) * a.lon)
    return (lat + lon).astype(int) % 2


def p_stripes(cd, a, n):
    return np.floor(_axis(cd, a.axis) / a.period).astype(int) % n


def p_gradient(cd, a, n):
    ax = _axis(cd, a.axis)
    size = cd.size["xyz".index(a.axis)]
    return np.clip((ax / max(size, 1e-9) * n).astype(int), 0, n - 1)


# Colour patterns are split in two: a target fn(coords, args) -> (N,3) linear RGB per voxel,
# then `_pick` onto the palette. Ranking scores filament sets against the targets in between.


def _strength(a):
    return 0.0 if _dither_mode(a) == "off" else getattr(a, "dither_strength", 1.0)


def _pick(target_lin, cd, a, palette):
    """Palette index per voxel: dithered if --dither is on, else nearest in Lab."""
    idx, blend = colormath.quantize_dither(target_lin, colormath.srgb_to_linear(palette),
                                           cd.cells, _strength(a))
    # dE per voxel, for main's report (which counts the surface voxels only)
    a.last_de = np.linalg.norm(colormath.linear_to_lab(blend) - colormath.linear_to_lab(target_lin),
                               axis=-1)
    return idx


def _dither_mode(a):
    return getattr(a, "dither", None) or ("ordered" if getattr(a, "pattern", None) == "texture" else "off")


def _sample(img, u, v):
    """Nearest-pixel lookup; u, v in [0,1], v=0 at the image top."""
    h, w = img.shape[:2]
    xi = np.clip((u * w).astype(int), 0, w - 1)
    yi = np.clip((v * h).astype(int), 0, h - 1)
    return img[yi, xi]


def _load(a):
    if not a.image:
        raise SystemExit(f"--pattern {a.pattern} needs --image")
    return np.asarray(colormath.open_image(a.image))


def _image_target(a, u, v):
    return colormath.srgb_to_linear(_sample(_load(a), u, v))


def _model_frame(cd, a):
    """`cd` in the model's own frame, before --rotate: an image is projected onto the faces
    it was made for, so a pose changes the layers, not the artwork. `a.unpose` is
    (fn taking posed points back, the unposed bbox origin, its size) from main."""
    up = getattr(a, "unpose", None)
    if up is None:
        return cd
    back, lo, size = up
    p = back(np.stack([cd.x0 + cd.x, cd.y0 + cd.y, cd.z], axis=1))
    return Coords(p[:, 0] - lo[0], p[:, 1] - lo[1], p[:, 2], size, item=cd.item, res=cd.res,
                  origin=(lo[0], lo[1]), cells=cd.cells)


def t_image_spherical(cd, a):
    """Equirectangular map (Earth/Mars/Moon style): u from azimuth, v from elevation."""
    cd = _model_frame(cd, a)
    u = ((cd.theta + math.pi) / (2 * math.pi) + a.lon_offset / 360.0) % 1.0
    v = 0.5 - cd.phi / math.pi
    return _image_target(a, u, v)


def t_image_cylindrical(cd, a):
    """Wrap around the vertical axis; v runs down the model's height."""
    cd = _model_frame(cd, a)
    u = ((cd.theta + math.pi) / (2 * math.pi) + a.lon_offset / 360.0) % 1.0
    v = 1.0 - cd.z / max(cd.size[2], 1e-9)
    return _image_target(a, u, v)


def t_image_planar(cd, a):
    """Project along --axis, like top_paint but for any direction."""
    cd = _model_frame(cd, a)
    ax = "xyz".index(a.axis)
    o = [i for i in range(3) if i != ax]
    co = [cd.x, cd.y, cd.z]
    u = co[o[0]] / max(cd.size[o[0]], 1e-9)
    v = 1.0 - co[o[1]] / max(cd.size[o[1]], 1e-9)
    return _image_target(a, np.clip(u, 0, 1), np.clip(v, 0, 1))


def t_texture(cd, a):
    """The model's own colours (GLB texture, vertex colours, material factors).

    Each voxel takes the mean colour of its nearest surface samples; the surface is
    sampled about twice per voxel so a texture finer than the voxels is averaged, not
    aliased.
    """
    from scipy.spatial import cKDTree
    from stackforge.core import glb
    if cd.item is None or cd.item.appearance is None:
        raise SystemExit("--pattern texture needs a GLB/glTF model (this input has no colours); "
                         "for a 3MF use an image-* pattern with --image")
    pts, cols = glb.surface_samples(cd.item, cd.res / 2)
    tree = cKDTree(pts)
    world = np.stack([cd.x0 + cd.x, cd.y0 + cd.y, cd.z], axis=1)
    k = min(4, len(pts))
    dist, nn = tree.query(world, k=k, workers=-1)
    if k == 1:
        dist, nn = dist[:, None], nn[:, None]
    w = 1.0 / (dist + cd.res * 0.25)
    return (cols[nn] * w[..., None]).sum(1) / w.sum(1)[:, None]


_EXPR_NAMES = {k: getattr(np, k) for k in (
    "sin", "cos", "tan", "arctan2", "sqrt", "abs", "floor", "ceil", "mod", "where",
    "minimum", "maximum", "exp", "log", "sign", "round")}
_EXPR_NAMES["pi"] = math.pi


_EXPR_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call,
    ast.Name, ast.Load, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.BitAnd, ast.BitOr,
    ast.BitXor, ast.Invert, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.keyword)


class _Floats(ast.NodeTransformer):
    """Numeric literals become floats, so `9**9**9` overflows instead of hanging on bigints."""

    def visit_Constant(self, node):
        if type(node.value) is int:
            return ast.copy_location(ast.Constant(float(node.value)), node)
        return node


def compile_expr(text, names):
    """Parse `text` and refuse anything but arithmetic, comparisons, `names` and calls to the
    functions among them. No attributes, subscripts, lambdas, comprehensions or strings, so
    there is nothing to escape through. Returns a code object for `eval` with no builtins."""
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as e:
        raise SystemExit(f"--expr: {e.msg}")
    for node in ast.walk(tree):
        if not isinstance(node, _EXPR_NODES):
            raise SystemExit(f"--expr: {type(node).__name__} is not allowed "
                             "(arithmetic, comparisons and the listed functions only)")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, bool)):
            raise SystemExit("--expr: only numeric constants are allowed")
        if isinstance(node, ast.Name) and node.id not in names:
            raise SystemExit(f"--expr: unknown name {node.id!r}")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name)
                                               and callable(names.get(node.func.id))):
            raise SystemExit("--expr: only the listed functions can be called")
    return compile(ast.fix_missing_locations(_Floats().visit(tree)), "<expr>", "eval")


def p_expr(cd, a, n):
    """Expression of x,y,z,r,theta,phi and a few numpy functions. Bool -> 0/1, number -> floor.

    The text is parsed and checked against a whitelist (see `compile_expr`) before it is
    evaluated, so it is safe on untrusted input.
    """
    if not a.expr:
        raise SystemExit("--pattern expr needs --expr")
    env = {**_EXPR_NAMES, "x": cd.x, "y": cd.y, "z": cd.z, "r": cd.r,
           "theta": cd.theta, "phi": cd.phi}
    code = compile_expr(a.expr, env)
    try:
        with np.errstate(all="ignore"):
            val = eval(code, {"__builtins__": {}}, env)
    except Exception as e:
        raise SystemExit(f"--expr failed: {type(e).__name__}: {e}")
    val = np.broadcast_to(np.asarray(val), cd.x.shape)
    if val.dtype == bool:
        return val.astype(int)
    return np.floor(val).astype(int)


PATTERNS = {
    "checker3d": p_checker3d,
    "checker-sphere": p_checker_sphere,
    "stripes": p_stripes,
    "gradient": p_gradient,
    "expr": p_expr,
}
TARGETS = {          # colour patterns: a target colour per voxel, matched to the palette
    "image-spherical": t_image_spherical,
    "image-cylindrical": t_image_cylindrical,
    "image-planar": t_image_planar,
    "texture": t_texture,
}
IMAGE_PATTERNS = {"image-spherical", "image-cylindrical", "image-planar"}
COLOUR_PATTERNS = set(TARGETS)      # patterns that need a palette, not just a count


def evaluate(pattern, cd, args, palette, target=None):
    """Palette index per voxel. `target` is the colour pattern's output, if already computed."""
    n = len(palette)
    if pattern in TARGETS:
        idx = _pick(TARGETS[pattern](cd, args) if target is None else target, cd, args, palette)
    else:
        idx = PATTERNS[pattern](cd, args, n)
    return np.asarray(idx).astype(int) % n


# --------------------------------------------------------------------------
# choosing the filaments
# --------------------------------------------------------------------------

RANK_COLOURS = 20_000   # distinct target colours scored per set, as plaque's --rank-samples


def printed_palette(fils, base, depth, optics="spectral"):
    """(sRGB (n,3) each filament shows on the surface, notes).

    spectral: a calibrated filament is `depth` mm of it over the base's bulk colour
    (Kubelka-Munk, as plaque), so its colour is the measured one rather than the library
    swatch, and a translucent one shows the base through. depth 0 colours all the way
    through, so that is the bulk colour. An uncalibrated filament keeps its swatch; so does
    everything with optics "swatch". Opaque voxels of one filament each: no stacking.
    """
    notes = []
    if optics == "swatch":
        return np.array([f.rgb() for f in fils], float), notes
    Rb = spectral.r_inf(*spectral.ks(base)) if spectral.is_calibrated(base) else None
    out = []
    for f in fils:
        if not spectral.is_calibrated(f):
            out.append(f.rgb())
            continue
        K, S = spectral.ks(f)
        R = spectral.r_inf(K, S) if depth <= 0 or Rb is None else spectral.layer(Rb, K, S, depth)
        out.append(np.clip(spectral.spectrum_to_srgb(R), 0, 255))
        for w in f.spectral.get("warnings") or []:
            notes.append(f"{f.label()}: its calibration was written with a warning: {w}")
        if f.spectral.get("synthetic"):
            notes.append(f"{f.label()}: SYNTHETIC calibration, not a measurement")
    plain = [f.label() for f in fils if not spectral.is_calibrated(f)]
    if plain:
        notes.append("no spectral calibration, so the library swatch stands in for what prints: "
                     + ", ".join(plain))
    if Rb is None and len(plain) < len(fils) and depth > 0:
        notes.append(f"the base ({base.label()}) has no calibration, so translucent filaments "
                     f"are taken at their bulk colour, as if nothing showed through")
    return np.array(out, float), notes


def score_palette(rgb, palette, args):
    """dE (k,) of each target colour (sRGB 0..255) against what `palette` shows for it: the
    area average of the dithered pair, or the nearest filament with --dither off. The same
    pick `_pick` makes, minus the per-voxel threshold, which does not move the average."""
    _, blend = colormath.quantize_dither(colormath.srgb_to_linear(rgb), colormath.srgb_to_linear(palette),
                                         np.zeros((len(rgb), 3), int), _strength(args))
    return np.linalg.norm(colormath.linear_to_lab(blend) - colormath.srgb_to_lab(rgb), axis=-1)


def _prints_as(swatch_hex, rgb):
    shown = colormath.to_hex(np.round(rgb))
    return "" if shown.upper() == swatch_hex.upper() else f"  -> prints as {shown}"


def histogram(targets_lin, max_colours=RANK_COLOURS):
    """(rgb (k,3) sRGB, weight (k,)) of linear target colours, as `colormath.colour_histogram`."""
    srgb = np.round(colormath.linear_to_srgb(targets_lin)).astype(np.int64)
    return colormath.colour_histogram(srgb, max_colours)


def _summary(de, weight):
    return {"mean": float(np.average(de, weights=weight)),
            "p95": colormath.weighted_percentile(de, weight, 95), "max": float(de.max())}


def rank_sets(palettes, bases, slots, targets_lin, args, base_first=True):
    """Every `slots`-sized set of palette indices (all of them, if there are fewer) with a
    base from `bases`, scored against the voxels' target colours (weighted by voxel count),
    best first.

    `palettes[b]` is every candidate's colour as it prints over base b (`printed_palette`).
    A set is tried with each of its members that is in `bases` as the base. `usage` is the
    share of the surface whose nearest filament is the base, for `pick_best`'s tie-break.
    With `base_first`, `keep` lists the base first, so it lands on extruder 1: Flash
    Studio's GUI honours a 3MF's object extruder, but its headless slice prints the object
    on 1 whatever it says (sliced 2026-10-17), so a check sliced that way would test the
    wrong filament. Without it `keep` stays in the listed order, which is the user's slots.
    """
    rgb, weight = histogram(targets_lin)
    n = len(next(iter(palettes.values())))
    lab = colormath.srgb_to_lab(rgb)
    out = []
    for combo in itertools.combinations(range(n), min(slots, n)):
        for b in (k for k in combo if k in bases):
            keep = [b] + [k for k in combo if k != b] if base_first else list(combo)
            pal = palettes[b][keep]
            de = score_palette(rgb, pal, args)
            near = ((lab[:, None] - colormath.srgb_to_lab(pal)[None]) ** 2).sum(-1).argmin(1)
            usage = float(weight[near == keep.index(b)].sum() / weight.sum())
            out.append({"keep": keep, "base": b, "usage": usage, **_summary(de, weight)})
    out.sort(key=lambda r: r[args.rank_by])
    return out


TIE_DE = 0.1    # scores this close are a tie: nobody sees 0.1 dE, everybody sees fewer boxes


def pick_best(rows, key):
    """The best row by `key`; among those within TIE_DE of it, the one whose base covers most
    of the surface (base voxels need no modifier). Swatches and opaque filaments do not
    change with the base, so without this the base would be whichever was tried first."""
    best = min(r[key] for r in rows)
    return max((r for r in rows if r[key] <= best + TIE_DE), key=lambda r: r["usage"])


# --------------------------------------------------------------------------
# labels -> boxes
# --------------------------------------------------------------------------


def shell_coords(shell, bounds, res, edges, size, item=None):
    """((zs, ys, xs), Coords) of the voxels in `shell`; Coords is None when it is empty."""
    x0, y0 = bounds[:2]
    zs, ys, xs = np.nonzero(shell)
    if len(zs) == 0:
        return (zs, ys, xs), None
    mids = (edges[:-1] + edges[1:]) / 2
    cd = Coords(xs * res + res / 2, ys * res + res / 2, mids[zs], size, item=item,
                res=res, origin=(x0, y0), cells=np.stack([xs, ys, zs], axis=1))
    return (zs, ys, xs), cd


def label_grid(occ, shell, bounds, res, edges, pattern, args, palette, item=None,
               coords=None, target=None):
    """(nz,ny,nx) int labels; -1 where there is nothing to colour. `coords` and `target`
    are `shell_coords` and the pattern's target colours, when already computed."""
    lab = np.full(occ.shape, -1, dtype=np.int16)
    (zs, ys, xs), cd = coords or shell_coords(shell, bounds, res, edges, args.size, item)
    if cd is None:
        return lab
    lab[zs, ys, xs] = evaluate(pattern, cd, args, palette, target)
    return lab


def wall_settings(flavor) -> dict:
    """Per-object overrides: Arachne walls with a 50% minimum bead.

    Classic walls give a region no perimeter under ~0.5 mm across (sliced cube, 2026-10-17:
    wall bands of 0.2-0.4 mm printed nothing); Arachne with the template's 85% bead still
    left 27% of a painted head's surface without plastic, 50% left 5% (25%: the same).
    Per object, so the template is untouched; the headless slice honours it (identical to
    the project setting). The prusa keys are PrusaSlicer's names and unverified on a slice.
    """
    if flavor == "orca":
        return {"wall_generator": "arachne", "min_bead_width": "50%"}
    return {"perimeter_generator": "arachne", "min_bead_width": "50%"}


def default_wall_band(walls):
    """The thinnest band a wall prints, by --walls (see paint_stack)."""
    return paint_stack.ARACHNE_WALL_BAND_MM if walls == "arachne" else paint_stack.WALL_BAND_MM


OVERHANG_VOX = 1.5  # voxels the boxes reach past the surface: a voxel's side and its diagonal


def overhang(lab, occ, reach=OVERHANG_VOX):
    """(labels padded by `pad` voxels on each side in XY, pad): every voxel outside the model
    within `reach` voxels of a labelled one (same layer) takes its label.

    The voxel boxes stop at the voxel staircase, short of the true curved surface. What is
    left between them and the mesh is a sliver under 0.4 mm wide in the object's own (base)
    filament: classic walls drop it, leaving a pit, and Arachne prints it, so the base shows
    (sliced head, 2026-10-17: 21% of the surface pitted; with Arachne 30-50% printed base).
    Reaching past the surface leaves no sliver; the slicer clips a modifier to its object.
    Voxels inside the model keep their labels, -1 included (the core under the shell)."""
    pad = int(math.ceil(reach))
    wide = ((0, 0), (pad, pad), (pad, pad))
    lab = np.pad(lab, wide, constant_values=-1)
    out = ~np.pad(occ, wide, constant_values=False)
    for k in range(lab.shape[0]):
        has = lab[k] >= 0
        if not has.any():
            continue
        d, (iy, ix) = distance_transform_edt(~has, return_indices=True)
        grow = out[k] & (d <= reach + 1e-9)
        lab[k][grow] = lab[k][iy[grow], ix[grow]]
    return lab, pad


def emit_boxes(lab, bounds, res, edges, base_index):
    """One BoxBuilder per non-base filament. Boxes sit in the middle half of each
    layer: slicers sample at mid-height, so they are caught regardless of
    rounding and never spill into the neighbouring layer."""
    x0, y0, _, _ = bounds
    builders = {}
    for k in range(lab.shape[0]):
        layer = lab[k]
        valid = layer >= 0
        if not valid.any():
            continue
        lo, hi = edges[k], edges[k + 1]
        z0, z1 = lo + 0.25 * (hi - lo), lo + 0.75 * (hi - lo)
        for ry, cx, rh, rw, f in _rects(layer, valid):
            if f == base_index:
                continue
            builders.setdefault(f, threemf.BoxBuilder()).add(
                x0 + cx * res, y0 + ry * res, z0, x0 + (cx + rw) * res, y0 + (ry + rh) * res, z1)
    out = []
    for f, bb in sorted(builders.items()):
        m = bb.mesh()
        if m is not None:
            out.append((f + 1, m[0], m[1]))
    return out


def _rects(layer, valid):
    return threemf.greedy_rects(layer, valid)


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------


def save_preview(path, lab, palette, edges=None, res=None, shade=None):
    """Top view and front view of the colour of the first coloured voxel seen.

    `shade` (nz,ny,nx,3 uint8), when given, is each voxel's colour; else its filament's.
    Stacked voxels pass the colour of their whole stack, which is what the eye sees there.
    A voxel is `res` wide but one layer tall, so the front view is resampled to `res` per
    pixel in z too; drawn one row per layer, a 0.08 mm grid stretched it tenfold.
    """
    pal = np.round(np.asarray(palette)).astype(np.uint8)
    background = np.array([30, 30, 34], np.uint8)

    def first_hit(axis, reverse):
        """2-D colours of the first coloured voxel along `axis`; only the stacked path
        passes a colour volume, the rest are coloured from their 2-D labels."""
        v = np.flip(lab, axis) if reverse else lab
        has = v >= 0
        idx = np.expand_dims(np.argmax(has, axis=axis), axis)
        if shade is None:
            got = pal[np.maximum(np.take_along_axis(v, idx, axis).squeeze(axis), 0)]
        else:
            sh = np.flip(shade, axis) if reverse else shade
            got = np.take_along_axis(sh, idx[..., None], axis).squeeze(axis)
        return np.where(has.any(axis=axis)[..., None], got, background)

    top = first_hit(0, True)[::-1]                 # looking down; y up on the page
    front = first_hit(1, False)                    # looking along +y, row = layer
    if edges is not None and res:
        rows = max(1, int(round(edges[-1] / res)))
        layer = np.searchsorted(edges, (np.arange(rows) + 0.5) * res) - 1
        front = front[np.clip(layer, 0, len(front) - 1)]
    front = front[::-1]                             # z up
    a, b = top, front
    hh = max(a.shape[0], b.shape[0])
    def pad(im, bottom):
        out = np.full((hh, im.shape[1], 3), 30, np.uint8)
        if bottom:
            out[hh - im.shape[0]:] = im      # z=0 sits on the sheet's floor
        else:
            out[:im.shape[0]] = im
        return out
    sheet = np.concatenate([pad(a, False), np.full((hh, 4, 3), 90, np.uint8), pad(b, True)], axis=1)
    Image.fromarray(sheet).save(path)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("model", help="a 3MF, or a GLB/glTF (its own colours are used by --pattern texture)")
    ap.add_argument("-o", "--output", required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--palette", help="filament colours in extruder order, comma-separated hex")
    src.add_argument("--filaments", help="comma-separated ids from the filament database")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--base-extruder", type=int, default=None,
                    help="extruder the object prints in, the core under the coloured shell; voxels "
                         "given it emit no modifier. Default: for image-* and texture, chosen with "
                         "the filaments (best score; on a tie, the one covering most of the "
                         "surface); else 1")
    ap.add_argument("--stack", choices=["on", "off"], default="on",
                    help="on: stack translucent filaments in bands along the surface normal, as "
                         "plaque stacks layers (image-* and texture; needs every filament "
                         "spectrally calibrated, else one filament per voxel). off: one filament "
                         "per voxel")
    ap.add_argument("--walls", choices=["arachne", "template"], default="arachne",
                    help="arachne: the painted objects get Arachne walls with a 50%% minimum bead, "
                         "so thin colour bands and one-voxel features print. template: leave the "
                         "template's wall settings (classic walls drop regions under ~0.5 mm)")
    ap.add_argument("--wall-band", type=float, default=None,
                    help="thinnest band, mm, the slicer prints on a wall; top faces stack per layer. "
                         f"Default {paint_stack.ARACHNE_WALL_BAND_MM} with --walls arachne, "
                         f"{paint_stack.WALL_BAND_MM} with classic walls")
    ap.add_argument("--optics", choices=["spectral", "swatch"], default="spectral",
                    help="spectral: a calibrated filament is matched as it prints, --depth mm of "
                         "it over the base (measured colour; translucent ones show the base). "
                         "Uncalibrated filaments use their swatch either way. swatch: the "
                         "library hex for all")
    ap.add_argument("--slots", type=int, default=None,
                    help="filament slots, COUNTING THE BASE. With more filaments than slots, every "
                         "set of this size that keeps the base is scored against the model's "
                         "colours and the best is used (image-* and texture patterns). "
                         "Default: the --template's slot count")
    ap.add_argument("--rank-by", choices=["mean", "p95"], default="mean",
                    help="what the best set minimises: mean dE, or the 95th percentile")
    ap.add_argument("--top", type=int, default=5, help="ranked sets to list")
    ap.add_argument("--pattern", choices=["texture"] + sorted((set(PATTERNS) | set(TARGETS)) - {"texture"}),
                    help="texture = the model's own colours (GLB); image-* wrap an --image; the "
                         "rest are decorative. Default: texture for a GLB, required for a 3MF")
    ap.add_argument("--dither", choices=["off", "ordered"], default=None,
                    help="mix the two nearest filaments across neighbouring voxels so the area "
                         "average matches the colour. Default: ordered for texture, else off. "
                         "With --walls template it needs --resolution >= 0.6 to print")
    ap.add_argument("--dither-strength", type=float, default=1.0,
                    help="0 = none, 1 = full mix between the two nearest filaments")
    ap.add_argument("--scale-to", type=float, default=None,
                    help="GLB only: scale so the largest dimension is this many mm "
                         "(default: the file is in metres)")
    ap.add_argument("--scale", type=float, default=5.0, help="checker3d cell size, mm")
    ap.add_argument("--lat", type=int, default=8, help="checker-sphere latitude bands")
    ap.add_argument("--lon", type=int, default=16, help="checker-sphere longitude bands")
    ap.add_argument("--axis", choices=["x", "y", "z"], default="z",
                    help="stripes / gradient / image-planar axis")
    ap.add_argument("--period", type=float, default=4.0, help="stripes period, mm")
    ap.add_argument("--image", help="image for the image-* patterns")
    ap.add_argument("--lon-offset", type=float, default=0.0,
                    help="degrees to rotate a wrapped image about the vertical axis")
    ap.add_argument("--expr", help="expression for --pattern expr: x y z r theta phi, arithmetic, comparisons and sin cos tan arctan2 sqrt abs floor ceil mod where minimum maximum exp log sign round")
    ap.add_argument("--resolution", type=float, default=0.8,
                    help="mm per voxel in XY. Coarser than the nozzle keeps the box count sane")
    ap.add_argument("--depth", type=float, default=1.2,
                    help="colour this many mm into the surface (0 = all the way through)")
    ap.add_argument("--layer-height", type=float, default=None,
                    help="must match the slicer (default: from --template, else 0.2)")
    ap.add_argument("--first-layer-height", type=float, default=None)
    ap.add_argument("--flavor", choices=["orca", "prusa"], default="orca")
    ap.add_argument("--part-type", choices=["modifier", "part"], default="modifier")
    ap.add_argument("--template", help="a project .3mf exported from your slicer")
    ap.add_argument("--preview", help="PNG: top and front view of the painted colours")
    ap.add_argument("--max-toolchanges", type=int, default=None,
                    help="limit the tool changes: each layer prints only some of the "
                         "filaments (always the base), chosen for the least colour error "
                         "within this many changes over the print (image-* and texture). "
                         "The count is an upper bound; the slicer can only come in under it")
    ap.add_argument("--rotate", default=None,
                    help="pose the model first: X,Y,Z degrees, turned about X, then Y, then Z "
                         "and set back on the bed. Fewer layers can mean fewer tool changes. "
                         "Pose it here, not in the slicer: rotating the painted 3MF there "
                         "puts the colour between layers. An image-* pattern stays on the "
                         "faces it is projected onto unposed")
    ap.add_argument("--dry-run", action="store_true",
                    help="do everything but write the 3MF (the GUI's tool-change estimate)")
    ap.add_argument("--toolchange-curve", default=None,
                    help="JSON: colour error against tool changes, for choosing "
                         "--max-toolchanges (the GUI's slider reads it). Also printed")
    return ap


SCREEN_COLOURS = 2_000   # per band class, when every stackable pair is screened with stacks
SCREEN_MAX = 2_000       # stackable pairs screened at most (~0.1 s each), best flat first
STACK_SHORTLIST = 6      # screened pairs rescored on the full RANK_COLOURS before choosing


class Shell:
    """One item's coloured voxels: where they are, what colour they want, which of them are
    on the surface, and (stacking) their band class, depth below it and column anchor.

    `surf` marks the voxels touching the outside, one per point of the surface. Scores and
    the reported error count those: counted per voxel, a top face (15 voxels deep at 0.08 mm
    layers) would outweigh the same area of wall (2 voxels at 0.6 mm) sevenfold.
    """

    def __init__(self, index, item, occ, shell, coords, target, geo=None):
        self.index, self.item, self.occ, self.shell = index, item, occ, shell
        self.coords, self.target, self.geo = coords, target, geo
        (zs, ys, xs), _ = coords
        self.surf = (occ & ~binary_erosion(occ, border_value=0))[zs, ys, xs]


def stack_geometry(dist, coords, edges, classes, args):
    """Per voxel: band class, depth below the surface, column anchor and target colour.

    Class and target are the column's, read at its outermost voxel (`paint_stack.columns`),
    so every voxel of a column solves to the same stack; only the band differs.
    """
    (zs, ys, xs), cd = coords
    res, lh = args.resolution, args.layer_height
    nrm = paint_stack.normals(dist, zs, ys, xs, res, lh)
    d = paint_stack.true_depth(dist[zs, ys, xs], nrm, res, lh)
    anchors = paint_stack.anchor_cells(np.stack([xs, ys, zs], axis=1), nrm, d, res, lh)
    anchors = np.clip(anchors, 0, np.array(dist.shape[::-1]) - 1)
    col, top = paint_stack.columns(anchors, d)
    cls = paint_stack.classify(nrm[top], lh, res, classes, args.wall_band)[col]
    a = anchors[top]
    mids = (edges[:-1] + edges[1:]) / 2
    at_top = Coords(a[:, 0] * res + res / 2, a[:, 1] * res + res / 2, mids[a[:, 2]], args.size,
                    item=cd.item, res=res, origin=(cd.x0, cd.y0), cells=a)
    return {"cls": cls, "depth": d, "cells": anchors,
            "target": TARGETS[args.pattern](at_top, args)[col]}


def _row(r, names):
    return (f"{r['mean']:8.2f} {r['p95']:6.2f} {r['max']:6.2f}  "
            + ", ".join(names[k] + (" [base]" if k == r["base"] else "") for k in r["keep"]))


def choose_filaments(fils, palettes, bases, slots, shells, names, args, classes=None,
                     base_first=True):
    """The best (set, base) for the shells' colours: a dict with keep, base and stacks.

    Every pair is scored one filament per voxel (`rank_sets`). With band `classes`, every
    pair that can stack (all calibrated) is screened with stacks on a coarse histogram
    (SCREEN_COLOURS per class) and the best STACK_SHORTLIST are rescored on the full one. All
    of them, not just the flat ranking's best: that ranking sees each filament as opaque, so
    it would never shortlist the translucent sets stacking is for. A set that cannot stack
    competes on its flat score, but has to beat the best stacked set by more than TIE_DE:
    their tie-breaks measure different things, and a tie should not turn stacking off.
    Everything is scored on the surface voxels (`Shell.surf`), one per point of the surface.
    stacks is the winner's `paint_stack.Stacks`, or None to paint one filament per voxel.
    """
    t0 = time.time()
    with_target = [s for s in shells if s.target is not None]
    targets = np.concatenate([s.target[s.surf] for s in with_target])
    key = args.rank_by
    ranked = rank_sets(palettes, bases, slots, targets, args, base_first)
    what = f"{len(ranked)} set/base pairs" if len(bases) > 1 else f"{len(ranked)} sets"
    print(f"\nranked {what} of {min(slots, len(names))} on {len(targets) / 1e3:.0f} k surface "
          f"voxels' colours, one filament per voxel ({time.time() - t0:.0f}s)")
    print(f"\n{'rank':>4} {'mean dE':>8} {'p95':>6} {'max':>6}  filaments")
    for i, r in enumerate(ranked[:args.top], 1):
        print(f"{i:>4} {_row(r, names)}")
    if not classes:
        win = pick_best(ranked, key)
        if win is not ranked[0]:
            print(f"  chosen: {_row(win, names)}\n    (within {TIE_DE} dE of the best, and its "
                  f"base covers most of the surface)")
        return {**win, "stacks": None}

    def stackable(r):
        return all(spectral.is_calibrated(fils[k]) for k in r["keep"])

    can = [r for r in ranked if stackable(r)]
    flat = [r for r in ranked if not stackable(r)]
    if not can:
        print("\nno candidate set is fully calibrated, so none can stack")
        return {**pick_best(ranked, key), "stacks": None}

    t0 = time.time()
    cls = np.concatenate([s.geo["cls"][s.surf] for s in with_target])

    def hists(n):
        return [histogram(targets[cls == c], n) if (cls == c).any() else None
                for c in range(len(classes))]

    def stacks_for(r):
        return paint_stack.Stacks([fils[k] for k in r["keep"]], r["keep"].index(r["base"]),
                                  classes)

    def scored(r, st, h):
        de, w, base = st.score(h, mix)
        return {**r, **_summary(de, w), "usage": float(np.average(base, weights=w)), "stacks": st}

    mix = _strength(args) > 0
    if len(can) > SCREEN_MAX:
        print(f"\n  ! {len(can)} pairs can stack; screening the {SCREEN_MAX} best one filament "
              f"per voxel (list fewer --filaments to screen them all)")
        can = can[:SCREEN_MAX]
    if len(can) > STACK_SHORTLIST:
        # Keep only the best few Stacks while screening: a capped gamut is ~60 MB of spectra.
        print(f"\nscreening {len(can)} pair(s) that can stack, with stacks ...", flush=True)
        coarse, heap = hists(SCREEN_COLOURS), []
        for i, r in enumerate(can):
            st = stacks_for(r)
            de, w, _ = st.score(coarse, mix)
            item = (-float(_summary(de, w)[key]), i, r, st)
            if len(heap) < STACK_SHORTLIST:
                heapq.heappush(heap, item)
            else:
                heapq.heappushpop(heap, item)
        pairs = [(r, st) for _, _, r, st in heap]
    else:
        pairs = [(r, stacks_for(r)) for r in can]
    full = hists(RANK_COLOURS)
    short = sorted((scored(r, st, full) for r, st in pairs), key=lambda r: r[key])
    win = pick_best(short, key)
    best_flat = pick_best(flat, key) if flat else None
    if best_flat is not None and best_flat[key] < win[key] - TIE_DE:
        win = best_flat
    print(f"\n{'rank':>4} {'mean dE':>8} {'p95':>6} {'max':>6}  stacked ({time.time() - t0:.0f}s)")
    for i, r in enumerate(short, 1):
        print(f"{i:>3}{'*' if r is win else ' '} {_row(r, names)}"
              + ("   (a gamut hit its cap)" if r["stacks"].capped else ""))
    if "stacks" not in win:
        print(f"  * a set that cannot stack scores better flat: {_row(win, names)}")
        return {**win, "stacks": None}
    if win is not short[0]:
        print(f"  * within {TIE_DE} dE of the best, so the base that covers most of the "
              f"surface wins (fewer modifier boxes)")
    return win


def label_stacked(s, stacks, args):
    """(labels volume, sRGB of each voxel's own stack) for one shell, painted with stacks."""
    lab = np.full(s.occ.shape, -1, dtype=np.int16)
    (zs, ys, xs), _ = s.coords
    g = s.geo
    state, shown, own = stacks.solve(s.target, g["cls"], g["cells"], _strength(args))
    lab[zs, ys, xs] = stacks.labels(g["cls"], state, stacks.band(g["cls"], g["depth"]))
    args.last_de = np.linalg.norm(colormath.linear_to_lab(shown) - colormath.linear_to_lab(s.target),
                                  axis=-1)
    return lab, colormath.linear_to_srgb(own)


def _hours(changes, secs):
    """' (~1.2 h at 7 s each)' from the template's tool-change time, or ''."""
    return f" (~{changes * secs / 3600:.1f} h at {secs:g} s each)" if secs else ""


def budget_tools(shells, palette, stacks, fils, nz, change_time, args):
    """{shell index: (labels, dE, own sRGB) per coloured voxel} under --max-toolchanges, or
    None when only the curve was asked for. Prints the curve (and writes it, with
    --toolchange-curve) and the plan chosen."""
    def flat(s, pal):
        return _pick(s.target, s.coords[1], args, pal), args.last_de

    if len(palette) > paint_budget.MAX_TOOLS and args.max_toolchanges is None:
        # only the curve was asked for (the GUI asks on every colour run): not worth failing
        print(f"\n  no tool-change curve: it takes at most {paint_budget.MAX_TOOLS} filaments, "
              f"and {len(palette)} are in use")
        return None
    b = paint_budget.Budget(shells, palette, stacks, fils, args.base_extruder - 1, args, nz,
                            pick={"strength": _strength(args), "flat": flat},
                            keep_own=bool(args.preview))
    ev = b.choose(args.max_toolchanges) if args.max_toolchanges is not None else None
    if args.toolchange_curve:
        rows = b.curve()
        print("\n  tool changes against colour error (changes at most; dE simulated, over the "
              "surface):")
        print(f"  {'changes':>8} {'mean dE':>8} {'p95':>6}  layers printing 1..{len(palette)}")
        for i, r in enumerate(rows):
            tag = "  (every filament on every layer it needs)" if i == len(rows) - 1 else ""
            print(f"  {r['changes']:>8} {r['mean']:8.2f} {r['p95']:6.2f}  "
                  f"{' / '.join(map(str, r['layers']))}{_hours(r['changes'], change_time)}{tag}")
        paint_budget.curve_json(args.toolchange_curve, rows, change_time, ev)
        print(f"  curve -> {args.toolchange_curve}")
    if ev is None:
        return None
    note = "" if ev["changes"] <= args.max_toolchanges else \
        f"; nothing fits {args.max_toolchanges}, this is the fewest"
    print(f"\n  --max-toolchanges {args.max_toolchanges}: at most {ev['changes']} changes"
          f"{_hours(ev['changes'], change_time)}, mean {ev['mean']:.2f} dE, p95 "
          f"{ev['p95']:.2f}{note}")
    return {sh.s.index: (ev["labels"][i], ev["de"][i], ev["own"][i])
            for i, sh in enumerate(b.shells)}


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.wall_band is None:
        args.wall_band = default_wall_band(args.walls)
    fils = None
    if args.filaments:
        from stackforge.core.filamentdb import DB
        fils = DB(args.db).resolve(args.filaments)
        palette = np.array([f.rgb() for f in fils], float)      # until --optics, below
        colors = [f.color for f in fils]
        names = [f.label() for f in fils]
    else:
        palette = colormath.parse_palette(args.palette)
        colors = [colormath.to_hex(c) for c in palette]
        names = list(colors)
    if args.base_extruder is not None and not 1 <= args.base_extruder <= len(palette):
        raise SystemExit(f"--base-extruder must be 1 to {len(palette)}")

    for flag, val in (("--resolution", args.resolution), ("--layer-height", args.layer_height),
                      ("--first-layer-height", args.first_layer_height),
                      ("--scale-to", args.scale_to), ("--wall-band", args.wall_band)):
        if val is not None and not val > 0:
            raise SystemExit(f"{flag} must be positive (got {val})")
    if args.depth < 0 or not math.isfinite(args.depth):
        raise SystemExit(f"--depth must be 0 or more (got {args.depth})")
    if not 0 <= args.dither_strength <= 1:
        raise SystemExit("--dither-strength must be between 0 and 1")
    glb_input = is_glb_path(args.model)
    if args.pattern is None:
        if not glb_input:
            raise SystemExit("--pattern is required for a 3MF (a GLB defaults to texture)")
        args.pattern = "texture"
    colour = args.pattern in COLOUR_PATTERNS
    try:
        angles = pose.parse_rotate(args.rotate) if args.rotate else None
    except ValueError as e:
        raise SystemExit(str(e))
    budgeting = args.max_toolchanges is not None or bool(args.toolchange_curve)
    if budgeting and not colour:
        raise SystemExit("--max-toolchanges and --toolchange-curve need a colour pattern "
                         "(image-* or texture): a decorative pattern has no colour to trade")
    if args.max_toolchanges is not None and args.max_toolchanges < 0:
        raise SystemExit(f"--max-toolchanges must be 0 or more (got {args.max_toolchanges})")

    slots = args.slots or (threemf.template_slots(args.template) if args.template else None) \
        or len(palette)
    ranking = len(palette) > slots
    if ranking and not colour:
        raise SystemExit(f"{len(palette)} filaments for {slots} slots: choosing between them needs "
                         f"a colour pattern (image-* or texture). List {slots} or fewer.")
    if ranking and slots < 2:
        raise SystemExit("--slots must be at least 2 to choose between filaments")
    auto_base = args.base_extruder is None and colour
    if args.base_extruder is None and not colour:
        args.base_extruder = 1
    why = None
    if colour and args.stack == "on":
        if fils is None:
            why = "--palette colours have no spectra"
        elif args.optics == "swatch":
            why = "--optics swatch"
        elif args.depth <= 0:
            why = "--depth 0 colours all the way through"
        else:
            # Decided here, before any stacking geometry is paid for: some set must be able to
            # stack, which needs every filament in it calibrated (the base too, if it is fixed).
            cal = [spectral.is_calibrated(f) for f in fils]
            need = min(slots, len(fils))
            if sum(cal) < need or (not auto_base and not cal[args.base_extruder - 1]):
                why = (f"stacking needs {need} spectrally calibrated filaments"
                       + ("" if auto_base else ", the base among them") + f"; {sum(cal)} are")
    stacking = colour and args.stack == "on" and why is None
    if why:
        print(f"  stacking off: {why}")

    bases = list(range(len(palette))) if auto_base else [args.base_extruder - 1]
    if fils:
        # The slicer keeps the library swatches (`colors`); matching uses what prints.
        palettes = {b: printed_palette(fils, fils[b], args.depth, args.optics)[0] for b in bases}
    else:
        palettes = {b: palette for b in bases}
    tag = "candidate " if ranking else "T"
    for i, (c, name) in enumerate(zip(colors, names), 1):
        base = "  (base)" if not auto_base and i == args.base_extruder else ""
        # What prints depends on the base; until it is chosen there is nothing to show.
        shows = "" if auto_base else _prints_as(c, palettes[bases[0]][i - 1])
        print(f"  {tag}{i} {c} {name}{base}{shows}")

    threemf.require_layer_grid(args.flavor, args.template, args.layer_height)
    t_lh, t_flh = threemf.template_layer_settings(args.template) if args.template else (None, None)
    args.layer_height = args.layer_height or t_lh or 0.2
    args.first_layer_height = args.first_layer_height or t_flh or args.layer_height
    if t_lh:
        print(f"layer grid {t_flh or t_lh} mm first, then {t_lh} mm (from template)")
    if colour and len(palette) < 2:
        raise SystemExit("image and texture patterns need at least two filaments")
    if args.pattern in IMAGE_PATTERNS and not args.image:
        raise SystemExit(f"--pattern {args.pattern} needs --image")
    if args.pattern == "texture" and not glb_input:
        raise SystemExit("--pattern texture needs a GLB/glTF model; a 3MF has no colours to read")
    if _dither_mode(args) != "off" and args.dither_strength > 0 and args.walls == "template" \
            and args.resolution < MIN_FEATURE_MM - 1e-9:
        print(f"  ! dither at --resolution {args.resolution} mm with the template's walls: "
              f"one-voxel features are narrower than a nozzle and mostly do not print. Use "
              f"{MIN_FEATURE_MM} or more, or --walls arachne.")
    classes = None
    if stacking:
        args.stack_depth = paint_stack.stack_depth(args.depth, args.layer_height)
        classes = paint_stack.band_classes(args.stack_depth, args.layer_height, args.resolution,
                                     args.wall_band)
        if args.stack_depth < args.depth - 1e-9:
            print(f"  stacking {args.stack_depth:.2f} mm, whole {args.layer_height} mm layers of "
                  f"--depth {args.depth}; below that is the base")

    print(f"reading {args.model}")
    bed = threemf.template_bed_size(args.template) if args.template else None
    items = threemf.read_model(args.model, scale_to=args.scale_to, bed=bed)
    if angles and any(angles):
        own = np.vstack([i.verts for i in items])
        olo, ohi = own.min(0), own.max(0)
        # sized as main sizes an unposed model: XY from its bbox, z from the bed
        args.unpose = (pose.unposer([i.verts for i in items], angles), olo,
                       (ohi[0] - olo[0], ohi[1] - olo[1], ohi[2]))
        pose.rotate_items(items, angles)
        print(f"  posed: rotated {pose.fmt_rotate(angles)} degrees (X, then Y, then Z), "
              f"set back on the bed")
    allv = np.vstack([i.verts for i in items])
    lo, hi = allv.min(0), allv.max(0)
    bounds = (lo[0], lo[1], hi[0], hi[1])
    args.size = tuple(hi - lo)
    print(f"  {len(items)} item(s), bbox {args.size[0]:.1f} x {args.size[1]:.1f} x "
          f"{args.size[2]:.1f} mm")
    if lo[2] < -1e-6:
        print(f"  ! model starts at z={lo[2]:.2f}; slicers drop it to the bed, so the "
              f"layer grid will be off. Place it on the bed first.")

    edges = layer_edges(hi[2], args.first_layer_height, args.layer_height)
    args.size = (hi[0] - lo[0], hi[1] - lo[1], hi[2])   # z from the bed, not from the model's minimum
    nz = len(edges) - 1
    nx = int(math.ceil((hi[0] - lo[0]) / args.resolution))
    ny = int(math.ceil((hi[1] - lo[1]) / args.resolution))
    print(f"  grid {nx} x {ny} x {nz} voxels ({nx * ny * nz / 1e6:.1f} M)")
    if nx * ny * nz > MAX_VOXELS:
        raise SystemExit(f"{nx * ny * nz / 1e6:.0f} M voxels is too many (limit "
                         f"{MAX_VOXELS // 1_000_000} M); raise --resolution or lower the model size")

    t0 = time.time()
    shells = []
    for i, item in enumerate(items):
        occ = voxelize(item, bounds, args.resolution, edges)
        if not occ.any():
            print(f"  ! item {i} ({item.name}) voxelised to nothing; is the mesh empty or degenerate?")
            continue
        if stacking:
            dist = surface_distance(occ, args.resolution, args.layer_height)
            shell = occ & (dist <= args.depth)
        else:
            shell = shell_mask(occ, args.resolution, args.layer_height, args.depth)
        coords = shell_coords(shell, bounds, args.resolution, edges, args.size, item)
        geo = target = None
        if coords[1] is not None and stacking:
            geo = stack_geometry(dist, coords, edges, classes, args)
            target = geo["target"]                 # the column's, so a column solves alike
        elif coords[1] is not None and colour:
            target = TARGETS[args.pattern](coords[1], args)
        shells.append(Shell(i, item, occ, shell, coords, target, geo))
        print(f"  item {i}: {occ.sum() / 1e3:.0f} k solid voxels, "
              f"{shell.sum() / 1e3:.0f} k coloured ({time.time() - t0:.1f}s)")

    stacks = None
    anything = any(s.target is not None for s in shells)
    if ranking and not anything:
        raise SystemExit("nothing to colour, so nothing to choose filaments for")
    if auto_base and not anything:
        args.base_extruder, auto_base = 1, False        # nothing to colour: any base will do
    if colour and (ranking or auto_base):
        # Only when picking a subset does the base go first: a full list is the user's slots.
        win = choose_filaments(fils, palettes, bases, slots, shells, names, args, classes,
                               base_first=ranking)
        keep, base, stacks = win["keep"], win["base"], win["stacks"]
        palette = palettes[base][keep]
        colors, names = [colors[k] for k in keep], [names[k] for k in keep]
        if fils:
            fils = [fils[k] for k in keep]
        args.base_extruder = keep.index(base) + 1
        print("\nusing" + (" (stacked):" if stacks else " (one filament per voxel):"))
        for i, (c, name) in enumerate(zip(colors, names), 1):
            print(f"  T{i} {c} {name}" + ("  (base)" if i == args.base_extruder else "")
                  + _prints_as(c, palette[i - 1]))
        print()
    else:
        palette = palettes[args.base_extruder - 1]
        if stacking:
            if all(spectral.is_calibrated(f) for f in fils):
                stacks = paint_stack.Stacks(fils, args.base_extruder - 1, classes)
            else:
                print("  stacking off: every filament needs a spectral calibration to stack")
    if fils:
        for n in printed_palette(fils, fils[args.base_extruder - 1], args.depth, args.optics)[1]:
            print(f"  ! {n}")
    if stacks is not None:
        cls = np.concatenate([s.geo["cls"][s.surf] for s in shells if s.geo is not None])
        print(f"  stacked in bands along the surface normal ({args.layer_height} mm a layer on a "
              f"top face, >= {max(args.wall_band, args.resolution)} mm on a wall):")
        for c, (m, u) in enumerate(classes):
            print(f"    {m:3d} x {u:.2f} mm bands: {100 * (cls == c).mean():5.1f}% of the surface")
        if stacks.capped:
            print("  ! a stack gamut hit its cap; the deepest stacks were not all searched")

    change_time = threemf.template_tool_change_time(args.template) if args.template else None
    chosen = budget_tools(shells, palette, stacks, fils, nz, change_time, args) \
        if budgeting else None

    decals, report = {}, []
    total_boxes, seen_labels = 0, np.zeros(len(palette), int)
    used = np.zeros(nz, np.int64)
    preview_lab = preview_shade = None
    for s in shells:
        shade = None
        args.last_de = None
        if chosen is not None and s.index in chosen:
            labels, de, own = chosen[s.index]
            (zs, ys, xs), _ = s.coords
            lab = np.full(s.occ.shape, -1, dtype=np.int16)
            lab[zs, ys, xs] = labels
            args.last_de = de
            if args.preview and stacks is not None:
                shade = np.zeros(lab.shape + (3,), np.uint8)
                shade[zs, ys, xs] = np.round(own).astype(np.uint8)
        elif stacks is not None and s.geo is not None:
            lab, shade_v = label_stacked(s, stacks, args)
            if args.preview:      # a stacked voxel's colour is its stack's, not its filament's
                (zs, ys, xs), _ = s.coords
                shade = np.zeros(lab.shape + (3,), np.uint8)
                shade[zs, ys, xs] = np.round(shade_v).astype(np.uint8)
        else:
            lab = label_grid(s.occ, s.shell, bounds, args.resolution, edges, args.pattern, args,
                             palette, s.item, s.coords, s.target)
        if args.last_de is not None:
            report.append(args.last_de[s.surf])
        used |= paint_budget.layer_tools(lab, s.occ, args.base_extruder - 1)
        # Only a modifier is clipped to its object: an overhanging part would print outside it.
        grown, pad = overhang(lab, s.occ) if args.part_type == "modifier" else (lab, 0)
        shift = pad * args.resolution
        parts = emit_boxes(grown, (bounds[0] - shift, bounds[1] - shift, bounds[2] + shift,
                                   bounds[3] + shift), args.resolution, edges,
                           args.base_extruder - 1)
        decals[s.index] = parts
        total_boxes += sum(len(t) // 12 for _, _, t in parts)
        seen_labels += np.bincount(lab[lab >= 0], minlength=len(palette))
        if args.preview:
            mine = lab >= 0
            preview_lab = lab if preview_lab is None else np.where(mine, lab, preview_lab)
            if shade is not None:
                if preview_shade is None:
                    preview_shade = shade
                else:
                    preview_shade[mine] = shade[mine]

    if report and sum(len(r) for r in report):
        de = float(np.concatenate(report).mean())
        print(f"  mean colour error {de:.1f} dE over the surface (target vs the area-average of the "
              f"printed {'stacks' if stacks is not None else 'mix'}; simulated, not measured)")
    tot = max(1, seen_labels.sum())
    for f, c in enumerate(seen_labels):
        tag = "  (base extruder: no modifier)" if f == args.base_extruder - 1 else ""
        print(f"  T{f + 1} {100 * c / tot:5.1f}% of coloured voxels{tag}")
    print(f"  {total_boxes} boxes ({total_boxes * 12} triangles)")
    changes = paint_budget.count_changes(used)
    by = paint_budget.tools_histogram(used, len(palette))
    print(f"  tool changes: at most {changes}{_hours(changes, change_time)} over {nz} layers "
          f"(layers printing 1..{len(palette)} filaments: {' / '.join(map(str, by))})")
    if total_boxes > 250_000:
        print("  ! that is a lot of geometry and your slicer will be slow to load it.\n"
              "    Raise --resolution, lower --depth, or use a simpler pattern.")
    if args.template:
        solid = threemf.template_top_solid_depth(args.template, args.layer_height)
        if solid and args.depth > solid + 1e-9:
            print(f"  ! --depth {args.depth} mm reaches below the template's solid shell "
                  f"({solid:.2f} mm); colour under that lands on sparse infill.")
    if args.preview and preview_lab is not None:
        save_preview(args.preview, preview_lab, palette, edges, args.resolution, preview_shade)
        print(f"  preview -> {args.preview}")

    if args.dry_run:
        print("dry run: nothing written")
        return
    threemf.get_writer(args.flavor)(
        args.output, items, decals, args.base_extruder, args.part_type,
        template=args.template, colors=colors,
        layer_height=args.layer_height, first_layer_height=args.first_layer_height,
        object_settings=wall_settings(args.flavor) if args.walls == "arachne" else None)
    print(f"wrote {args.output} ({os.path.getsize(args.output) / 1e6:.1f} MB, {args.flavor} flavor)")
    print(f"\nSlice with layer height EXACTLY {args.layer_height} mm and first layer "
          f"{args.first_layer_height} mm, or the modifiers land between layers.")


if __name__ == "__main__":
    main()
