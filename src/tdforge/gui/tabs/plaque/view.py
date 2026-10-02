"""Plaque tab, view: previews, estimate, ranking viewer, export."""

from __future__ import annotations

import os

import numpy as np
import tkinter as tk
from PIL import Image
from tkinter import filedialog, messagebox, ttk

from tdforge.tools import stackforge as sf
from tdforge.core import tdcolor
from tdforge.gui.tabs.plaque import APP
from tdforge.gui.theme import OK, WARN, ImageView


class ViewMixin:
    """Centre panel and result handling of the plaque tab (state lives on PlaqueTab)."""

    def _build_center(self, master):
        f = ttk.Frame(master)
        self.nb = ttk.Notebook(f)
        self.nb.pack(fill="both", expand=True)

        self.view_target = ImageView(self.nb, "Open an image to begin  (Ctrl+O)")
        self.view_sim = ImageView(self.nb, "Generate to see the simulated print")
        self.view_err = ImageView(self.nb, "Generate to see where colour is unreachable")
        self.view_combo = ImageView(self.nb, "Tick more filaments than toolheads and Generate ranks them")

        self.nb.add(self.view_target, text="Target")
        self.nb.add(self.view_sim, text="Simulated print")
        self.nb.add(self.view_err, text="Error map")
        self.nb.add(self.view_combo, text="Combinations")

        legend = ttk.Frame(f)
        legend.pack(fill="x", pady=(6, 0))
        self.lbl_legend = ttk.Label(legend, text="", style="Hint.TLabel")
        self.lbl_legend.pack(side="left")
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._update_legend())
        return f

    def _update_legend(self):
        tab = self.nb.index(self.nb.select())
        self.lbl_legend.config(text=[
            "The image as it will be laid out, fitted to the plaque.",
            "How the print should actually look, composited through the filament stacks.",
            "Green = reachable. Red = outside the gamut; add a filament rather than "
            "changing settings.",
            "Target first, then the best loadouts with their swatches and error.",
        ][tab])

    def _refresh_estimate(self):
        self._check_stale()
        try:
            a = self.config_ns()
        except (tk.TclError, ValueError):
            return
        problems = sf.check_args(a)
        if problems:
            self.lbl_est.config(text="\n".join(problems))
            return
        sel = self.selected()
        # _validate adds the base if it is unticked, so count it here too.
        base_id = self.v_base.get()
        if base_id in self.db.filaments and base_id not in {f.id for f in sel}:
            sel = sel + [self.db.filaments[base_id]]
        n, slots = len(sel), a.slots
        total = (a.first_layer_height
                 + (a.base_layers - 1 + a.max_layers) * a.layer_height)
        w_px = max(1, int(round(a.width / a.resolution)))
        if self.source_img is not None:
            h_px = (max(1, int(round(a.height / a.resolution))) if a.height > 0
                    else max(1, int(round(w_px * self.source_img.height / self.source_img.width))))
        else:
            h_px = w_px
        combos = 0
        if n > slots:
            from math import comb
            combos = comb(max(0, n - 1), slots - 1)

        lines = [
            f"grid       {w_px} x {h_px} px",
            f"plaque     {w_px*a.resolution:.0f} x {h_px*a.resolution:.0f} x {total:.2f} mm",
            f"layers     {a.base_layers} base + {a.max_layers} colour",
            f"selected   {n} filament{'s' if n != 1 else ''}",
        ]
        if combos:
            lines.append(f"ranking    {combos} combinations")
        base = self.db.filaments.get(self.v_base.get())
        if base is not None:
            base_h = a.first_layer_height + (a.base_layers - 1) * a.layer_height
            try:
                t = float(base.transmittance(base_h).max())
            except SystemExit:
                t = 0.0
            if t > 0.01:
                lines.append(f"base       {base_h:.2f} mm passes {100*t:.0f}% — not opaque")
        est = [f.id for f in sel if f.provenance != "measured"]
        if est:
            lines.append(f"unmeasured {len(est)} of {n}")
        self.lbl_est.config(text="\n".join(lines))

    def _open_image(self):
        p = filedialog.askopenfilename(
            title="Open image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp"),
                       ("All files", "*.*")])
        if p:
            self._load_image(p)

    def _load_image(self, path):
        try:
            self.source_img = tdcolor.open_image(path)
        except Exception as exc:
            messagebox.showerror(APP, f"Could not open {path}:\n{exc}")
            return
        self.image_path = path
        self._set_title(f"{APP} — {os.path.basename(path)}")
        self._refresh_fit()
        self._refresh_estimate()
        self._status(f"loaded {os.path.basename(path)} "
                     f"({self.source_img.width}x{self.source_img.height})")

    def _refresh_fit(self):
        if self.source_img is None:
            return
        a = self.config_ns()
        w_px = max(1, int(round(a.width / a.resolution)))
        h_px = (max(1, int(round(a.height / a.resolution))) if a.height > 0
                else max(1, int(round(w_px * self.source_img.height / self.source_img.width))))
        base = self.db.filaments.get(self.v_base.get())
        pad = tuple(int(v) for v in base.rgb()) if base else (255, 255, 255)
        self.fitted = tdcolor.fit_image(self.image_path, w_px, h_px, a.fit, pad=pad)
        self.view_target.set_image(self.fitted)
        self.nb.select(0)
        self._update_legend()

    def _signature(self):
        """Everything the generated result depends on, or None if unreadable."""
        try:
            a = self.config_ns()
        except (tk.TclError, ValueError):
            return None
        pool = tuple(sorted(repr(f) for f in self.selected()))
        return (tuple(sorted(vars(a).items())), self.image_path, pool, self.v_base.get())

    def _check_stale(self):
        if self.result is None or self.result["sig"] == self._signature():
            return
        self.result = None
        self.rank_results = None
        self.btn_export.config(state="disabled")
        self._tab_titles(stale=True)
        self._status("inputs changed: Generate to update the preview", WARN)

    def _tab_titles(self, stale):
        sfx = " (out of date)" if stale else ""
        for view, title in ((self.view_sim, "Simulated print"),
                            (self.view_err, "Error map"),
                            (self.view_combo, "Combinations")):
            self.nb.tab(view, text=title + sfx)

    def _on_solved(self, r):
        self._finish()
        self.result = r
        self.rank_results = r["ranked"]
        self._tab_titles(stale=False)
        if r["ranked"]:
            self.view_combo.set_image(Image.open(r["ranked"]["sheet"]))
        else:
            self.view_combo.set_image(None)
        self.view_sim.set_image(r["achieved"])
        heat = np.clip(r["err"] / 20.0, 0, 1)
        self.view_err.set_image(
            (np.stack([heat, 1 - heat, np.zeros_like(heat)], -1) * 255).astype(np.uint8))
        self.nb.select(1)
        self._update_legend()
        self.btn_export.config(state="normal")

        e = r["err"]
        labels = r["labels"]
        per_layer = np.mean([len(np.unique(labels[i])) for i in range(labels.shape[0])])
        capped = "   ·   gamut hit the state cap" if r["gamut"].capped else ""
        thin = sf.thin_fraction(labels, r["gamut"].base_index)
        if thin > 0.05 and r["args"].resolution < sf.MIN_FEATURE_MM - 1e-9:
            capped += f"   ·   {100*thin:.0f}% of colour in 1-px features (slicer drops them)"
        if r["ranked"]:
            capped = (f"   ·   best of {len(r['ranked']['results'])} combinations: "
                      + ", ".join(f.name for f in r["fils"]) + capped)
        self._status(
            f"dE mean {e.mean():.1f}  p95 {np.percentile(e,95):.1f}  "
            f"max {e.max():.1f}  (blurred {r['blurred']:.1f})   ·   "
            f"{per_layer:.1f} filaments per layer{capped}",
            OK if e.mean() < 8 else WARN)
        self._check_stale()     # inputs may have changed while it ran

    def _export(self):
        if self.worker.busy:            # Ctrl+E bypasses the disabled button
            return
        if not self.result:
            messagebox.showwarning(APP, "Nothing to export yet: press Generate "
                                        "(it is out of date if you changed anything).")
            return
        r = self.result
        p = filedialog.asksaveasfilename(
            title="Export 3MF", defaultextension=".3mf",
            filetypes=[("3MF", "*.3mf")],
            initialfile=os.path.splitext(os.path.basename(r["image_path"] or "plaque"))[0]
            + "_plaque.3mf")
        if not p:
            return
        a = r["args"]
        try:
            plate, decals, total_h = sf.build_geometry(
                r["labels"], a.width, a.resolution, a.layer_height,
                a.first_layer_height + (a.base_layers - 1) * a.layer_height,
                r["gamut"].base_index, len(r["fils"]))
            sf.write_plaque(
                p, self.v_flavor.get(), plate, decals, r["fils"],
                r["gamut"].base_index, self.v_part.get(),
                self.v_template.get() or None, a.layer_height,
                a.first_layer_height)
        except (Exception, SystemExit) as exc:
            messagebox.showerror(APP, f"Export failed:\n{exc}")
            return
        nbox = sum(len(t) // 12 for _, _, t in decals)
        size = os.path.getsize(p) / 1e6
        self._status(f"wrote {os.path.basename(p)}  ({size:.1f} MB, {nbox} boxes)", OK)
        messagebox.showinfo(
            APP,
            f"Wrote {p}\n\n"
            f"{total_h:.2f} mm thick · {nbox} boxes · {size:.1f} MB\n\n"
            f"Load order:\n" +
            "\n".join(f"  T{i}  {f.label()}" for i, f in enumerate(r["fils"], 1)) +
            f"\n\nSlice at layer height EXACTLY {a.layer_height} mm with a "
            f"{a.first_layer_height} mm first layer, or the modifiers land "
            f"between layers and colours drop out.")

    def _save_preview(self):
        if not self.result:
            messagebox.showwarning(APP, "Generate a preview first.")
            return
        p = filedialog.asksaveasfilename(defaultextension=".png",
                                         filetypes=[("PNG", "*.png")])
        if p:
            Image.fromarray(self.result["achieved"].astype(np.uint8)).save(p)
            self._status(f"saved {os.path.basename(p)}", OK)
