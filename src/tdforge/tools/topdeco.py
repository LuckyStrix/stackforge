#!/usr/bin/env python3
"""
topdeco -- project an image onto whatever is visible from directly above a 3MF.

Renders a top-down z-buffer of the model, samples an image across the model's
XY footprint, quantizes it to your loaded filaments, and emits one modifier
volume per filament that hugs the visible top surface. The slicer intersects
those modifiers with the object and swaps extruders inside them, so only the
top N layers change color and the geometry is untouched.

Works on arbitrary geometry, not just flat plates: the modifier boxes follow
the z-buffer, so a chainmail sheet, a domed badge and a terrain tile all get
the image laid over their real top surface.

    topdeco fabric.3mf logo.png -o out.3mf \
        --palette "#101010,#0B3D91,#FC3D21,#FFFFFF" --depth 0.6

Colors can come from the filament database instead of --palette:

    topdeco fabric.3mf logo.png -o out.3mf \
        --filaments white,black,blue,red

See --help for the rest.
"""

from __future__ import annotations

import argparse
import math
import os

import numpy as np
from PIL import Image

from tdforge.core import td3mf
from tdforge.core import tdcolor


# --------------------------------------------------------------------------
# top-down rasterization
# --------------------------------------------------------------------------


def rasterize_top(items, bounds, res: float):
    """Z-buffer the scene from +Z. Returns (zbuf, itembuf); NaN/-1 where empty."""
    x0, y0, x1, y1 = bounds
    w = max(1, int(math.ceil((x1 - x0) / res)))
    h = max(1, int(math.ceil((y1 - y0) / res)))
    if w * h > 60_000_000:
        raise SystemExit(
            f"--resolution {res} over a {x1-x0:.0f}x{y1-y0:.0f}mm area needs "
            f"{w}x{h} samples. Coarsen it or use --region."
        )

    zbuf = np.full((h, w), -np.inf)
    ibuf = np.full((h, w), -1, dtype=np.int32)

    # Pixel centres. Row 0 is the +Y edge so image rows map straight across.
    px = x0 + (np.arange(w) + 0.5) * res
    py = y1 - (np.arange(h) + 0.5) * res

    for idx, item in enumerate(items):
        v, t = item.verts, item.tris
        a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
        nz = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (
            c[:, 0] - a[:, 0]
        )
        for ti in np.nonzero(nz > 1e-12)[0]:  # upward-facing only
            _raster_tri(a[ti], b[ti], c[ti], nz[ti], px, py, x0, y1, res, w, h,
                        zbuf, ibuf, idx)

    zbuf[np.isinf(zbuf)] = np.nan
    return zbuf, ibuf


def _raster_tri(p0, p1, p2, area2, px, py, x0, y1, res, w, h, zbuf, ibuf, idx):
    cx0 = max(0, int(math.floor((min(p0[0], p1[0], p2[0]) - x0) / res)))
    cx1 = min(w, int(math.ceil((max(p0[0], p1[0], p2[0]) - x0) / res)) + 1)
    ry0 = max(0, int(math.floor((y1 - max(p0[1], p1[1], p2[1])) / res)))
    ry1 = min(h, int(math.ceil((y1 - min(p0[1], p1[1], p2[1])) / res)) + 1)
    if cx0 >= cx1 or ry0 >= ry1:
        return

    gx = px[cx0:cx1][None, :]
    gy = py[ry0:ry1][:, None]

    w0 = ((p1[0] - gx) * (p2[1] - gy) - (p1[1] - gy) * (p2[0] - gx)) / area2
    w1 = ((p2[0] - gx) * (p0[1] - gy) - (p2[1] - gy) * (p0[0] - gx)) / area2
    w2 = 1.0 - w0 - w1
    inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
    if not inside.any():
        return

    z = w0 * p0[2] + w1 * p1[2] + w2 * p2[2]
    sub_z = zbuf[ry0:ry1, cx0:cx1]
    sub_i = ibuf[ry0:ry1, cx0:cx1]
    better = inside & (z > sub_z)
    sub_z[better] = z[better]
    sub_i[better] = idx


# --------------------------------------------------------------------------


def build_boxes(zbuf, ibuf, labels, bounds, res, layer_h, depth, above, item_count):
    """Group samples by (item, extruder, layer band) and emit boxes per item."""
    x0, y0, x1, y1 = bounds

    band = np.full(labels.shape, -(2**31), dtype=np.int64)
    finite = ~np.isnan(zbuf)
    band[finite] = np.floor(zbuf[finite] / layer_h + 0.5).astype(np.int64)

    # One volume per (item, extruder). Layer bands only decide box heights, so
    # folding them into a single volume keeps the slicer's volume list short.
    per_item = {i: [] for i in range(item_count)}
    for item_i in np.unique(ibuf[ibuf >= 0]):
        for ext in np.unique(labels[labels >= 0]):
            sel = (ibuf == item_i) & (labels == ext)
            if not sel.any():
                continue
            bb = td3mf.BoxBuilder()
            for bnd in np.unique(band[sel]):
                top = bnd * layer_h
                for ry, cx, rh, rw, _ in td3mf.greedy_rects(labels, sel & (band == bnd)):
                    bb.add(
                        x0 + cx * res, y1 - (ry + rh) * res, top - depth,
                        x0 + (cx + rw) * res, y1 - ry * res, top + above,
                    )
            m = bb.mesh()
            if m is not None:
                per_item[int(item_i)].append((int(ext), m[0], m[1]))
    return per_item


def save_preview(path, labels, palette, mask):
    h, w = labels.shape
    out = np.full((h, w, 3), 40, dtype=np.uint8)
    ok = labels >= 0
    out[ok] = palette[labels[ok]].astype(np.uint8)
    out[~mask] = (25, 25, 30)
    Image.fromarray(out).save(path)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Project an image onto the top-visible surfaces of a 3MF.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("model", help="input .3mf")
    ap.add_argument("image", help="image to project")
    ap.add_argument("-o", "--output", required=True)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--palette", help="filament colours in extruder order, comma-separated hex")
    src.add_argument("--filaments", help="comma-separated ids from the filament database")
    ap.add_argument("--db", default="filaments.json")
    ap.add_argument("--base-extruder", type=int, default=1,
                    help="extruder the object already prints in; not emitted as a modifier")
    ap.add_argument("--resolution", type=float, default=0.4,
                    help="mm per sample; also the granularity of emitted boxes")
    ap.add_argument("--layer-height", type=float, default=None,
                    help="z-band size for box placement (default: from --template, else 0.2)")
    ap.add_argument("--depth", type=float, default=0.6,
                    help="mm below the top surface the colour reaches")
    ap.add_argument("--above", type=float, default=1.0,
                    help="mm the modifier sticks up into air (tolerance, harmless)")
    ap.add_argument("--dither", choices=["none", "floyd", "ordered", "blue"], default="none")
    ap.add_argument("--fit", choices=["contain", "cover", "stretch"], default="contain")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270])
    ap.add_argument("--flip", action="store_true", help="mirror the image in X")
    ap.add_argument("--region", help="x0,y0,x1,y1 in mm; default is the model footprint")
    ap.add_argument("--flavor", choices=["orca", "prusa"], default="orca")
    ap.add_argument("--part-type", choices=["modifier", "part"], default="modifier",
                    help="modifier volumes (geometry untouched) or real overlapping parts")
    ap.add_argument("--template",
                    help="a project .3mf exported from your slicer; its printer, "
                         "filament and print settings are carried over verbatim so "
                         "the output opens as a project rather than bare geometry")
    ap.add_argument("--preview", help="write a PNG of what will be painted")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.filaments:
        from tdforge.core.filamentdb import DB
        fils = DB(args.db).resolve(args.filaments)
        palette = np.array([f.rgb() for f in fils])
        for i, f in enumerate(fils, 1):
            print(f"  T{i} {f.color} {f.label()}")
    else:
        palette = tdcolor.parse_palette(args.palette or "#FFFFFF,#101010,#0B3D91,#FC3D21")

    if not 1 <= args.base_extruder <= len(palette):
        raise SystemExit(f"--base-extruder must be within 1..{len(palette)}")

    if args.layer_height is None:
        t_lh, _ = td3mf.template_layer_settings(args.template) if args.template else (None, None)
        args.layer_height = t_lh or 0.2
        if t_lh:
            print(f"layer height {t_lh} mm (from template)")

    # Colour is only continuous where the slicer prints solid. Below the top
    # shell it switches to sparse infill, and paint applied down there lands on
    # a lattice rather than a surface.
    if args.template:
        solid = td3mf.template_top_solid_depth(args.template, args.layer_height)
        if solid and args.depth > solid + 1e-9:
            print(f"  ! --depth {args.depth} mm reaches below the template's solid "
                  f"top shell ({solid:.2f} mm). Colour under that is sparse infill, "
                  f"not a surface. Reduce --depth, or raise top_shell_layers.")

    print(f"reading {args.model}")
    items = td3mf.read_3mf(args.model)
    allv = np.vstack([i.verts for i in items])
    print(f"  {len(items)} build item(s), {sum(len(i.tris) for i in items)} triangles")

    if args.region:
        bounds = tuple(float(x) for x in args.region.split(","))
        if len(bounds) != 4:
            raise SystemExit("--region wants x0,y0,x1,y1")
    else:
        bounds = (allv[:, 0].min(), allv[:, 1].min(), allv[:, 0].max(), allv[:, 1].max())
    x0, y0, x1, y1 = bounds
    print(f"  footprint {x1-x0:.1f} x {y1-y0:.1f} mm, z up to {allv[:,2].max():.2f}")

    zbuf, ibuf = rasterize_top(items, bounds, args.resolution)
    mask = ~np.isnan(zbuf)
    h, w = zbuf.shape
    print(f"  z-buffer {w}x{h}, {mask.mean()*100:.1f}% covered")
    if not mask.any():
        raise SystemExit("nothing visible from above -- check --region or model orientation")

    # Transparent pixels and contain-padding take the base's colour, so they
    # quantize to the base extruder and stay unpainted.
    pad = tuple(int(v) for v in palette[args.base_extruder - 1])
    img = tdcolor.fit_image(args.image, w, h, args.fit, args.rotate, args.flip, pad=pad)
    labels = tdcolor.quantize(img, palette, mask, args.dither)

    if args.preview:
        save_preview(args.preview, labels, palette, mask)
        print(f"  preview -> {args.preview}")

    ext = labels + 1
    ext[labels < 0] = -1
    ext[ext == args.base_extruder] = -1
    painted = ext >= 0
    print(f"  {painted.sum()} samples to repaint ({painted.mean()*100:.1f}% of grid)")

    decals = build_boxes(zbuf, ibuf, ext, bounds, args.resolution,
                         args.layer_height, args.depth, args.above, len(items))
    nboxes = sum(len(t) // 12 for v in decals.values() for _, _, t in v)
    print(f"  {nboxes} merged boxes across {sum(len(v) for v in decals.values())} modifier volumes")

    td3mf.get_writer(args.flavor)(
        args.output, items, decals, args.base_extruder, args.part_type,
        template=args.template,
        colors=[tdcolor.to_hex(c) for c in palette],
        layer_height=args.layer_height,
    )
    print(f"wrote {args.output} ({os.path.getsize(args.output)/1e6:.1f} MB, {args.flavor} flavor)")


if __name__ == "__main__":
    main()
