"""Model pose: rotate a model before it is painted, and what a pose costs.

The pose has to be set before paint runs: paint's colour boxes sit on the slicer's layer
grid, so rotating the painted 3MF in the slicer would put them between layers. paint's
--rotate turns the mesh about its bounding-box centre, then puts it back on the bed where it
was (same XY centre, lowest point at z = 0). Pure numpy; the GUI's Pose box draws `views`
and shows `stats`.
"""
from __future__ import annotations

import math

import numpy as np

OVERHANG_DEG = 45.0     # faces steeper than this below horizontal count as needing support
BED_EPS = 0.2           # mm: faces this close to the bed rest on it


def parse_rotate(text) -> tuple:
    """'X,Y,Z' degrees (spaces or commas) -> (x, y, z) floats."""
    parts = [p for p in str(text).replace(",", " ").split() if p]
    try:
        vals = tuple(float(p) for p in parts)
    except ValueError:
        vals = ()
    if len(vals) != 3 or not all(math.isfinite(v) for v in vals):
        raise ValueError(f"--rotate wants three angles in degrees, X,Y,Z (got {text!r})")
    return vals


def fmt_rotate(angles) -> str:
    return ",".join(f"{a:g}" for a in angles)


def matrix(angles) -> np.ndarray:
    """3x3 rotation: about X, then Y, then Z (fixed axes), degrees."""
    x, y, z = (math.radians(a) for a in angles)
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return rz @ ry @ rx


def rotate_points(verts_list, angles):
    """Rotated copies of each (n,3) array, turned together about their joint bounding-box
    centre and put back with the same XY centre and the lowest point at z = 0."""
    allv = np.vstack(verts_list)
    lo, hi = allv.min(0), allv.max(0)
    c = (lo + hi) / 2
    r = matrix(angles)
    out = [(v - c) @ r.T + c for v in verts_list]
    rv = np.vstack(out)
    rlo, rhi = rv.min(0), rv.max(0)
    shift = np.array([c[0] - (rlo[0] + rhi[0]) / 2, c[1] - (rlo[1] + rhi[1]) / 2, -rlo[2]])
    return [v + shift for v in out]


def rotate_items(items, angles):
    """paint's --rotate on threemf Items (in place; triangles and colours are unchanged)."""
    if not any(angles):
        return items
    for item, v in zip(items, rotate_points([i.verts for i in items], angles)):
        item.verts = v
    return items


def _faces(verts, tris):
    a, b, c = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    n = np.cross(b - a, c - a)
    area2 = np.linalg.norm(n, axis=1)
    return a, b, c, n, area2


def stats(verts_list, tris_list, layer_height, first_layer=None) -> dict:
    """Size, layers on the slicer's grid and overhang area of a posed model.

    Overhang is the area of faces pointing down more than OVERHANG_DEG below horizontal,
    leaving out those resting on the bed: a rough measure of the support it needs.
    """
    allv = np.vstack(verts_list)
    size = allv.max(0) - allv.min(0)
    first = first_layer or layer_height
    layers = 1 + max(0, math.ceil((size[2] - first) / layer_height - 1e-9))
    z0 = allv[:, 2].min()
    over = 0.0
    lim = -math.sin(math.radians(OVERHANG_DEG))
    for v, t in zip(verts_list, tris_list):
        if len(t) == 0:
            continue
        a, b, c, n, area2 = _faces(v, t)
        nz = n[:, 2] / np.maximum(area2, 1e-12)
        high = np.maximum(np.maximum(a[:, 2], b[:, 2]), c[:, 2])
        on_bed = high - z0 < BED_EPS
        over += float(area2[(nz < lim) & ~on_bed].sum() / 2)
    return {"size": tuple(float(s) for s in size), "layers": int(layers),
            "overhang_mm2": over}


def sample_surface(verts_list, tris_list, n=150_000, seed=0):
    """(points, normals) spread over the surface by area: enough to draw a solid view of
    a 12-triangle cube or a million-triangle scan alike."""
    v = np.vstack(verts_list)
    off = np.cumsum([0] + [len(x) for x in verts_list[:-1]])
    t = np.vstack([tt + o for tt, o in zip(tris_list, off) if len(tt)])
    a, b, c, nrm, area2 = _faces(v, t)
    keep = area2 > 0
    a, b, c, nrm, area2 = a[keep], b[keep], c[keep], nrm[keep], area2[keep]
    rng = np.random.default_rng(seed)
    pick = rng.choice(len(area2), size=n, p=area2 / area2.sum())
    u, w = rng.random(n), rng.random(n)
    flip = u + w > 1
    u[flip], w[flip] = 1 - u[flip], 1 - w[flip]
    pts = a[pick] + u[:, None] * (b[pick] - a[pick]) + w[:, None] * (c[pick] - a[pick])
    return pts, nrm[pick] / area2[pick][:, None]


# view name -> (screen right, screen up, towards the viewer), all in model axes
VIEWS = {"top": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
         "front": ((1, 0, 0), (0, 0, 1), (0, -1, 0)),
         "side": ((0, 1, 0), (0, 0, 1), (1, 0, 0))}
LIGHT = np.array([-0.4, 0.5, 0.75])


def views(points, normals, angles, px=220, bed_z=True):
    """{name: (h, w, 3) uint8} shaded orthographic views of the sampled surface in pose
    `angles`, all at one scale, z-buffered by splatting each sample to a pixel. The bed
    (z = 0) is a line in the front and side views."""
    r = matrix(angles)
    c = (points.min(0) + points.max(0)) / 2
    p = (points - c) @ r.T
    p[:, 2] -= p[:, 2].min()
    nrm = normals @ r.T
    span = float((p.max(0) - p.min(0)).max()) or 1.0
    scale = (px - 8) / span
    light = LIGHT / np.linalg.norm(LIGHT)
    out = {}
    for name, (right, up, toward) in VIEWS.items():
        right, up, toward = (np.array(x, float) for x in (right, up, toward))
        sx, sy, d = p @ right, p @ up, p @ toward
        facing = nrm @ toward > 0
        ix = np.round((sx - sx.min()) * scale).astype(np.int64) + 4
        iy = np.round((sy.max() - sy) * scale).astype(np.int64) + 4
        w, h = int(ix.max()) + 5, int(iy.max()) + 5
        img = np.zeros((h, w, 3), np.uint8)
        img[:] = (30, 30, 34)
        sel = np.nonzero(facing)[0]
        sel = sel[np.argsort(-d[sel], kind="stable")]       # nearest first
        # each sample covers 2 x 2 pixels, so a sparse patch shows no specks
        sel = np.repeat(sel, 4)
        dx, dy = np.tile([0, 1, 0, 1], len(sel) // 4), np.tile([0, 0, 1, 1], len(sel) // 4)
        flat = np.minimum(iy[sel] + dy, h - 1) * w + np.minimum(ix[sel] + dx, w - 1)
        pix, first = np.unique(flat, return_index=True)     # each pixel's nearest sample
        near = sel[first]
        # light fixed to the viewer, so every view reads the same way
        lit = np.clip(0.25 + 0.75 * (nrm[near] @ (light[0] * right + light[1] * up
                                                  + light[2] * toward)), 0.15, 1.0)
        g = (60 + 180 * lit).astype(np.uint8)
        img.reshape(-1, 3)[pix] = np.stack([g, g, np.minimum(g.astype(int) + 4, 255)], 1)
        if bed_z and name != "top":
            img[min(h - 1, int(iy.max()) + 1), :] = (91, 141, 214)
        out[name] = img
    return out
