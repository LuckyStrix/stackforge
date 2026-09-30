#!/usr/bin/env python3
"""Compare stackforge's dither modes by how the plaque looks, not per-pixel error.

Per-pixel dE is the wrong yardstick for halftoning: a dither deliberately puts
"wrong" pixels next to each other and relies on the eye averaging them, so it
always loses on per-pixel error. This blurs both the target and each result in
linear light (a Gaussian standing in for the eye's low-pass at viewing
distance) and scores dE on the blurred images -- an S-CIELAB-lite. `--blur` is
that Gaussian's sigma in pixels: at 0.4 mm/px, 1.5 px is ~0.6 mm, roughly
arm's length.

    halftone_compare.py photo.jpg --filaments white,black,blue,red --sheet docs/halftone.png
"""

from __future__ import annotations

import argparse

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import gaussian_filter

import stackforge as sf
import tdcolor
from filamentdb import DB

MODES = ["none", "ordered", "blue", "floyd"]


def blurred_de(a, b, sigma):
    """Mean/p95 dE between two sRGB uint8 images after a linear-light blur."""
    def prep(x):
        lin = tdcolor.srgb_to_linear(x.astype(np.float64))
        lin = np.stack([gaussian_filter(lin[..., c], sigma) for c in range(3)], -1)
        return tdcolor.linear_to_lab(lin)
    d = np.linalg.norm(prep(a) - prep(b), axis=-1)
    return float(d.mean()), float(np.percentile(d, 95))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("image")
    ap.add_argument("--db", default="filaments.json")
    ap.add_argument("--filaments", required=True)
    ap.add_argument("--base")
    ap.add_argument("--width", type=float, default=60.0, help="plaque width in mm")
    ap.add_argument("--resolution", type=float, default=0.4)
    ap.add_argument("--layer-height", type=float, default=0.08)
    ap.add_argument("--max-layers", type=int, default=12)
    ap.add_argument("--blur", type=float, default=1.5, help="eye low-pass sigma, px")
    ap.add_argument("--sheet", help="contact sheet PNG")
    args = ap.parse_args(argv)

    db = DB(args.db)
    fils = db.resolve(args.filaments)
    base = db.get(args.base) if args.base else fils[0]
    w = max(1, round(args.width / args.resolution))
    im = Image.open(args.image)
    h = max(1, round(w * im.height / im.width))
    img = tdcolor.fit_image(args.image, w, h, "cover")
    gamut = sf.Gamut(fils, base, args.layer_height, args.max_layers, verbose=False)

    rows, tiles = [], [("target", img)]
    for mode in MODES:
        got = gamut.srgb()[sf.solve_image(gamut, img, mode)].astype(np.uint8)
        px = np.linalg.norm(tdcolor.srgb_to_lab(got.astype(float))
                            - tdcolor.srgb_to_lab(img.astype(float)), axis=-1)
        bm, bp = blurred_de(got, img, args.blur)
        rows.append((mode, float(px.mean()), bm, bp))
        tiles.append((mode, got))

    print(f"{'mode':<8} {'pixel dE':>9} {'blurred dE':>11} {'blurred p95':>12}")
    for mode, pm, bm, bp in rows:
        print(f"{mode:<8} {pm:9.2f} {bm:11.2f} {bp:12.2f}")
    print(f"(blur sigma {args.blur} px = {args.blur * args.resolution:.2f} mm)")

    if args.sheet:
        pad, lab = 6, 16
        sheet = Image.new("RGB", ((w + pad) * len(tiles) + pad, h + lab + 2 * pad), "#202020")
        d = ImageDraw.Draw(sheet)
        for i, (name, arr) in enumerate(tiles):
            x = pad + i * (w + pad)
            sheet.paste(Image.fromarray(arr), (x, pad + lab))
            d.text((x, pad), name, fill="white")
        sheet = sheet.resize((sheet.width * 2, sheet.height * 2), Image.NEAREST)
        sheet.save(args.sheet)
        print(f"contact sheet -> {args.sheet}")


if __name__ == "__main__":
    main()
