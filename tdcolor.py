#!/usr/bin/env python3
"""Color utilities shared by topdeco and stackforge.

Everything perceptual happens in CIELAB; everything physical (layer
compositing) happens in linear light. sRGB is only ever an input/output
encoding -- never a space to average or composite in.
"""

from __future__ import annotations

import re

import numpy as np
from PIL import Image

D65 = np.array([0.95047, 1.0, 1.08883])
_M_RGB2XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ]
)
_M_XYZ2RGB = np.linalg.inv(_M_RGB2XYZ)


def srgb_to_linear(c):
    c = np.asarray(c, dtype=np.float64) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(lin):
    lin = np.clip(np.asarray(lin, dtype=np.float64), 0.0, 1.0)
    c = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
    return c * 255.0


def linear_to_lab(lin):
    xyz = np.asarray(lin, dtype=np.float64) @ _M_RGB2XYZ.T
    t = xyz / D65
    eps, k = 216 / 24389, 24389 / 27
    f = np.where(t > eps, np.cbrt(np.clip(t, 0, None)), (k * t + 16) / 116)
    return np.stack(
        [116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])],
        axis=-1,
    )


def srgb_to_lab(rgb):
    """rgb in 0..255, any leading shape."""
    return linear_to_lab(srgb_to_linear(rgb))


def parse_hex(tok: str):
    tok = tok.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", tok):
        tok = "".join(c * 2 for c in tok)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", tok):
        raise ValueError(f"bad color {tok!r}; want RGB or RRGGBB hex")
    return [int(tok[i : i + 2], 16) for i in (0, 2, 4)]


def to_hex(rgb) -> str:
    r, g, b = (int(round(float(v))) for v in np.clip(rgb, 0, 255))
    return f"#{r:02X}{g:02X}{b:02X}"


def parse_palette(spec: str) -> np.ndarray:
    cols = [parse_hex(t) for t in spec.split(",")]
    if len(cols) < 2:
        raise SystemExit("palette needs at least two filaments")
    return np.array(cols, dtype=np.float64)


def bayer(n: int) -> np.ndarray:
    m = np.array([[0.0]])
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return m / m.size


def palette_spread(palette: np.ndarray) -> float:
    d = np.linalg.norm(palette[:, None, :] - palette[None, :, :], axis=-1)
    np.fill_diagonal(d, np.inf)
    return float(d.min(axis=1).mean())


def nearest_lab(img_rgb: np.ndarray, pal_lab: np.ndarray) -> np.ndarray:
    lab = srgb_to_lab(img_rgb.astype(np.float64))
    d = ((lab[:, :, None, :] - pal_lab[None, None, :, :]) ** 2).sum(-1)
    return d.argmin(-1).astype(np.int32)


def floyd_steinberg(img: np.ndarray, palette: np.ndarray, pal_lab: np.ndarray,
                    query=None, err_clamp: float = 48.0) -> np.ndarray:
    """Error-diffuse img (h,w,3 sRGB) onto `palette`.

    `query` optionally replaces the nearest-color search (used by stackforge to
    search a KD-tree of achievable stack colors instead of a flat palette).

    `err_clamp` bounds the per-channel error pushed into neighbours. Without it,
    a region whose target colour sits outside the reachable gamut diffuses the
    same unfixable error over and over; it compounds across the region and
    drags neighbouring pixels somewhere far worse than the honest nearest
    match. Clamping localises that loss instead of letting it spread.
    """
    h, w = img.shape[:2]
    buf = img.astype(np.float64).copy()
    out = np.zeros((h, w), dtype=np.int32)
    for y in range(h):
        for x in range(w):
            old = np.clip(buf[y, x], 0, 255)
            if query is None:
                lab = srgb_to_lab(old)
                i = int(((lab - pal_lab) ** 2).sum(-1).argmin())
            else:
                i = int(query(old))
            out[y, x] = i
            err = np.clip(old - palette[i], -err_clamp, err_clamp)
            if x + 1 < w:
                buf[y, x + 1] += err * 7 / 16
            if y + 1 < h:
                if x > 0:
                    buf[y + 1, x - 1] += err * 3 / 16
                buf[y + 1, x] += err * 5 / 16
                if x + 1 < w:
                    buf[y + 1, x + 1] += err * 1 / 16
    return out


def quantize(img, palette, mask, mode):
    """img (h,w,3) uint8 -> (h,w) palette index; masked-out pixels get -1."""
    pal_lab = linear_to_lab(srgb_to_linear(palette))
    h, w = img.shape[:2]

    if mode == "none":
        out = nearest_lab(img, pal_lab)
    elif mode == "ordered":
        spread = palette_spread(palette) * 0.5
        tile = np.tile(bayer(8), (h // 8 + 1, w // 8 + 1))[:h, :w] - 0.5
        out = nearest_lab(np.clip(img + tile[:, :, None] * spread, 0, 255), pal_lab)
    elif mode == "floyd":
        out = floyd_steinberg(img, palette, pal_lab)
    else:
        raise SystemExit(f"unknown dither mode {mode!r}")

    out[~mask] = -1
    return out


def fit_image(path, w, h, mode="contain", rotate=0, flip=False, pad=(255, 255, 255)):
    im = Image.open(path).convert("RGB")
    if rotate:
        im = im.rotate(-rotate, expand=True)
    if flip:
        im = im.transpose(Image.FLIP_LEFT_RIGHT)
    if mode == "stretch":
        return np.asarray(im.resize((w, h), Image.LANCZOS))
    scale = (
        min(w / im.width, h / im.height) if mode == "contain" else max(w / im.width, h / im.height)
    )
    nw, nh = max(1, round(im.width * scale)), max(1, round(im.height * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (w, h), pad)
    canvas.paste(im, ((w - nw) // 2, (h - nh) // 2))
    return np.asarray(canvas)
