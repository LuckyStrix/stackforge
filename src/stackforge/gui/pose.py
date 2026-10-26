"""Pose box: turn the model before it is painted, and see what the pose costs.

Three shaded views (top, front, side) redraw as the angles change, with the posed size,
layer count and overhang area (core/pose.py). The angles are paint's --rotate field in the
form, both ways. "Estimate tool changes" runs paint as a dry run at coarse voxels with one
filament per voxel and reads its count: rough, but on one footing for every pose.
"""
from __future__ import annotations

import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from stackforge.core import pose, threemf
from stackforge.gui import theme
from stackforge.gui.argform import argv as av
from stackforge.gui.imageview import ImageView
from stackforge.gui.runner import Job, tool_command

ESTIMATE_RESOLUTION = 1.2      # mm voxels for the estimate: seconds, not minutes
_pool = ThreadPoolExecutor(max_workers=1)


def _load(path, scale_to):
    """(verts list, tris list, surface samples) of a model, off the UI thread."""
    items = threemf.read_model(path, scale_to=scale_to)
    vs, ts = [i.verts for i in items], [i.tris for i in items]
    return vs, ts, pose.sample_surface(vs, ts)


def changes_from(text):
    """(changes, layers) from paint's report, or None."""
    m = re.search(r"tool changes: at most (\d+).*? over (\d+) layers", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


class PoseBox(QWidget):
    """Angles, three views, the pose's numbers and a tool-change estimate, for `panel`."""

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.model = None           # (path, scale_to) loaded or loading
        self.mesh = None            # (verts, tris, (points, normals))
        self.future = None
        self.job = None
        self._syncing = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        title = QLabel("Pose")
        title.setStyleSheet(f"color: {theme.ACCENT}; font-weight: bold")
        lay.addWidget(title)

        grid = QGridLayout()
        self.spins = []
        for i, axis in enumerate("XYZ"):
            sp = QDoubleSpinBox()
            sp.setRange(-180, 180)
            sp.setDecimals(1)
            sp.setSingleStep(15)
            sp.setWrapping(True)
            sp.setSuffix(" deg")
            sp.valueChanged.connect(self._angles_changed)
            self.spins.append(sp)
            grid.addWidget(QLabel(axis), 0, 3 * i)
            grid.addWidget(sp, 0, 3 * i + 1)
            row = QHBoxLayout()
            for step in (-90, 90):
                b = QPushButton(f"{step:+d}")
                b.setMaximumWidth(44)
                b.clicked.connect(lambda _=False, k=i, s=step: self.turn(k, s))
                row.addWidget(b)
            grid.addLayout(row, 0, 3 * i + 2)
        lay.addLayout(grid)

        views = QHBoxLayout()
        self.views = {}
        for name in pose.VIEWS:
            col = QVBoxLayout()
            v = ImageView("no model yet")
            v.setMinimumSize(90, 90)
            self.views[name] = v
            col.addWidget(v, 1)
            cap = theme.hint(name)
            col.addWidget(cap)
            views.addLayout(col, 1)
        lay.addLayout(views, 1)

        self.info = QLabel("Choose a model to pose it.")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        row = QHBoxLayout()
        reset = QPushButton("Reset")
        reset.clicked.connect(lambda: self.set_angles((0.0, 0.0, 0.0)))
        row.addWidget(reset)
        self.est_btn = QPushButton("Estimate tool changes")
        self.est_btn.clicked.connect(self.estimate)
        row.addWidget(self.est_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.est = QLabel("")
        self.est.setWordWrap(True)
        lay.addWidget(self.est)
        lay.addWidget(theme.hint("Pose here, not in the slicer: rotating the painted 3MF there "
                                 "moves the colour off the layer grid."))
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._poll)

    # ---- angles <-> form ---------------------------------------------------------------
    def angles(self):
        return tuple(sp.value() for sp in self.spins)

    def set_angles(self, angles):
        self._syncing = True
        for sp, a in zip(self.spins, angles):
            sp.setValue(a)
        self._syncing = False
        self._angles_changed()

    def turn(self, axis, step):
        a = list(self.angles())
        a[axis] = (a[axis] + step + 180) % 360 - 180
        self.set_angles(a)

    def _angles_changed(self, *_):
        if self._syncing:
            return
        a = self.angles()
        self._syncing = True
        try:
            self.panel.form.set_values({"rotate": pose.fmt_rotate(a) if any(a) else ""})
        finally:
            self._syncing = False
        self.est.setText("")
        self.redraw()

    def form_changed(self):
        """The form changed: follow a typed --rotate, and load a newly chosen model."""
        if self._syncing:
            return
        vals = self.panel.form.values()
        text = vals.get("rotate")
        try:
            a = pose.parse_rotate(text) if text else (0.0, 0.0, 0.0)
        except ValueError:
            a = None
        if a is not None:
            a = tuple(pose.normalise_angle(x) for x in a)
        if a is not None and any(abs(x - y) > 1e-9 for x, y in zip(a, self.angles())):
            self._syncing = True
            for sp, v in zip(self.spins, a):
                sp.setValue(v)          # normalised: the spin boxes clamp to -180..180
            self._syncing = False
            self.redraw()
        path, scale = vals.get("model"), vals.get("scale_to")
        if path and os.path.isfile(path) and (path, scale) != self.model:
            self.model, self.mesh = (path, scale), None
            self.info.setText("loading the model ...")
            self.future = _pool.submit(_load, path, scale)
            self.timer.start()

    # ---- drawing -----------------------------------------------------------------------
    def _poll(self):
        if self.job is not None and self.job.done:
            self._estimated()
        if self.future is not None and self.future.done():
            fut, self.future = self.future, None
            try:
                self.mesh = fut.result()
            except BaseException as exc:            # noqa: BLE001 -- SystemExit from readers
                self.info.setText(f"could not read the model: {exc}")
            self.redraw()
        if self.future is None and (self.job is None or self.job.done):
            self.timer.stop()

    def layer_grid(self):
        vals = self.panel.form.values()
        lh, first = vals.get("layer_height"), vals.get("first_layer_height")
        if not lh and vals.get("template"):
            lh, tf = threemf.template_layer_settings(vals["template"])
            first = first or tf
        return float(lh or 0.2), float(first or lh or 0.2)

    def redraw(self):
        if self.mesh is None:
            return
        verts, tris, (pts, nrm) = self.mesh
        a = self.angles()
        for name, img in pose.views(pts, nrm, a).items():
            self.views[name].set_image(img)
        lh, first = self.layer_grid()
        st = pose.stats(pose.rotate_points(verts, a), tris, lh, first)
        sx, sy, sz = st["size"]
        self.info.setText(f"{sx:.1f} x {sy:.1f} x {sz:.1f} mm, {st['layers']} layers at "
                          f"{lh:g} mm; overhang past {pose.OVERHANG_DEG:g} deg: "
                          f"{st['overhang_mm2'] / 100:.1f} cm2 (needs support)")

    # ---- estimate ----------------------------------------------------------------------
    def estimate(self):
        if self.job is not None and not self.job.done:
            return
        form = self.panel.form
        vals = form.values()
        if not vals.get("model"):
            self.est.setText("choose a model first")
            return
        for dest in ("preview", "toolchange_curve", "max_toolchanges"):
            vals.pop(dest, None)
        tmp = tempfile.mkdtemp(prefix="stackforge-pose-")
        vals.update(output=os.path.join(tmp, "estimate.3mf"), dry_run=True, stack="off",
                    resolution=max(float(vals.get("resolution") or 0), ESTIMATE_RESOLUTION))
        missing = av.missing_required(form.leaf_view(), vals)
        if missing:
            self.est.setText("fill in the form first: " + ", ".join(missing))
            return
        self.job = Job(tool_command(self.panel.tool, av.build_argv(form.root_spec, vals,
                                                                    self.panel.command)))
        self._out = []
        self.est.setText(f"estimating at {vals['resolution']:g} mm voxels, one filament per "
                         f"voxel ...")
        self.est_btn.setEnabled(False)
        self.timer.start()

    def _estimated(self):
        job, self.job = self.job, None
        text = job.drain()
        self.est_btn.setEnabled(True)
        got = changes_from(text)
        if job.returncode != 0 or got is None:
            lines = [ln for ln in text.splitlines() if ln.strip()]
            self.est.setText("estimate failed: " + (lines[-1] if lines else f"exit {job.returncode}"))
            return
        ch, layers = got
        self.est.setText(f"pose {pose.fmt_rotate(self.angles())}: about {ch} tool changes over "
                         f"{layers} layers (coarse, one filament per voxel; compare poses, "
                         f"not runs)")
