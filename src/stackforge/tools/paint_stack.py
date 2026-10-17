"""Filament stacking for stackforge-paint: translucent bands along the surface normal.

plaque stacks layers on a flat plaque, all facing up. paint's surface faces every way, and
the slicer prints a band differently by direction (sliced 2026-10-17, Flash Studio 1.7.13,
0.42 mm lines, a cube banded T2 / T3 / core): on a top face every band printed exactly, down
to 0.16 mm (two 0.08 mm layers); on a wall, bands of 0.16-0.42 mm got no perimeter at all
and the wall stayed the core filament, while 0.6 mm got its own. A band must also hold a voxel
of its own along the normal, or it is never labelled. So the thinnest band a voxel can carry
follows its outward normal n:

    u_min = max(wall_band * |n_xy|, layer_height * |n_z|, voxel step along n)

The shell is cut into a few band classes (`band_classes`), each (m bands, u mm each) with its
own stack gamut, plaque's SpectralGamut with the band as its layer: a top face's are its
layers; a wall's are whole voxels, so its bands are what its voxels print (one 0.8 mm voxel of
a 1.2 mm --depth is one 0.8 mm band, not a modelled 1.2 mm); slopes take the classes between,
dividing the depth. A voxel takes the filament of its band in the stack chosen for its colour.

Every voxel down one column must choose the same stack, or the bands mix. A column is the
voxels whose normals lead to the same outermost voxel (`anchor_cells`, `columns`); its target
colour, band class and dither threshold are all read there, so the whole column solves alike.
"""

from __future__ import annotations

import numpy as np

from stackforge.core import colormath, spectral
from stackforge.tools.plaque import SpectralGamut, gamut_match, mix_pairs

WALL_BAND_MM = 0.6     # thinnest band a wall prints at 0.42 mm lines (0.42 got no perimeter)
GRID, CAP = 192, 400_000   # plaque's gamut defaults


def stack_depth(depth, lh):
    """The stacked depth: whole layers, so a top face's bands are the layers it prints.
    --depth 1.0 at 0.12 mm layers stacks 0.96 mm; below that is the base."""
    return max(1, int(depth / lh + 1e-6)) * lh


def band_classes(depth, lh, res, wall_band=WALL_BAND_MM):
    """[(m bands, u mm each)], thinnest bands first, over `depth` (a `stack_depth`).

    A top face: one band per layer. A wall: its whole voxels within `depth`, split into the
    most equal groups of at least `wall_band` (or one). Between: the top's count halved, each
    band depth / m, while thinner than the wall's. 1.2 mm, 0.08 mm layers, 0.6 mm voxels ->
    15 x 0.08, 7 x 0.17, 3 x 0.4, 2 x 0.6; with 0.8 mm voxels the wall is 1 x 0.8.
    """
    m_top = max(1, int(depth / lh + 1e-6))
    n_vox = max(1, int(depth / res + 1e-6))
    m_wall = next(m for m in range(n_vox, 0, -1)
                  if n_vox % m == 0 and (n_vox // m) * res >= wall_band - 1e-9 or m == 1)
    u_wall = n_vox * res / m_wall
    out, m = [(m_top, lh)], m_top // 2
    while m > m_wall and depth / m < u_wall - 1e-9:
        if depth / m > out[-1][1] + 1e-9:
            out.append((m, depth / m))
        m //= 2
    if u_wall > out[-1][1] + 1e-9:
        out.append((m_wall, u_wall))
    return out


def normals(dist, zs, ys, xs, res, lh, step=2):
    """(N,3) outward unit normals (x, y, z) at voxels (zs, ys, xs) of `dist`, the distance to
    the outside in mm. Its gradient points inward. Differences span `step` voxels each way,
    which smooths the staircase a voxel grid makes of a slope. Where it is flat (the middle
    of a thin part) the normal is taken as horizontal: a wall, the cautious class."""
    idx = (zs, ys, xs)

    def grad(axis, spacing):
        hi, lo = list(idx), list(idx)
        hi[axis] = np.minimum(idx[axis] + step, dist.shape[axis] - 1)
        lo[axis] = np.maximum(idx[axis] - step, 0)
        span = (hi[axis] - lo[axis]) * spacing
        return (dist[tuple(hi)] - dist[tuple(lo)]) / np.maximum(span, 1e-9)

    n = -np.stack([grad(2, res), grad(1, res), grad(0, lh)], axis=1).astype(np.float64)
    length = np.linalg.norm(n, axis=1)
    flat = length < 1e-9
    n[flat] = (1.0, 0.0, 0.0)
    length[flat] = 1.0
    return n / length[:, None]


def voxel_step(nrm, res, lh):
    """Voxel spacing along each normal: res across a wall, a layer down a top face."""
    return np.hypot(nrm[:, 0], nrm[:, 1]) * res + np.abs(nrm[:, 2]) * lh


def classify(nrm, lh, res, classes, wall_band=WALL_BAND_MM):
    """Class index into `classes` per voxel: the thinnest bands its normal can print."""
    nxy = np.hypot(nrm[:, 0], nrm[:, 1])
    u_min = np.maximum.reduce([wall_band * nxy, lh * np.abs(nrm[:, 2]), voxel_step(nrm, res, lh)])
    us = np.array([u for _, u in classes])
    fits = us[None, :] >= u_min[:, None] - 1e-9        # classes run thinnest first
    return np.where(fits.any(1), fits.argmax(1), len(classes) - 1)


def true_depth(dist, nrm, res, lh):
    """Depth of each voxel centre below the surface. The distance transform measures to the
    nearest outside voxel's centre, half a voxel beyond the surface, along the normal."""
    return np.maximum(dist - 0.5 * voxel_step(nrm, res, lh), 0.0)


def anchor_cells(cells, nrm, depth_in, res, lh):
    """(N,3) voxel index (x, y, z) of each voxel's column top: the outermost voxel on its
    normal, at (depth - half a step) up it. Computed in index space and rounded, so it lands
    on a voxel centre: snapped to the surface itself it would sit on a voxel face, where
    float noise splits one column across two cells."""
    up = np.maximum(depth_in - 0.5 * voxel_step(nrm, res, lh), 0.0)
    a = cells + nrm * up[:, None] / np.array([res, res, lh])
    return np.rint(a).astype(np.int64)


def columns(anchors, depth_in):
    """(column id per voxel, index of each column's outermost voxel). Voxels sharing an
    anchor are one column; its outermost voxel stands for it (class, target)."""
    _, col = np.unique(anchors, axis=0, return_inverse=True)
    col = col.ravel()
    order = np.lexsort((depth_in, col))
    first = order[np.r_[True, col[order][1:] != col[order][:-1]]]
    return col, first


class Stacks:
    """One SpectralGamut per band class for a filament set over its base."""

    def __init__(self, fils, base, classes, grid=GRID, cap=CAP):
        self.fils, self.base = list(fils), base
        self.ms = [m for m, _ in classes]
        self.us = [u for _, u in classes]
        spectral.require_calibrated(self.fils, what="paint stacking",
                                    alt="Or pass --stack off to paint one filament per voxel.")
        self.gamuts = [SpectralGamut(self.fils, self.fils[base], u, m, grid=grid, cap=cap,
                                     verbose=False) for m, u in classes]
        self.capped = any(g.capped for g in self.gamuts)

    def band(self, cls, depth_in):
        """Band index from the top per voxel (0 = outermost)."""
        return np.floor(depth_in / np.asarray(self.us)[cls] + 1e-6).astype(np.int64)

    def solve(self, target_lin, cls, cells, strength):
        """(state per voxel, the colour its area shows (linear), its state's own colour).

        With strength > 0 a voxel takes one of the two states whose mix is closest
        (plaque's `mix_pairs`), by the threshold at its anchor cell; strength 0 is the
        nearest state.
        """
        state = np.zeros(len(target_lin), np.int64)
        shown = np.zeros_like(target_lin)
        own = np.zeros_like(target_lin)
        srgb = colormath.linear_to_srgb(target_lin)
        for c, g in enumerate(self.gamuts):
            sel = np.nonzero(cls == c)[0]
            if not len(sel):
                continue
            if strength > 0:
                a, b, al = (v[:, 0] for v in mix_pairs(g, srgb[sel, None, :]))
                thr = 0.5 + (colormath.dither_threshold(*cells[sel].T) - 0.5) * strength
                state[sel] = np.where(al > thr, b, a)
                shown[sel] = g.colors[a] + al[:, None] * (g.colors[b] - g.colors[a])
            else:
                _, state[sel] = g.tree.query(colormath.linear_to_lab(target_lin[sel]), workers=-1)
                shown[sel] = g.colors[state[sel]]
            own[sel] = g.colors[state[sel]]
        return state, shown, own

    def labels(self, cls, state, band):
        """Filament index per voxel: the filament of its band (from the top) in its stack;
        the base below a short stack and below the last band."""
        out = np.full(len(state), self.base, np.int64)
        for c, (g, m) in enumerate(zip(self.gamuts, self.ms)):
            sel = np.nonzero(cls == c)[0]
            if not len(sel):
                continue
            cur, j = state[sel].copy(), band[sel]
            for k in range(m):                   # walk down from the top band
                down = (j > k) & (cur > 0)
                cur[down] = g.parent[cur[down]]
            f = g.fil[cur]
            out[sel] = np.where((j < m) & (cur > 0) & (f >= 0), f, self.base)
        return out

    def _base_share(self, c, state):
        """Share of each state's m bands that are the base (voxels needing no modifier), in
        one walk down the stacks."""
        g, m = self.gamuts[c], self.ms[c]
        cur, base = state.copy(), np.zeros(len(state))
        for _ in range(m):
            f = g.fil[cur]
            base += (cur <= 0) | (f < 0) | (f == self.base)
            cur = np.where(cur > 0, g.parent[np.maximum(cur, 0)], cur)
        return base / m

    def score(self, hists, mix):
        """(dE per colour, weight, base share per colour) over every class's histogram
        [(rgb, weight) | None]. The base share is of the stack each colour gets (its mix,
        weighted), for the tie-break between near-equal sets."""
        des, ws, base = [], [], []
        for c, (g, h) in enumerate(zip(self.gamuts, hists)):
            if h is None:
                continue
            rgb, w = h
            de, a, b, al = gamut_match(g, rgb, mix)
            des.append(de)
            ws.append(w)
            share_a = self._base_share(c, a)
            base.append(share_a if not mix else (1 - al) * share_a + al * self._base_share(c, b))
        return np.concatenate(des), np.concatenate(ws), np.concatenate(base)
