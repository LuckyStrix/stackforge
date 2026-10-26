"""Tool-change budget for stackforge-paint: fewer filaments on a layer, fewer tool changes.

A layer printing k tools costs k - 1 changes, and one more when it does not use the tool the
layer below ended on. Sliced (Flash Studio 1.7.13, Creator 5, the head painted in four
filaments, 2026-10-18): 2697 changes over 935 layers, every layer starting on the tool the
last one ended with and 89% of layers using all four. Which tool a layer ends on is the
slicer's choice (it orders a layer's tools by flush volume), so the count here is a bound:
a layer's possible last tools are carried forward as a set, and a layer pays the boundary
change if any tool in that set is missing from it (`step`). The slice can only come in
under it.

The plan is a set of tools per layer, always holding the base (the core prints in it on
every layer). Every subset holding the base is solved once over the whole model, giving each
layer's colour error and the tools it actually uses under that subset (`Budget`). A pass up
the layers then finds, for each weight lam, the plan minimising error + lam x changes, with
the set of possible last tools as its state (`plans`). Sweeping lam traces the frontier:
the least error for each number of changes.

One filament per voxel, a layer's labels depend only on its own subset, so the error a plan
adds up to is exact. A stacked column can cross layers (a slope's bands lie on several), and
its bands must all come from one stack: `evaluate` solves each column against its own
constraint, band by band, in the gamut of the union filtered to the stacks that satisfy it.
Plans are re-scored that way, so the curve reports what will be written.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import numpy as np
from scipy.spatial import cKDTree

from stackforge.core import colormath
from stackforge.tools import paint_stack

MAX_TOOLS = 6         # subsets holding the base: 2^(n-1) whole-model solves
LAMBDAS = np.geomspace(1e-5, 200.0, 240)   # weights, in mean dE per tool change
CURVE_POINTS = 16     # plans on the curve, spread over the change counts
FILTER_CACHE = 3_000_000   # filtered gamut states kept between plans


def _bits(mask):
    return [i for i in range(mask.bit_length()) if mask >> i & 1]


def _count(mask):
    return bin(mask).count("1")


def step(carried, used):
    """(changes at most, possible last tools) of a layer printing the tools in bitmask `used`,
    when the tool it starts on is any of bitmask `carried` (0: nothing printed yet).

    The slicer starts a layer on the tool it is holding when that tool prints there (sliced:
    every layer did), then works through the rest; the last is its choice, so any but the
    first may be it. A layer printing nothing carries the tool through.
    """
    if used == 0:
        return 0, carried
    k = _count(used)
    changes = k - 1 + (1 if carried & ~used else 0)
    if k == 1:
        return changes, used
    # Started on a tool of its own (any, on the first layer), any may end; started on a
    # carried tool t, any but t.
    last = used if carried == 0 or carried & ~used else 0
    for t in _bits(carried & used):
        last |= used & ~(1 << t)
    return changes, last


def count_changes(used):
    """Tool changes at most over the layers' tool bitmasks, bottom up."""
    carried, total = 0, 0
    for u in used:
        ch, carried = step(carried, int(u))
        total += ch
    return total


def layer_tools(lab, occ, base):
    """(nz,) bitmask of the tools each layer of one shell prints: every label on it, and the
    base wherever a solid voxel has no colour or the base's (the core under the shell)."""
    out = np.zeros(lab.shape[0], np.int64)
    for f in np.unique(lab[lab >= 0]):
        out |= (lab == f).any(axis=(1, 2)).astype(np.int64) << int(f)
    core = (occ & ((lab < 0) | (lab == base))).any(axis=(1, 2))
    return out | (core.astype(np.int64) << base)


def tools_histogram(used, n):
    """Layers printing 1, 2, ... n tools."""
    k = np.array([_count(int(u)) for u in used])
    return [int((k == i).sum()) for i in range(1, n + 1)]


def plans(cost, used, lams):
    """(len(lams), nz) subset index per layer: for each weight, the plan minimising
    cost + lam * changes (`step`'s bound), found up the layers with the set of possible last
    tools as the state. cost and used are (nz, n_subsets)."""
    nz, ns = cost.shape
    lams = np.asarray(lams, float)
    nl = len(lams)
    nstate = 1 << int(max(int(used.max()), 1)).bit_length()
    value = np.full((nstate, nl), np.inf)
    value[0] = 0.0
    back_state = np.zeros((nz, nstate, nl), np.int16)
    back_sub = np.zeros((nz, nstate, nl), np.int16)
    trans = {}
    for k in range(nz):
        new = np.full((nstate, nl), np.inf)
        for carried in np.nonzero(np.isfinite(value[:, 0]))[0]:
            for s in range(ns):
                u = int(used[k, s])
                key = (int(carried), u)
                if key not in trans:
                    trans[key] = step(*key)
                ch, last = trans[key]
                cand = value[carried] + cost[k, s] + lams * ch
                better = cand < new[last]
                if better.any():
                    new[last] = np.where(better, cand, new[last])
                    back_state[k, last][better] = carried
                    back_sub[k, last][better] = s
        value = new
    state = value.argmin(0)
    out = np.zeros((nl, nz), np.int64)
    cols = np.arange(nl)
    for k in range(nz - 1, -1, -1):
        out[:, k] = back_sub[k, state, cols]
        state = back_state[k, state, cols]
    return out


def spread(changes, n):
    """Indices of up to n entries of sorted `changes`, spread evenly over their range, the
    first and last always among them."""
    if len(changes) <= n:
        return list(range(len(changes)))
    aims = np.linspace(changes[0], changes[-1], n)
    return sorted({int(np.abs(np.asarray(changes) - a).argmin()) for a in aims})


class _Shell:
    """One shell's subset solves: per voxel, the label, error and (stacked, with a preview)
    own colour under each. `core` is the layers whose uncoloured voxels print the base."""

    def __init__(self, s, base):
        self.s = s
        (self.zs, self.ys, self.xs), _ = s.coords
        self.surf = s.surf
        self.core = (s.occ & ~s.shell).any(axis=(1, 2)).astype(np.int64) << base
        self.labels, self.de, self.own = [], [], []


class Budget:
    """Every subset holding the base, solved over every shell, and the plans built from them.

    `shells` are paint's Shells, `palette` the chosen filaments as they print (sRGB), `stacks`
    the chosen set's paint_stack.Stacks (None: one filament per voxel), `base` its index.
    `keep_own` keeps each stacked voxel's own colour, for the preview.
    """

    def __init__(self, shells, palette, stacks, fils, base, args, nz, pick=None, keep_own=False):
        self.n, self.base, self.args, self.nz = len(palette), base, args, nz
        if self.n > MAX_TOOLS:
            raise SystemExit(f"--max-toolchanges takes at most {MAX_TOOLS} filaments "
                             f"({self.n} would be {2 ** (self.n - 1)} whole-model solves)")
        self.palette, self.stacks, self.fils = np.asarray(palette, float), stacks, fils
        self.pick = pick
        self.keep_own = keep_own and stacks is not None
        # Every subset holding the base, the full set first: a plan of zeros is unrestricted.
        self.masks = sorted((m for m in range(1 << self.n) if m >> base & 1),
                            key=lambda m: (-_count(m), m))
        self.shells = [_Shell(s, base) for s in shells if s.target is not None]
        # a shell with nothing to colour still prints its core in the base
        self.core = np.zeros(nz, np.int64)
        for s in shells:
            if s.target is None:
                self.core |= s.occ.any(axis=(1, 2)).astype(np.int64) << base
        self.sub_stacks, self._filters, self._filtered_states, self._evals = {}, {}, 0, {}
        t0 = time.time()
        for m in self.masks:
            self._solve(m)
        self.cost = np.zeros((nz, len(self.masks)))
        self.used = np.repeat(self.core[:, None], len(self.masks), axis=1)
        for i in range(len(self.masks)):
            for sh in self.shells:
                np.add.at(self.cost[:, i], sh.zs[sh.surf], sh.de[i][sh.surf])
                self.used[:, i] |= self._used(sh, sh.labels[i])
        self.n_surf = sum(int(sh.surf.sum()) for sh in self.shells)
        print(f"  tool budget: {len(self.masks)} filament subsets solved ({time.time() - t0:.0f}s)")

    # -- one subset over the whole model ---------------------------------------------------

    def _stacks_for(self, mask):
        if mask == (1 << self.n) - 1:
            return self.stacks
        if mask not in self.sub_stacks:
            ids = _bits(mask)
            st = paint_stack.Stacks([self.fils[i] for i in ids], ids.index(self.base),
                                    list(zip(self.stacks.ms, self.stacks.us)), ids=ids)
            for g in st.gamuts:
                g.states = None       # the spectra (~60 MB a capped gamut); colours suffice
            self.sub_stacks[mask] = st
        return self.sub_stacks[mask]

    def _solve(self, mask):
        ids = np.array(_bits(mask))
        strength = self.pick["strength"]
        for sh in self.shells:
            s = sh.s
            own = None
            if self.stacks is None:
                idx, de = self.pick["flat"](s, self.palette[ids])
                lab = ids[idx]
            else:
                g = s.geo
                st = self._stacks_for(mask)
                col, rep = self.columns(sh)
                state, shown, own_c = st.solve(s.target[rep], g["cls"][rep], g["cells"][rep],
                                               strength)
                lab = st.labels(g["cls"], state[col], sh.band)
                de = np.linalg.norm(colormath.linear_to_lab(shown) -
                                    colormath.linear_to_lab(s.target[rep]), axis=-1)[col]
                if self.keep_own:
                    own = colormath.linear_to_srgb(own_c).astype(np.float32)[col]
            sh.labels.append(lab.astype(np.int8))
            sh.de.append(np.asarray(de, np.float32))
            sh.own.append(own)

    def columns(self, sh):
        """(column per voxel, a voxel standing for each column); stacked shells only."""
        if not hasattr(sh, "col"):
            g = sh.s.geo
            sh.col, sh.rep = paint_stack.columns(g["cells"], g["depth"])
            sh.band = self.stacks.band(g["cls"], g["depth"])
        return sh.col, sh.rep

    def _used(self, sh, labels):
        """(nz,) tool bitmask per layer, as `layer_tools` reads it from the volume."""
        out = sh.core.copy()
        np.bitwise_or.at(out, sh.zs, np.left_shift(1, labels.astype(np.int64)))
        return out

    # -- plans -----------------------------------------------------------------------------

    def search(self):
        """[(predicted changes, predicted cost, plan)]: the frontier of the plans the weights
        give, fewest changes first, ending with the unrestricted plan (all zeros)."""
        if not hasattr(self, "_front"):
            t0 = time.time()
            ks = np.arange(self.nz)
            found = {}
            for p in plans(self.cost, self.used, LAMBDAS * self.n_surf):
                found.setdefault(p.tobytes(), (count_changes(self.used[ks, p]),
                                               float(self.cost[ks, p].sum()), p))
            free = np.zeros(self.nz, np.int64)
            top = (count_changes(self.used[ks, free]), float(self.cost[ks, free].sum()), free)
            best, front = np.inf, []
            for r in sorted(found.values(), key=lambda r: (r[0], r[1])):
                if r[0] < top[0] and r[1] < best - 1e-9:     # dominated plans are dropped
                    front.append(r)
                    best = r[1]
            self._front = front + [top]
            print(f"  tool budget: {len(found)} plans from {len(LAMBDAS)} weights, "
                  f"{len(self._front)} on the frontier ({time.time() - t0:.0f}s)")
        return self._front

    def evaluate(self, plan, keep=False):
        """`plan` solved exactly, each stacked column against its own bands' layers:
        {"used" (nz,), "changes", "mean", "p95", "max", "layers"}, cached by plan. With
        `keep`, also "labels", "de" and "own" (None unless kept), per shell, per voxel."""
        plan = np.asarray(plan, np.int64)
        key = plan.tobytes()
        if not keep and key in self._evals:
            return self._evals[key]
        allowed = np.array([self.masks[i] for i in plan], np.int64)
        used = self.core.copy()
        arrays = {"labels": [], "de": [], "own": []}
        des = []
        for sh in self.shells:
            sub = plan[sh.zs]
            labels = np.empty(len(sh.zs), np.int8)
            de = np.empty(len(sh.zs), np.float32)
            own = np.empty((len(sh.zs), 3), np.float32) if self.keep_own else None
            for i in np.unique(sub):
                sel = sub == i
                labels[sel], de[sel] = sh.labels[i][sel], sh.de[i][sel]
                if own is not None:
                    own[sel] = sh.own[i][sel]
            if self.stacks is not None:
                self._mixed_columns(sh, allowed, labels, de, own)
            used |= self._used(sh, labels)
            des.append(de[sh.surf])
            for k, v in (("labels", labels), ("de", de), ("own", own)):
                arrays[k].append(v)
        de = np.concatenate(des) if des else np.zeros(1)
        stats = {"used": used, "changes": count_changes(used), "mean": float(de.mean()),
                 "p95": float(np.percentile(de, 95)), "max": float(de.max()),
                 "layers": tools_histogram(used, self.n)}
        self._evals[key] = stats
        return {**stats, **arrays} if keep else stats

    def _mixed_columns(self, sh, allowed, labels, de, own):
        """Re-solve the columns whose bands lie on layers with different subsets, in place.

        A column's band j may only hold the tools of every layer its voxels in band j sit on.
        A column whose stacked voxels all sit on layers with one subset is that subset's
        solve already (`evaluate` gathered it). The rest are grouped by their constraint and
        each group is solved in the union's gamut, filtered to the stacks that satisfy it.
        """
        g = sh.s.geo
        col, rep = self.columns(sh)
        cls_v, band = g["cls"], sh.band
        ms = np.asarray(self.stacks.ms)
        full = (1 << self.n) - 1
        inside = np.nonzero(band < ms[cls_v])[0]     # below the stack is the base anyway
        a = allowed[sh.zs[inside]]
        lo = np.full(len(rep), full, np.int64)
        hi = np.zeros(len(rep), np.int64)
        np.minimum.at(lo, col[inside], a)
        np.maximum.at(hi, col[inside], a)
        todo = np.nonzero(lo != hi)[0]
        if not len(todo):
            return
        mmax = int(ms.max())
        pat = np.full((len(rep), mmax), full, np.int64)  # a band with no voxel is free
        np.bitwise_and.at(pat, (col[inside], band[inside]), a)
        union = np.zeros(len(rep), np.int64)
        np.bitwise_or.at(union, col[inside], a)
        key = np.concatenate([cls_v[rep[todo]][:, None], union[todo][:, None], pat[todo]], axis=1)
        groups, inv = np.unique(key, axis=0, return_inverse=True)
        # each re-solved column's group, and its voxels sorted by group, once
        gid = np.full(len(rep), -1, np.int64)
        gid[todo] = inv.ravel()
        vg = gid[col]
        vox_all = np.nonzero(vg >= 0)[0]
        vox_all = vox_all[np.argsort(vg[vox_all], kind="stable")]
        bounds = np.searchsorted(vg[vox_all], np.arange(len(groups) + 1))
        by_group = np.argsort(gid[todo], kind="stable")
        cbounds = np.searchsorted(gid[todo][by_group], np.arange(len(groups) + 1))
        col_of = np.full(len(rep), -1, np.int64)       # a column's row within its group
        strength = self.pick["strength"]
        for gi, row in enumerate(groups):
            c, u = int(row[0]), int(row[1])
            cols_g = todo[by_group[cbounds[gi]:cbounds[gi + 1]]]
            col_of[cols_g] = np.arange(len(cols_g))
            st = self._stacks_for(u)
            idx, subg = self._filtered(st, c, u, tuple(int(v) for v in row[2:2 + ms[c]]))
            r = rep[cols_g]
            st_i, shown, own_c = paint_stack.solve_gamut(subg, sh.s.target[r], g["cells"][r],
                                                         strength)
            err = np.linalg.norm(colormath.linear_to_lab(shown) -
                                 colormath.linear_to_lab(sh.s.target[r]), axis=-1)
            vox = vox_all[bounds[gi]:bounds[gi + 1]]
            which = col_of[col[vox]]
            labels[vox] = st.labels(cls_v[vox], idx[st_i][which], band[vox])
            de[vox] = err[which]
            if own is not None:
                own[vox] = colormath.linear_to_srgb(own_c)[which]

    def _filtered(self, st, c, u, bands):
        """(state indices, gamut-like) of class c's stacks over subset u whose band j holds
        a tool of bands[j]; cached, since neighbouring plans share most constraints, up to
        FILTER_CACHE states in all."""
        key = (c, u, bands)
        if key not in self._filters:
            gam, bf = st.gamuts[c], st.band_fils(c)
            ok = np.ones(len(bf), bool)
            for j, mask in enumerate(bands):
                ok &= (mask >> bf[:, j]) & 1 == 1
            idx = np.nonzero(ok)[0]                # never empty: the bare base (state 0) is in
            if self._filtered_states + len(idx) > FILTER_CACHE:
                self._filters, self._filtered_states = {}, 0
            self._filtered_states += len(idx)
            self._filters[key] = idx, SimpleNamespace(colors=gam.colors[idx], lab=gam.lab[idx],
                                                      tree=cKDTree(gam.lab[idx]))
        return self._filters[key]

    # -- the curve and the choice ----------------------------------------------------------

    def curve(self, points=CURVE_POINTS):
        """Evaluated plans spread over the frontier, fewest changes first; the last is the
        unrestricted plan (`spread` always keeps it)."""
        front = self.search()
        return [self.evaluate(front[i][2]) for i in spread([r[0] for r in front], points)]

    def choose(self, limit, fits=4):
        """The plan with the least evaluated error among those with at most `limit` evaluated
        changes, evaluated with its arrays. The frontier's counts are each subset's own
        solve, and re-solving a column can add or drop a tool on a layer, so plans predicted
        a little over the limit are tried too, from the most changes down (least error
        first), until `fits` of them fit. If none does, the one with the fewest changes."""
        slack = max(10, limit // 10)
        found, fewest = [], None
        for _, _, p in reversed([r for r in self.search() if r[0] <= limit + slack]):
            ev = self.evaluate(p)
            if fewest is None or ev["changes"] < fewest[0]:
                fewest = (ev["changes"], p)
            if ev["changes"] <= limit:
                found.append((ev["mean"], p))
                if len(found) >= fits:
                    break
        if found:
            plan = min(found, key=lambda f: f[0])[1]
        elif fewest is not None:
            plan = fewest[1]
        else:
            plan = self.search()[0][2]
        return self.evaluate(plan, keep=True)


def curve_json(path, rows, change_time, chosen=None):
    """Write the curve for the GUI: one entry per plan, fewest changes first, the last
    unrestricted. `chosen` is the plan written, if --max-toolchanges picked one."""
    def entry(ev):
        return {"changes": ev["changes"], "mean": round(ev["mean"], 3),
                "p95": round(ev["p95"], 3), "max": round(ev["max"], 3),
                "layers_by_tools": ev["layers"]}
    with open(path, "w") as f:
        json.dump({"tool_change_time_s": change_time, "points": [entry(ev) for ev in rows],
                   "chosen": entry(chosen) if chosen else None}, f, indent=1)
