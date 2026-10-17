"""Filament stacking for stackforge-paint: translucent bands along the surface normal.

plaque stacks layers on a flat plaque, all facing up. paint's surface faces every way, and
the slicer prints a band differently by direction (sliced 2026-10-17, Flash Studio 1.7.13,
0.42 mm lines, a cube banded T2 / T3 / core): on a top face every band printed exactly, down
to 0.16 mm (two 0.08 mm layers); on a wall, bands of 0.16-0.42 mm got no perimeter at all
and the wall stayed the core filament, while 0.6 mm got its own. So the thinnest band a voxel
can carry follows its outward normal n:

    u_min = max(wall_band * |n_xy|, layer_height * |n_z|)

and the shell (`--depth` mm) is cut into m = floor(depth / u_min) equal bands. m is snapped
down to a few classes (`band_counts`); each class has its own stack gamut, plaque's
SpectralGamut with the band as its layer. A voxel takes the filament of its band in the stack
chosen for its colour. Equal bands that fill the depth, rather than u_min bands over the base:
a wall's single band is then the whole depth of filament, as without stacking.

Every voxel down one column must choose the same stack, or the bands mix. Their targets are
nearly equal already; the dither threshold is taken at the column's surface point (voxel +
normal * depth), so they agree on that too.
"""

from __future__ import annotations

import numpy as np

from stackforge.core import colormath, spectral
from stackforge.tools.plaque import SpectralGamut, gamut_de, mix_pairs

WALL_BAND_MM = 0.6     # thinnest band a wall prints at 0.42 mm lines (0.42 got no perimeter)
GRID, CAP = 192, 400_000   # plaque's gamut defaults


def band_counts(depth, lh, wall_band=WALL_BAND_MM):
    """Band counts m, most first: one band per layer on a top face, halved down to what a
    wall can print. 1.2 mm, 0.08 mm layers, 0.6 mm walls -> [15, 7, 3, 2]."""
    m_top = max(1, int(depth / lh + 1e-6))
    m_wall = max(1, min(m_top, int(depth / wall_band + 1e-6)))
    ms, m = [], m_top
    while m > m_wall:
        ms.append(m)
        m //= 2
    return ms + [m_wall]


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


def classify(nrm, depth, lh, ms, wall_band=WALL_BAND_MM):
    """Class index into `ms` per voxel: the most bands its normal can print."""
    nxy = np.hypot(nrm[:, 0], nrm[:, 1])
    u_min = np.maximum(wall_band * nxy, lh * np.abs(nrm[:, 2]))
    allowed = np.floor(depth / np.maximum(u_min, 1e-9) + 1e-6)
    ms_arr = np.asarray(ms)
    # ms is descending, so the first m that fits is the most bands that fit
    fits = ms_arr[None, :] <= allowed[:, None]
    return np.where(fits.any(1), fits.argmax(1), len(ms) - 1)


def true_depth(dist, nrm, res, lh):
    """Depth of each voxel centre below the surface. The distance transform measures to the
    nearest outside voxel's centre, half a voxel beyond the surface, along the normal."""
    half = 0.5 * (np.hypot(nrm[:, 0], nrm[:, 1]) * res + np.abs(nrm[:, 2]) * lh)
    return np.maximum(dist - half, 0.0)


def anchor_cells(world, nrm, depth_in, res, lh):
    """Integer cells of each voxel's surface point (voxel + normal * depth): the dither
    threshold is read there, so a whole column shares it."""
    a = world + nrm * depth_in[:, None]
    return np.floor(a / np.array([res, res, lh])).astype(np.int64)


class Stacks:
    """One SpectralGamut per band class for a filament set over its base."""

    def __init__(self, fils, base, depth, ms, grid=GRID, cap=CAP):
        self.fils, self.base, self.depth, self.ms = list(fils), base, depth, list(ms)
        spectral.require_calibrated(self.fils, what="paint stacking",
                                    alt="Or pass --stack off to paint one filament per voxel.")
        self.gamuts = [SpectralGamut(self.fils, self.fils[base], depth / m, m, grid=grid, cap=cap,
                                     verbose=False) for m in self.ms]
        self.capped = any(g.capped for g in self.gamuts)

    def band(self, cls, depth_in):
        """Band index from the top per voxel (0 = outermost)."""
        u = self.depth / np.asarray(self.ms, float)[cls]
        return np.floor(depth_in / u + 1e-6).astype(np.int64)

    def solve(self, target_lin, cls, cells, strength):
        """(state per voxel, the colour its area shows (linear), its state's own colour).

        With strength > 0 a voxel takes one of the two states whose mix is closest
        (plaque's `mix_pairs`), by the threshold at its anchor cell; strength 0 is the
        nearest state.
        """
        state = np.zeros(len(target_lin), np.int64)
        shown = np.zeros_like(target_lin)
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
                _, s = g.tree.query(colormath.linear_to_lab(target_lin[sel]), workers=-1)
                state[sel] = s
                shown[sel] = g.colors[s]
        own = np.zeros_like(target_lin)
        for c, g in enumerate(self.gamuts):
            sel = cls == c
            own[sel] = g.colors[state[sel]]
        return state, shown, own

    def labels(self, cls, state, band):
        """Filament index per voxel: the filament of band `band` (from the top) in its
        stack. Below a short stack, and below the last band, is the base."""
        out = np.full(len(state), self.base, np.int64)
        for c, g in enumerate(self.gamuts):
            sel = np.nonzero(cls == c)[0]
            if not len(sel):
                continue
            cur, j = state[sel].copy(), band[sel]
            for k in range(self.ms[c]):          # walk down from the top band
                down = (j > k) & (cur > 0)
                cur[down] = g.parent[cur[down]]
            f = g.fil[cur]
            out[sel] = np.where((j < self.ms[c]) & (cur > 0) & (f >= 0), f, self.base)
        return out

    def score(self, hists, mix):
        """(dE per colour, weight) over every class's histogram [(rgb, weight) | None]."""
        des, ws = [], []
        for g, h in zip(self.gamuts, hists):
            if h is not None:
                des.append(gamut_de(g, h[0], mix))
                ws.append(h[1])
        return np.concatenate(des), np.concatenate(ws)
