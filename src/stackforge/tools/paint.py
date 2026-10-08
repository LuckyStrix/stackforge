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
import math
import os
import time

import numpy as np
from PIL import Image
from scipy.ndimage import distance_transform_edt

from stackforge.core import threemf
from stackforge.core.filamentdb import DEFAULT_DB
from stackforge.core import colormath

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
    padded = np.pad(occ, 1)          # the bounding box edge counts as outside
    dist = distance_transform_edt(padded, sampling=(lh, res, res))[1:-1, 1:-1, 1:-1]
    return occ & (dist <= depth)


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


def _nearest_filament(rgb, palette):
    """(N,3) sRGB -> nearest palette index in Lab, chunked to bound memory."""
    pal_lab = colormath.srgb_to_lab(palette)
    out = np.empty(len(rgb), dtype=np.int32)
    for s in range(0, len(rgb), 200_000):
        lab = colormath.srgb_to_lab(rgb[s:s + 200_000].astype(np.float64))
        out[s:s + 200_000] = ((lab[:, None, :] - pal_lab[None]) ** 2).sum(-1).argmin(-1)
    return out


def _pick(rgb_srgb, cd, a, palette):
    """Palette index for sampled sRGB colours: dithered if --dither is on, else nearest in Lab."""
    mode = _dither_mode(a)
    if mode == "off":
        return _nearest_filament(rgb_srgb, palette)
    return _pick_linear(colormath.srgb_to_linear(np.asarray(rgb_srgb, dtype=np.float64)), cd, a, palette)


def _pick_linear(target_lin, cd, a, palette):
    pal_lin = colormath.srgb_to_linear(palette)
    strength = 0.0 if _dither_mode(a) == "off" else getattr(a, "dither_strength", 1.0)
    idx, blend = colormath.quantize_dither(target_lin, pal_lin, cd.cells, strength)
    de = np.linalg.norm(colormath.linear_to_lab(blend) - colormath.linear_to_lab(target_lin), axis=-1)
    a.colour_report = getattr(a, "colour_report", []) + [(de.sum(), len(de))]
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


def p_image_spherical(cd, a, n, palette):
    """Equirectangular map (Earth/Mars/Moon style): u from azimuth, v from elevation."""
    u = ((cd.theta + math.pi) / (2 * math.pi) + a.lon_offset / 360.0) % 1.0
    v = 0.5 - cd.phi / math.pi
    return _pick(_sample(_load(a), u, v), cd, a, palette)


def p_image_cylindrical(cd, a, n, palette):
    """Wrap around the vertical axis; v runs down the model's height."""
    u = ((cd.theta + math.pi) / (2 * math.pi) + a.lon_offset / 360.0) % 1.0
    v = 1.0 - cd.z / max(cd.size[2], 1e-9)
    return _pick(_sample(_load(a), u, v), cd, a, palette)


def p_image_planar(cd, a, n, palette):
    """Project along --axis, like top_paint but for any direction."""
    ax = "xyz".index(a.axis)
    o = [i for i in range(3) if i != ax]
    co = [cd.x, cd.y, cd.z]
    u = co[o[0]] / max(cd.size[o[0]], 1e-9)
    v = 1.0 - co[o[1]] / max(cd.size[o[1]], 1e-9)
    return _pick(_sample(_load(a), np.clip(u, 0, 1), np.clip(v, 0, 1)), cd, a, palette)


def p_texture(cd, a, n, palette):
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
    target = (cols[nn] * w[..., None]).sum(1) / w.sum(1)[:, None]
    return _pick_linear(target, cd, a, palette)


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
    "image-spherical": p_image_spherical,
    "image-cylindrical": p_image_cylindrical,
    "image-planar": p_image_planar,
    "texture": p_texture,
}
IMAGE_PATTERNS = {"image-spherical", "image-cylindrical", "image-planar"}
COLOUR_PATTERNS = IMAGE_PATTERNS | {"texture"}      # patterns that need a palette, not just a count


def evaluate(pattern, cd, args, palette):
    n = len(palette)
    fn = PATTERNS[pattern]
    idx = fn(cd, args, n, palette) if pattern in COLOUR_PATTERNS else fn(cd, args, n)
    return np.asarray(idx).astype(int) % n


# --------------------------------------------------------------------------
# labels -> boxes
# --------------------------------------------------------------------------


def label_grid(occ, shell, bounds, res, edges, pattern, args, palette, item=None):
    """(nz,ny,nx) int labels; -1 where there is nothing to colour."""
    x0, y0, x1, y1 = bounds
    nz, ny, nx = occ.shape
    lab = np.full(occ.shape, -1, dtype=np.int16)
    zs, ys, xs = np.nonzero(shell)
    if len(zs) == 0:
        return lab
    mids = (edges[:-1] + edges[1:]) / 2
    cd = Coords(xs * res + res / 2, ys * res + res / 2, mids[zs], args.size, item=item,
                res=res, origin=(x0, y0), cells=np.stack([xs, ys, zs], axis=1))
    lab[zs, ys, xs] = evaluate(pattern, cd, args, palette)
    return lab


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


def save_preview(path, lab, palette):
    """Top view and front view of the colour of the first coloured voxel seen."""
    pal = np.vstack([palette, [[30, 30, 34]]]).astype(np.uint8)
    empty = len(palette)

    def first_hit(vol, axis, reverse):
        v = np.flip(vol, axis) if reverse else vol
        has = v >= 0
        idx = np.argmax(has, axis=axis)
        got = np.take_along_axis(v, np.expand_dims(idx, axis), axis).squeeze(axis)
        return np.where(has.any(axis=axis), got, empty)

    top = first_hit(lab, 0, True)[::-1]            # looking down; y up on the page
    front = first_hit(lab, 1, False)[::-1]          # looking along +y; z up
    a, b = pal[top], pal[front]
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
    ap.add_argument("--base-extruder", type=int, default=1,
                    help="extruder the object already prints in; voxels given this colour "
                         "emit no modifier")
    ap.add_argument("--pattern", choices=["texture"] + sorted(set(PATTERNS) - {"texture"}),
                    help="texture = the model's own colours (GLB); image-* wrap an --image; the "
                         "rest are decorative. Default: texture for a GLB, required for a 3MF")
    ap.add_argument("--dither", choices=["off", "ordered"], default=None,
                    help="mix the two nearest filaments across neighbouring voxels so the area "
                         "average matches the colour. Default: ordered for texture, else off. "
                         "Needs --resolution >= 0.6 to print")
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
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.filaments:
        from stackforge.core.filamentdb import DB
        fils = DB(args.db).resolve(args.filaments)
        palette = np.array([f.rgb() for f in fils], float)
        for i, f in enumerate(fils, 1):
            print(f"  T{i} {f.color} {f.label()}")
        colors = [f.color for f in fils]
    else:
        palette = colormath.parse_palette(args.palette)
        colors = [colormath.to_hex(c) for c in palette]
    if not 1 <= args.base_extruder <= len(palette):
        raise SystemExit(f"--base-extruder must be 1 to {len(palette)}")

    for flag, val in (("--resolution", args.resolution), ("--layer-height", args.layer_height),
                      ("--first-layer-height", args.first_layer_height),
                      ("--scale-to", args.scale_to)):
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

    threemf.require_layer_grid(args.flavor, args.template, args.layer_height)
    t_lh, t_flh = threemf.template_layer_settings(args.template) if args.template else (None, None)
    args.layer_height = args.layer_height or t_lh or 0.2
    args.first_layer_height = args.first_layer_height or t_flh or args.layer_height
    if t_lh:
        print(f"layer grid {t_flh or t_lh} mm first, then {t_lh} mm (from template)")
    if args.pattern in COLOUR_PATTERNS and len(palette) < 2:
        raise SystemExit("image and texture patterns need at least two filaments")
    if args.pattern in IMAGE_PATTERNS and not args.image:
        raise SystemExit(f"--pattern {args.pattern} needs --image")
    if args.pattern == "texture" and not glb_input:
        raise SystemExit("--pattern texture needs a GLB/glTF model; a 3MF has no colours to read")
    if _dither_mode(args) != "off" and args.dither_strength > 0 \
            and args.resolution < MIN_FEATURE_MM - 1e-9:
        print(f"  ! dither at --resolution {args.resolution} mm: one-voxel features are narrower "
              f"than a nozzle and mostly do not print. Use {MIN_FEATURE_MM} or more.")

    print(f"reading {args.model}")
    bed = threemf.template_bed_size(args.template) if args.template else None
    items = threemf.read_model(args.model, scale_to=args.scale_to, bed=bed)
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
    decals = {}
    total_boxes, seen_labels = 0, np.zeros(len(palette), int)
    preview_lab = None
    for i, item in enumerate(items):
        occ = voxelize(item, bounds, args.resolution, edges)
        if not occ.any():
            print(f"  ! item {i} ({item.name}) voxelised to nothing; is the mesh empty or degenerate?")
            continue
        shell = shell_mask(occ, args.resolution, args.layer_height, args.depth)
        lab = label_grid(occ, shell, bounds, args.resolution, edges, args.pattern, args, palette, item)
        parts = emit_boxes(lab, bounds, args.resolution, edges, args.base_extruder - 1)
        decals[i] = parts
        total_boxes += sum(len(t) // 12 for _, _, t in parts)
        seen_labels += np.bincount(lab[lab >= 0], minlength=len(palette))
        preview_lab = lab if preview_lab is None else np.maximum(preview_lab, lab)
        print(f"  item {i}: {occ.sum() / 1e3:.0f} k solid voxels, "
              f"{shell.sum() / 1e3:.0f} k coloured ({time.time() - t0:.1f}s)")

    rep = getattr(args, "colour_report", [])
    if rep:
        de = sum(r[0] for r in rep) / max(1, sum(r[1] for r in rep))
        print(f"  mean colour error {de:.1f} dE (target vs the area-average of the printed mix; "
              f"simulated, not measured)")
    tot = max(1, seen_labels.sum())
    for f, c in enumerate(seen_labels):
        tag = "  (base extruder: no modifier)" if f == args.base_extruder - 1 else ""
        print(f"  T{f + 1} {100 * c / tot:5.1f}% of coloured voxels{tag}")
    print(f"  {total_boxes} boxes ({total_boxes * 12} triangles)")
    if total_boxes > 250_000:
        print("  ! that is a lot of geometry and your slicer will be slow to load it.\n"
              "    Raise --resolution, lower --depth, or use a simpler pattern.")
    if args.template:
        solid = threemf.template_top_solid_depth(args.template, args.layer_height)
        if solid and args.depth > solid + 1e-9:
            print(f"  ! --depth {args.depth} mm reaches below the template's solid shell "
                  f"({solid:.2f} mm); colour under that lands on sparse infill.")
    if args.preview and preview_lab is not None:
        save_preview(args.preview, preview_lab, palette)
        print(f"  preview -> {args.preview}")

    threemf.get_writer(args.flavor)(
        args.output, items, decals, args.base_extruder, args.part_type,
        template=args.template, colors=colors,
        layer_height=args.layer_height, first_layer_height=args.first_layer_height)
    print(f"wrote {args.output} ({os.path.getsize(args.output) / 1e6:.1f} MB, {args.flavor} flavor)")
    print(f"\nSlice with layer height EXACTLY {args.layer_height} mm and first layer "
          f"{args.first_layer_height} mm, or the modifiers land between layers.")


if __name__ == "__main__":
    main()
