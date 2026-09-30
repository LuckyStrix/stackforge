#!/usr/bin/env python3
"""filamentdb_gui -- browse, edit and grow the filament database.

    python3 filamentdb_gui.py [filaments.json]

The database is the weakest link in every other tool here: stackforge's colour
maths is only as good as the `td` and colour it is handed. The CLI can already
write those numbers, but it cannot show you what they MEAN. This can, and that
is the point of it:

  * a live stack preview -- what N layers of this filament actually look like
    over white and over black, composited with the same optical model the
    plaque generator uses. A td that is wrong by a factor of two is obvious
    here and invisible in a JSON file.
  * the calibration loop in one window: write the step-wedge 3MF, print it,
    type in (or photograph) the patches, fit, see the residuals, and commit the
    result to the entry -- with the same refusal to save a fit from a single
    low-contrast wedge that calibrate.py applies.

Nothing is written to disk until you press Save; the title bar carries a dot
while there are unsaved edits.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from copy import deepcopy
from datetime import date

import numpy as np
import tkinter as tk
from PIL import Image, ImageTk
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk

import calibrate
import guikit
import polymaker
import td3mf
import tdcolor
from filamentdb import (DB, PROVENANCE, TD_GUESS, Filament, hueforge_td,
                        seed_db, slugify)
from guikit import (ACCENT, BG, BG3, ERR, FG, FG_DIM, LINE, OK, WARN,
                    ScrollFrame, Section, readable_on, spin, swatch_image,
                    text_view)

APP = "filament library"

# What the preview composites over. Two contrasting backings, because that is
# exactly the pair that pins a td down -- a filament whose td is wrong usually
# still looks plausible over one of them.
PREVIEW_BASES = [("over white", "#F4F5F0"), ("over black", "#1A1A1C")]


# --------------------------------------------------------------------------
# optical preview
# --------------------------------------------------------------------------


def stack_colors(fil: Filament, base_hex: str, layers: int, layer_h: float) -> np.ndarray:
    """sRGB of 0..layers layers of `fil` laid over `base_hex`.

    Same single-pass model as stackforge: T = exp(-t/td) per channel, then
    alpha-over with alpha = 1-T. Composited in linear light.
    """
    base = tdcolor.srgb_to_linear(np.array(tdcolor.parse_hex(base_hex), float))
    col = fil.linear()
    out = []
    for n in range(layers + 1):
        T = fil.transmittance(n * layer_h)
        out.append(tdcolor.linear_to_srgb(base * T + col * (1 - T)))
    return np.array(out)


def opaque_at(fil: Filament, layer_h: float, limit=64) -> int | None:
    """First layer count whose transmittance drops under 1% on every channel."""
    for n in range(1, limit + 1):
        if fil.transmittance(n * layer_h).max() < 0.01:
            return n
    return None


class StackPreview(ttk.Frame):
    """Two ramps of the selected filament, over white and over black."""

    def __init__(self, master):
        super().__init__(master)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, height=190)
        self.canvas.pack(fill="both", expand=True)
        self._fil = None
        self._layers = 12
        self._layer_h = 0.08
        self.canvas.bind("<Configure>", lambda e: self.redraw())

    def show(self, fil, layers, layer_h):
        self._fil, self._layers, self._layer_h = fil, layers, layer_h
        self.redraw()

    def redraw(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 40 or h < 40:
            return
        if self._fil is None:
            c.create_text(w // 2, h // 2, text="select a filament", fill=FG_DIM)
            return
        try:
            ramps = [(lbl, stack_colors(self._fil, hexcol, self._layers, self._layer_h))
                     for lbl, hexcol in PREVIEW_BASES]
        except SystemExit as exc:              # bad td / td_rgb
            c.create_text(w // 2, h // 2, text=str(exc), fill=ERR, width=w - 20)
            return

        n = self._layers + 1
        left, top, gap = 8, 22, 10
        cw = max(6, (w - 2 * left) / n)
        rh = max(18, (h - top - 34 - gap) / 2)

        opq = opaque_at(self._fil, self._layer_h)
        for r, (label, ramp) in enumerate(ramps):
            y0 = top + r * (rh + gap)
            c.create_text(left, y0 - 7, text=label, fill=FG_DIM, anchor="w",
                          font=("TkDefaultFont", 8))
            for i, rgb in enumerate(ramp):
                x0 = left + i * cw
                c.create_rectangle(x0, y0, x0 + cw - 1, y0 + rh,
                                   fill=tdcolor.to_hex(rgb), outline="")
                if opq is not None and i == opq:
                    c.create_line(x0, y0 - 3, x0, y0 + rh + 3, fill=ACCENT, width=2)

        # Layer-count ruler under the lower ramp.
        y = top + 2 * (rh + gap) - gap + 6
        step = max(1, round(n / 12))
        for i in range(0, n, step):
            c.create_text(left + i * cw + cw / 2, y, text=str(i), fill=FG_DIM,
                          anchor="n", font=("TkFixedFont", 8))

        note = f"{self._layers} layers = {self._layers * self._layer_h:.2f} mm"
        if opq:
            note += (f"   ·   opaque (T<1%) at {opq} layers "
                     f"= {opq * self._layer_h:.2f} mm")
            colour = OK if 4 <= opq <= 14 else WARN
        else:
            note += f"   ·   still translucent at {self._layers} layers"
            colour = WARN
        c.create_text(left, y + 16, text=note, fill=colour, anchor="nw",
                      font=("TkDefaultFont", 8))


# --------------------------------------------------------------------------
# colour picking
# --------------------------------------------------------------------------


class PhotoPicker(tk.Toplevel):
    """Click a photo of a printed swatch to lift its colour.

    Averages a small patch rather than taking the single pixel under the
    cursor, because print texture and JPEG noise make one pixel meaningless.
    """

    def __init__(self, master, path, patch=7):
        super().__init__(master)
        self.title("Pick a colour")
        self.configure(bg=BG)
        self.transient(master)
        self.result = None
        self.patch = patch

        self.src = np.asarray(Image.open(path).convert("RGB"), dtype=np.float64)
        ih, iw = self.src.shape[:2]
        scale = min(880 / iw, 620 / ih, 1.0)
        self.scale = scale
        disp = Image.fromarray(self.src.astype(np.uint8)).resize(
            (max(1, int(iw * scale)), max(1, int(ih * scale))), Image.LANCZOS)
        self._tk = ImageTk.PhotoImage(disp)

        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0,
                                width=disp.width, height=disp.height)
        self.canvas.pack(padx=10, pady=10)
        self.canvas.create_image(0, 0, image=self._tk, anchor="nw")
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<B1-Motion>", self._click)

        bar = ttk.Frame(self, padding=(10, 0, 10, 10))
        bar.pack(fill="x")
        self.preview = tk.Label(bar, text="  click the swatch  ", bg=BG3, fg=FG,
                                width=22)
        self.preview.pack(side="left")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        self.btn_ok = ttk.Button(bar, text="Use this colour", style="Go.TButton",
                                 command=self._ok, state="disabled")
        self.btn_ok.pack(side="right", padx=6)
        ttk.Label(bar, text=f"averages a {patch}x{patch} px patch",
                  style="Hint.TLabel").pack(side="left", padx=10)

        self.grab_set()
        self.wait_window(self)

    def _click(self, e):
        y, x = int(e.y / self.scale), int(e.x / self.scale)
        h, w = self.src.shape[:2]
        r = self.patch // 2
        y0, y1 = max(0, y - r), min(h, y + r + 1)
        x0, x1 = max(0, x - r), min(w, x + r + 1)
        if y1 <= y0 or x1 <= x0:
            return
        rgb = self.src[y0:y1, x0:x1].reshape(-1, 3).mean(0)
        self._hex = tdcolor.to_hex(rgb)
        self.preview.config(text=f"  {self._hex}  ", bg=self._hex,
                            fg=readable_on(rgb))
        self.btn_ok.config(state="normal")

    def _ok(self):
        self.result = self._hex
        self.destroy()


def best_layers(fil, base_hex, layer_h=0.08, factor=1.6, options=(1, 2, 3, 4, 6, 8, 12, 16, 24)):
    """Layer count where a wrong td shows up most against this base.

    A single layer is the sharpest test for an opaque filament and useless for
    a translucent one -- black is already opaque at one layer, while natural
    barely absorbs anything until it is millimetres thick. So the useful
    thickness has to be chosen per filament rather than fixed.
    """
    base = tdcolor.srgb_to_linear(np.array(tdcolor.parse_hex(base_hex), float))
    col = fil.linear()

    def patch(td, n):
        T = np.exp(-(n * layer_h) / td)
        return tdcolor.srgb_to_lab(tdcolor.linear_to_srgb(base * T + col * (1 - T)))

    best, best_de = options[0], -1.0
    for n in options:
        ref = patch(fil.td, n)
        de = 0.5 * (np.linalg.norm(patch(fil.td * factor, n) - ref)
                    + np.linalg.norm(patch(fil.td / factor, n) - ref))
        if de > best_de:
            best, best_de = n, float(de)
    return best, best_de


class MatchTab(ttk.Frame):
    """Set td by eye: pick the swatch that matches a printed patch.

    Nobody without an instrument can name a colour accurately, but anyone can
    say which of two pairs has the bigger step between them. So every candidate
    is drawn as patch-against-base, and the judgement asked for is a comparison
    of contrast rather than of absolute colour -- which is also what makes the
    screen's white point and the room's lighting mostly cancel out.
    """

    N_CANDIDATES = 9
    SPAN = 2.5                      # widest td ratio offered either way

    def __init__(self, master, editor):
        super().__init__(master, padding=12)
        self.ed = editor
        self._cands = []
        self._hit = []

        ttk.Label(self, text="Match a printed patch by eye",
                  style="Head.TLabel").pack(anchor="w")
        ttk.Label(
            self,
            text="Print a wedge, then hold it against the screen and pick the "
                 "square whose step from the base colour looks like yours. "
                 "Compare the DIFFERENCE between the two halves, not the "
                 "colours themselves — that is what survives the screen being "
                 "lit differently from the plastic.",
            style="Hint.TLabel", wraplength=640, justify="left",
        ).pack(anchor="w", pady=(2, 10))

        ctl = ttk.Frame(self)
        ctl.pack(fill="x", pady=(0, 8))
        ttk.Label(ctl, text="Over").pack(side="left")
        self.v_base = tk.StringVar()
        self.cb_base = ttk.Combobox(ctl, textvariable=self.v_base, state="readonly",
                                    width=26)
        self.cb_base.pack(side="left", padx=6)
        self.cb_base.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Label(ctl, text="Layers").pack(side="left", padx=(12, 0))
        self.v_layers = tk.IntVar(value=1)
        spin(ctl, self.v_layers, 1, 40, 1, command=self.refresh, width=5).pack(
            side="left", padx=6)
        ttk.Label(ctl, text="× layer height").pack(side="left")
        spin(ctl, self.ed.v_layer, 0.04, 0.3, 0.01, command=self.refresh,
             width=6).pack(side="left", padx=6)
        for v in (self.v_layers, self.ed.v_layer):
            v.trace_add("write", lambda *_: self.refresh())
        ttk.Button(ctl, text="Best layer count", command=self._recommend).pack(
            side="left", padx=(12, 0))
        ttk.Button(ctl, text="Write wedge…", command=self._write_wedge).pack(
            side="left", padx=6)

        self.lbl_rec = ttk.Label(self, text="", style="Hint.TLabel", wraplength=640,
                                 justify="left")
        self.lbl_rec.pack(anchor="w", pady=(0, 6))

        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, height=260)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._draw())
        self.canvas.bind("<Button-1>", self._click)

        self.lbl_pick = ttk.Label(self, text="", style="Stat.TLabel")
        self.lbl_pick.pack(anchor="w", pady=(8, 0))

    # -- state -----------------------------------------------------------

    def refresh(self):
        fil = self.ed._fil()
        if fil is None:
            return
        names = [f.id for f in self.ed.db.filaments.values() if f.id != fil.id]
        self.cb_base["values"] = names
        if self.v_base.get() not in names:
            white = next((n for n in names if "white" in n), names[0] if names else "")
            self.v_base.set(white)
        self._draw()

    def _layer_h(self) -> float:
        try:
            return max(0.01, float(self.ed.v_layer.get()))
        except (tk.TclError, ValueError):
            return 0.08

    def _recommend(self):
        fil = self.ed._fil()
        base = self.ed.db.filaments.get(self.v_base.get())
        if fil is None or base is None:
            return
        lh = self._layer_h()
        best_w, de_w = best_layers(fil, base.color, lh)
        self.v_layers.set(best_w)
        mm = f"{best_w} layers = {best_w * lh:.2f} mm of {fil.name}"
        if de_w < 3:
            self.lbl_rec.config(
                text=f"{mm} over {base.name} is the best this pairing can do, and "
                     f"it is still weak (dE {de_w:.1f} for a 1.6x td error). Try a "
                     f"more contrasting base — a translucent filament shows almost "
                     f"nothing over white.",
                style="Warn.TLabel")
        else:
            self.lbl_rec.config(
                text=f"{mm} over {base.name}: a 1.6x td error moves this patch by "
                     f"dE {de_w:.1f}, so picking the closest square should pin td "
                     f"to roughly ±15–20%.",
                style="Hint.TLabel")
        self._draw()

    def _write_wedge(self):
        """Hand the wedge writer the base and depth this tab is asking about."""
        try:
            self.ed.v_steps.set(max(4, int(self.v_layers.get())))
        except (tk.TclError, ValueError):
            pass
        if self.v_base.get() in self.ed.db.filaments:
            self.ed.v_wbase.set(self.v_base.get())
        self.ed._write_wedge()

    # -- drawing ---------------------------------------------------------

    def _candidates(self, fil):
        ratios = np.exp(np.linspace(-np.log(self.SPAN), np.log(self.SPAN),
                                    self.N_CANDIDATES))
        return [(r, fil.td * r) for r in ratios]

    def _draw(self):
        c = self.canvas
        c.delete("all")
        self._hit = []
        fil = self.ed._fil()
        base = self.ed.db.filaments.get(self.v_base.get())
        w, h = c.winfo_width(), c.winfo_height()
        if fil is None or base is None or w < 60 or h < 60:
            return
        try:
            n = max(1, int(self.v_layers.get()))
        except (tk.TclError, ValueError):
            return

        lh = self._layer_h()
        base_lin = base.linear()
        col = fil.linear()
        self._cands = self._candidates(fil)

        pad, top = 10, 26
        cw = (w - 2 * pad) / len(self._cands)
        # Big patches: the whole judgement is visual, and a small swatch is a
        # harder comparison than a large one.
        ph = max(120, h - top - 72)
        for i, (ratio, td) in enumerate(self._cands):
            x0 = pad + i * cw
            T = np.exp(-(n * lh) / td)
            patch = tdcolor.to_hex(tdcolor.linear_to_srgb(base_lin * T + col * (1 - T)))
            # base on the left, filament-over-base on the right: the eye judges
            # the step between them, not either one alone
            c.create_rectangle(x0, top, x0 + cw / 2, top + ph,
                               fill=base.color, outline="")
            c.create_rectangle(x0 + cw / 2, top, x0 + cw - 4, top + ph,
                               fill=patch, outline="")
            current = abs(ratio - 1.0) < 1e-9
            c.create_rectangle(x0 - 1, top - 1, x0 + cw - 3, top + ph + 1,
                               outline=ACCENT if current else LINE,
                               width=2 if current else 1)
            c.create_text(x0 + (cw - 4) / 2, top - 12,
                          text=("now" if current else f"×{ratio:.2f}"),
                          fill=ACCENT if current else FG_DIM,
                          font=("TkDefaultFont", 8))
            c.create_text(x0 + (cw - 4) / 2, top + ph + 12, text=f"{td:.3f}",
                          fill=FG_DIM, font=("TkFixedFont", 8))
            self._hit.append((x0, x0 + cw - 4, td, ratio))

        # What to print, in millimetres. The layer count alone is meaningless
        # without the layer height it was counted at.
        c.create_text(
            pad, top + ph + 30,
            text=f"print {n} layer{'s' if n > 1 else ''} × {lh:g} mm = "
                 f"{n * lh:.2f} mm of {fil.name}, over {base.name}",
            fill=FG, anchor="w", font=("TkDefaultFont", 10, "bold"))

        # ...on a base thick enough to be opaque, or the patch is partly a
        # measurement of the build plate.
        try:
            need = int(np.ceil(base.td_vec().max() * 4.6 / lh))
        except SystemExit:
            need = 0
        if need:
            demanding = need * lh > 1.0
            c.create_text(
                pad, top + ph + 50,
                text=f"the {base.name} underneath has to be opaque: {need} layers "
                     f"({need * lh:.2f} mm)."
                     + ("  Left half of each square is that bare base."
                        if not demanding else
                        "  Thinner and you are partly measuring the build plate."),
                fill=WARN if demanding else FG_DIM, anchor="w",
                font=("TkDefaultFont", 9))

    def _click(self, e):
        fil = self.ed._fil()
        if fil is None:
            return
        for x0, x1, td, ratio in self._hit:
            if x0 <= e.x <= x1:
                self._apply(fil, td, ratio)
                return

    def _apply(self, fil, td, ratio):
        if abs(ratio - 1.0) < 1e-9:
            self.lbl_pick.config(text="that is the current value — nothing changed",
                                 foreground=FG_DIM)
            return
        base = self.ed.db.filaments.get(self.v_base.get())
        n = int(self.v_layers.get())
        lh = self._layer_h()
        old = fil.td
        self.ed.v_td.set(round(float(td), 4))
        self.ed.v_perch.set(False)
        self.ed._toggle_perchannel()
        note = (f"td set by eye: matched a printed {n}-layer patch "
                f"({n * lh:.2f} mm at {lh:g} mm layers) over {base.name} "
                f"on {date.today().isoformat()}")
        if note not in (fil.notes or ""):
            fil.notes = ((fil.notes + " | ").lstrip(" |") + note) if fil.notes else note
        fil.provenance = "matched"
        self.ed.v_prov.set("matched")
        self.ed._load_fields()
        self.ed._refresh_row(fil.id)
        self.ed.dirty = True
        if self.ed.on_dirty:
            self.ed.on_dirty(True)
        self.lbl_pick.config(text=f"td {old:.4f} → {td:.4f}  (×{ratio:.2f})  "
                                  f"— provenance now 'matched'", foreground=OK)
        self._draw()


class SkuBrowser(tk.Toplevel):
    """Search Polymaker's published hex/TD table and pick a product from it."""

    def __init__(self, master, catalog):
        super().__init__(master)
        self.title("Polymaker catalogue")
        self.configure(bg=BG)
        self.transient(master)
        self.geometry("720x560")
        self.cat = catalog
        self.result = None
        self._swatches = []

        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Search").pack(side="left")
        self.v_q = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.v_q)
        e.pack(side="left", fill="x", expand=True, padx=6)
        self.v_q.trace_add("write", lambda *_: self._render())
        ttk.Button(top, text="Refresh from wiki", command=self._refresh).pack(side="left")

        ttk.Label(
            self,
            text=f"{len(catalog.products)} products scraped "
                 f"{catalog.fetched_at or 'at some point'}. Polymaker publish a TD "
                 f"for only some of them; the rest give you a colour and leave the "
                 f"td a guess.",
            style="Hint.TLabel", wraplength=690, justify="left",
        ).pack(fill="x", padx=10)

        self.list = ScrollFrame(self, width=700)
        self.list.pack(fill="both", expand=True, padx=10, pady=6)

        bar = ttk.Frame(self, padding=(10, 0, 10, 10))
        bar.pack(fill="x")
        self.lbl = ttk.Label(bar, text="", style="Stat.TLabel")
        self.lbl.pack(side="left")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right")
        self.btn_use = ttk.Button(bar, text="Use this product", style="Go.TButton",
                                  command=self._ok, state="disabled")
        self.btn_use.pack(side="right", padx=6)

        self.selected = None
        self._render()
        e.focus_set()
        self.grab_set()
        self.wait_window(self)

    def _render(self):
        for w in self.list.inner.winfo_children():
            w.destroy()
        self._swatches.clear()
        q = self.v_q.get().strip()
        hits = self.cat.search(q, limit=200) if q else list(self.cat.products.values())[:200]
        for p in hits:
            row = tk.Frame(self.list.inner, bg=BG, pady=2)
            row.pack(fill="x")
            if p.hexes:
                img = swatch_image(tdcolor.parse_hex(p.hex), 26, 16)
                self._swatches.append(img)
                sw = tk.Label(row, image=img, bg=BG)
            else:
                sw = tk.Label(row, text=" ? ", bg=BG, fg=FG_DIM, width=4)
            sw.pack(side="left", padx=(2, 8))
            usable = bool(p.hexes) and not p.dual
            txt = f"{p.sku}   {p.product} — {p.name}"
            if p.dual:
                txt += "   (two colours, not importable)"
            elif not p.hexes:
                txt += "   (no hex published)"
            lbl = tk.Label(row, text=txt, bg=BG, fg=FG if usable else FG_DIM, anchor="w")
            lbl.pack(side="left", fill="x", expand=True)
            td = tk.Label(row, text=f"TD {p.td}" if p.td else "TD —", bg=BG,
                          fg=OK if p.td else FG_DIM, font=("TkFixedFont", 8))
            td.pack(side="right", padx=6)
            for w in (row, sw, lbl, td):
                w.bind("<Button-1>", lambda e, pp=p: self._pick(pp))
                w.bind("<Double-Button-1>", lambda e, pp=p: (self._pick(pp), self._ok()))
        self.lbl.config(text=f"{len(hits)} shown"
                             + ("  (first 200)" if len(hits) == 200 else ""))

    def _pick(self, p):
        self.selected = p
        usable = bool(p.hexes) and not p.dual
        self.btn_use.config(state="normal" if usable else "disabled")
        td = (f"TD {p.td} → td {hueforge_td(p.td):.4f} mm"
              if p.td else "no TD published — td stays a guess")
        self.lbl.config(text=f"{p.sku}  {p.hex or 'no hex'}   {td}",
                        foreground=FG if usable else WARN)

    def _ok(self):
        if self.selected and self.selected.hexes and not self.selected.dual:
            self.result = self.selected
            self.destroy()

    def _refresh(self):
        """Re-scrape on a worker so the window keeps painting."""
        self.lbl.config(text="downloading the wiki page…", foreground=FG_DIM)
        self.btn_use.config(state="disabled")
        box = {}

        def work():
            try:
                box["cat"] = polymaker.refresh(self.cat.path, verbose=False)
            except BaseException as exc:      # SystemExit included
                box["err"] = str(exc)

        t = threading.Thread(target=work, daemon=True)
        t.start()

        def poll():
            if t.is_alive():
                self.after(150, poll)
                return
            if "err" in box:
                self.lbl.config(text="refresh failed", foreground=ERR)
                messagebox.showerror(APP, box["err"], parent=self)
                return
            self.cat = box["cat"]
            self._render()
            self.lbl.config(text=f"{len(self.cat.products)} products, "
                                 f"fetched {self.cat.fetched_at}", foreground=OK)

        self.after(150, poll)


def ask_color(master, current):
    try:
        init = tdcolor.to_hex(tdcolor.parse_hex(current))
    except ValueError:
        init = "#808080"
    _, hexcol = colorchooser.askcolor(color=init, parent=master, title="Filament colour")
    return hexcol.upper() if hexcol else None


# --------------------------------------------------------------------------
# the editor
# --------------------------------------------------------------------------


class FilamentEditor(ttk.Frame):
    """The whole editor, minus window chrome, so it can be a window or a panel."""

    def __init__(self, master, db_path="filaments.json", on_dirty=None,
                 catalog_path=polymaker.CACHE):
        super().__init__(master, padding=8)
        self.db_path = db_path
        self.catalog_path = catalog_path
        self.on_dirty = on_dirty
        self._cat = None                      # Polymaker catalogue, loaded lazily
        self.db = DB(db_path)
        # One layer height for the whole window. It is a property of how you
        # print, not of a tab: the preview, the by-eye match and the wedge all
        # have to assume the same one or their millimetres disagree.
        self.v_layer = tk.DoubleVar(value=0.08)
        self.dirty = False
        self.current: str | None = None       # filament id
        self._loading = False                 # suppress field traces while loading
        self._rows: dict[str, dict] = {}
        self._swatches: list = []
        self._fit = None                      # last fit result, pending Apply

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)
        panes.add(self._build_list(panes), weight=0)
        panes.add(self._build_detail(panes), weight=1)
        self._build_status()

        self._render_list()
        if self.db.filaments:
            self.select(sorted(self.db.filaments)[0])
        else:
            self._set_editor_state(False)

    # -- left: the library ------------------------------------------------

    def _build_list(self, master):
        f = ttk.Frame(master, width=330)
        f.pack_propagate(False)

        head = ttk.Frame(f)
        head.pack(fill="x", pady=(0, 4))
        ttk.Label(head, text="Filaments", style="Head.TLabel").pack(side="left")
        self.lbl_count = ttk.Label(head, text="", style="Stat.TLabel")
        self.lbl_count.pack(side="right")

        row = ttk.Frame(f)
        row.pack(fill="x", pady=(0, 4))
        ttk.Label(row, text="Filter").pack(side="left")
        self.v_filter = tk.StringVar()
        self.v_filter.trace_add("write", lambda *_: self._render_list())
        ttk.Entry(row, textvariable=self.v_filter).pack(
            side="left", fill="x", expand=True, padx=(6, 0))

        self.list = ScrollFrame(f, width=310)
        self.list.pack(fill="both", expand=True)

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="New", command=self.new_filament).pack(
            side="left", expand=True, fill="x", padx=(0, 3))
        ttk.Button(btns, text="Duplicate", command=self.duplicate).pack(
            side="left", expand=True, fill="x", padx=3)
        ttk.Button(btns, text="Delete", style="Danger.TButton",
                   command=self.delete).pack(side="left", expand=True,
                                             fill="x", padx=(3, 0))
        ttk.Button(f, text="Add from Polymaker SKU…",
                   command=self._browse_skus).pack(fill="x", pady=(4, 0))
        return f

    def _render_list(self):
        for w in self.list.inner.winfo_children():
            w.destroy()
        self._rows.clear()
        self._swatches.clear()
        needle = self.v_filter.get().strip().lower()
        rows = sorted(self.db.filaments.values(),
                      key=lambda f: (f.brand.lower(), f.series.lower(), f.name.lower()))
        shown = 0
        for fil in rows:
            if needle and needle not in (fil.label() + " " + fil.id).lower():
                continue
            shown += 1
            self._rows[fil.id] = self._make_row(fil)
        self.lbl_count.config(
            text=f"{shown}/{len(self.db.filaments)}" if needle else f"{len(self.db.filaments)}")
        if self.current in self._rows:
            self._highlight(self.current)

    def _make_row(self, fil):
        row = tk.Frame(self.list.inner, bg=BG, padx=4, pady=3)
        row.pack(fill="x", pady=1)
        img = swatch_image(fil.rgb(), 26, 18)
        self._swatches.append(img)
        sw = tk.Label(row, image=img, bg=BG)
        sw.pack(side="left", padx=(0, 8))
        text = tk.Frame(row, bg=BG)
        text.pack(side="left", fill="x", expand=True)
        measured = fil.provenance == "measured"
        name = tk.Label(text, text=fil.name or fil.id, bg=BG,
                        fg=FG if measured else FG_DIM, anchor="w")
        name.pack(fill="x")
        sub = tk.Label(text, text=" ".join(x for x in (fil.brand, fil.series) if x),
                       bg=BG, fg=FG_DIM, anchor="w", font=("TkDefaultFont", 8))
        sub.pack(fill="x")
        right = tk.Frame(row, bg=BG)
        right.pack(side="right")
        td = tk.Label(right, text=f"td {fil.td:.2f}", bg=BG, fg=FG_DIM,
                      font=("TkFixedFont", 8))
        td.pack(side="right")
        badge = tk.Label(right, text="" if measured else "est", bg=BG, fg=WARN,
                         font=("TkDefaultFont", 7))
        badge.pack(side="right", padx=4)

        widgets = {"row": row, "swatch": sw, "name": name, "sub": sub,
                   "td": td, "badge": badge, "text": text, "right": right}
        for w in (row, sw, text, name, sub, right, td, badge):
            w.bind("<Button-1>", lambda e, fid=fil.id: self.select(fid))
        return widgets

    def _highlight(self, fid):
        for other, w in self._rows.items():
            bg = BG3 if other == fid else BG
            for key in ("row", "swatch", "text", "name", "sub", "right", "td", "badge"):
                w[key].configure(bg=bg)

    def _refresh_row(self, fid):
        """Update one row in place, so typing a name does not rebuild the list."""
        w = self._rows.get(fid)
        if not w:
            return
        fil = self.db.filaments[fid]
        img = swatch_image(fil.rgb(), 26, 18)
        self._swatches.append(img)
        w["swatch"].configure(image=img)
        w["swatch"].image = img
        measured = fil.provenance == "measured"
        w["name"].configure(text=fil.name or fil.id, fg=FG if measured else FG_DIM)
        w["sub"].configure(text=" ".join(x for x in (fil.brand, fil.series) if x))
        w["td"].configure(text=f"td {fil.td:.2f}")
        w["badge"].configure(text="" if measured else "est")

    # -- right: details, preview, calibration -----------------------------

    def _build_detail(self, master):
        f = ttk.Frame(master)
        self.nb = ttk.Notebook(f)
        self.nb.pack(fill="both", expand=True)
        self.nb.add(self._build_details(self.nb), text="Details")
        self.nb.add(self._build_look(self.nb), text="Look")
        self.match = MatchTab(self.nb, self)
        self.nb.add(self.match, text="Match by eye")
        self.nb.add(self._build_calibrate(self.nb), text="Calibrate")
        return f

    def _var(self, kind, key, cast=None):
        """A field variable wired straight into the selected filament."""
        v = {"s": tk.StringVar, "d": tk.DoubleVar, "b": tk.BooleanVar}[kind]()
        v.trace_add("write", lambda *_: self._on_field(key, v, cast))
        return v

    def _build_details(self, master):
        sc = ScrollFrame(master, width=520)
        f = sc.inner

        idn = Section(f, "Identity")
        idn.pack(fill="x", padx=10, pady=(10, 8))
        self.v_id = tk.StringVar()
        idrow = ttk.Frame(idn)
        self.e_id = ttk.Entry(idrow, textvariable=self.v_id)
        self.e_id.pack(side="left", fill="x", expand=True)
        self.e_id.bind("<Return>", lambda e: self._commit_id())
        self.e_id.bind("<FocusOut>", lambda e: self._commit_id())
        ttk.Button(idrow, text="from name", width=10,
                   command=self._reslug).pack(side="left", padx=(6, 0))
        idn.field("id", idrow,
                  "How every other tool refers to this filament. Renaming one "
                  "breaks any command line that names it.")
        self.v_brand = self._var("s", "brand")
        self.v_series = self._var("s", "series")
        self.v_name = self._var("s", "name")
        idn.field("Brand", ttk.Entry(idn, textvariable=self.v_brand))
        idn.field("Series", ttk.Entry(idn, textvariable=self.v_series))
        idn.field("Name", ttk.Entry(idn, textvariable=self.v_name))
        self.v_sku = self._var("s", "sku")
        skurow = ttk.Frame(idn)
        e = ttk.Entry(skurow, textvariable=self.v_sku, width=12)
        e.pack(side="left")
        e.bind("<Return>", lambda ev: self._lookup_sku())
        ttk.Button(skurow, text="Look up", width=8,
                   command=self._lookup_sku).pack(side="left", padx=6)
        ttk.Button(skurow, text="Browse…", width=9,
                   command=self._browse_skus).pack(side="left")
        idn.field("Polymaker SKU", skurow,
                  "Fills the colour, and the TD where Polymaker publish one, "
                  "from their wiki. Vendor data, never 'measured'.")

        ap = Section(f, "Appearance")
        ap.pack(fill="x", padx=10, pady=(0, 8))
        self.v_color = self._var("s", "color")
        crow = ttk.Frame(ap)
        self.e_color = ttk.Entry(crow, textvariable=self.v_color, width=10)
        self.e_color.pack(side="left")
        self.big_swatch = tk.Label(crow, text="        ", bg=BG3, relief="flat")
        self.big_swatch.pack(side="left", padx=8, fill="y")
        ttk.Button(crow, text="Pick…", width=7,
                   command=self._pick_color).pack(side="left")
        ttk.Button(crow, text="From photo…", width=12,
                   command=self._pick_from_photo).pack(side="left", padx=(6, 0))
        ap.field("Colour", crow,
                 "The bulk colour: what a fully opaque slab of it looks like, "
                 "not what one layer looks like over white.")
        self.v_finish = self._var("s", "finish")
        ap.field("Finish", ttk.Combobox(ap, textvariable=self.v_finish,
                                        state="readonly", values=sorted(TD_GUESS)),
                 "Only a bookkeeping label — but changing it offers the matching "
                 "starter td.")

        op = Section(f, "Optics")
        op.pack(fill="x", padx=10, pady=(0, 8))
        self.v_td = self._var("d", "td", float)
        tdrow = ttk.Frame(op)
        spin(tdrow, self.v_td, 0.01, 5.0, 0.01, width=9).pack(side="left")
        ttk.Button(tdrow, text="Estimate from colour", width=19,
                   command=self._guess_td).pack(side="left", padx=6)
        op.field("td (mm)", tdrow,
                 "Thickness at which transmittance falls to 1/e (36.8%): "
                 "T(t) = exp(-t/td). NOT HueForge's TD scale. Estimating fits "
                 "this colour against every TD Polymaker publish — better than "
                 "a finish-based guess, still no substitute for a wedge.")
        self.v_perch = tk.BooleanVar()
        op.row(ttk.Checkbutton(op, text="per-channel td", variable=self.v_perch,
                               command=self._toggle_perchannel))
        prow = ttk.Frame(op)
        self.v_tdr = [self._var("d", f"td{c}", float) for c in "rgb"]
        self.e_tdr = []
        for i, c in enumerate("RGB"):
            ttk.Label(prow, text=c).pack(side="left", padx=(0 if i == 0 else 8, 3))
            e = spin(prow, self.v_tdr[i], 0.01, 5.0, 0.01, width=7)
            e.pack(side="left")
            self.e_tdr.append(e)
        op.row(prow)
        op.note("A red that passes red but blocks green and blue cannot be "
                "described by one number. If a scalar fit reports a bad residual "
                "on clean measurements, that is the signal to switch.")
        hf = ttk.Frame(op)
        self.v_hueforge = tk.StringVar()
        ttk.Entry(hf, textvariable=self.v_hueforge, width=8).pack(side="left")
        ttk.Button(hf, text="Convert", command=self._import_hueforge).pack(
            side="left", padx=6)
        op.field("HueForge TD", hf,
                 "Theirs is closer to 'thickness to opacity'; this divides by "
                 "4.6 and marks the entry as vendor data.")

        pv = Section(f, "Provenance")
        pv.pack(fill="x", padx=10, pady=(0, 8))
        self.v_prov = self._var("s", "provenance")
        pv.field("Source", ttk.Combobox(pv, textvariable=self.v_prov,
                                        state="readonly", values=list(PROVENANCE)),
                 "'estimated' entries print approximate colours and are dimmed "
                 "everywhere. Only a calibration fit should make one 'measured'.")
        self.v_measured = self._var("s", "measured_at")
        pv.field("Measured at", ttk.Entry(pv, textvariable=self.v_measured))
        self.v_lhref = self._var("d", "layer_height_ref", float)
        pv.field("Layer height ref", spin(pv, self.v_lhref, 0.0, 0.5, 0.01),
                 "The layer height the measurement was taken at.")
        self.v_tags = self._var("s", "tags")
        pv.field("Tags", ttk.Entry(pv, textvariable=self.v_tags), "Comma separated.")
        self.t_notes = tk.Text(pv, height=4, bg=BG3, fg=FG, insertbackground=FG,
                               relief="flat", borderwidth=0, padx=6, pady=4,
                               wrap="word", font=("TkDefaultFont", 9))
        self.t_notes.bind("<KeyRelease>", lambda e: self._on_notes())
        pv.field("Notes", self.t_notes)
        return sc

    def _build_look(self, master):
        f = ttk.Frame(master, padding=12)
        ttk.Label(f, text="Stacked over white and over black",
                  style="Head.TLabel").pack(anchor="w")
        ttk.Label(
            f,
            text="Composited with the same model stackforge uses. This is the "
                 "quickest check that a td is sane: a filament that goes opaque "
                 "in two layers, or is still see-through at twenty, will not "
                 "behave in a plaque the way the numbers claim.",
            style="Hint.TLabel", wraplength=620, justify="left",
        ).pack(anchor="w", pady=(2, 10))

        ctl = ttk.Frame(f)
        ctl.pack(fill="x", pady=(0, 8))
        ttk.Label(ctl, text="Layer height").pack(side="left")
        self.v_pv_layer = self.v_layer
        spin(ctl, self.v_pv_layer, 0.04, 0.3, 0.01,
             command=self._refresh_preview, width=7).pack(side="left", padx=(6, 16))
        ttk.Label(ctl, text="Layers").pack(side="left")
        self.v_pv_layers = tk.IntVar(value=14)
        spin(ctl, self.v_pv_layers, 2, 40, 1,
             command=self._refresh_preview, width=7).pack(side="left", padx=6)
        for v in (self.v_pv_layer, self.v_pv_layers):
            v.trace_add("write", lambda *_: self._refresh_preview())

        self.preview = StackPreview(f)
        self.preview.pack(fill="both", expand=True)

        self.t_optics = text_view(f, height=12)
        self.t_optics.pack(fill="x", pady=(10, 0))
        return f

    def _build_calibrate(self, master):
        sc = self.cal_scroll = ScrollFrame(master, width=520)
        f = sc.inner

        w = Section(f, "1 · Print a step wedge")
        w.pack(fill="x", padx=10, pady=(10, 8))
        w.note("A staircase carrying 1..N layers of this filament over an opaque "
               "base. Print one over white AND one over black if you can: two "
               "backgrounds separate the filament's colour from its opacity, "
               "which a single background cannot do.")
        self.v_wbase = tk.StringVar()
        self.cb_wbase = ttk.Combobox(w, textvariable=self.v_wbase, state="readonly")
        w.field("Base filament", self.cb_wbase)
        self.v_steps = tk.IntVar(value=12)
        self.v_baselayers = tk.IntVar(value=8)
        self.v_stepw = tk.DoubleVar(value=10.0)
        self.v_stepd = tk.DoubleVar(value=14.0)
        self.v_flavor = tk.StringVar(value="orca")
        w.field("Steps", spin(w, self.v_steps, 3, 40, 1))
        w.field("Layer height (mm)", spin(w, self.v_layer, 0.04, 0.3, 0.01),
                "Slice at exactly this, or the steps carry the wrong thickness "
                "and the fit is meaningless.")
        w.field("Base layers", spin(w, self.v_baselayers, 2, 30, 1))
        w.field("Step width (mm)", spin(w, self.v_stepw, 4, 40, 1))
        w.field("Step depth (mm)", spin(w, self.v_stepd, 4, 60, 1))
        w.field("Slicer flavour", ttk.Combobox(w, textvariable=self.v_flavor,
                                               state="readonly",
                                               values=["orca", "prusa"]))
        w.row(ttk.Button(w, text="Write wedge 3MF…", command=self._write_wedge))

        m = Section(f, "2 · Measured patches")
        m.pack(fill="x", padx=10, pady=(0, 8))
        m.note("Thinnest step first, comma separated. A spectrophotometer is "
               "ideal; a phone photo under flat indirect daylight with a white "
               "card in frame, white-balanced against the card, works well "
               "enough.")
        self.wedge_a = self._wedge_inputs(m, "Wedge A", "#F4F5F0")
        self.wedge_b = self._wedge_inputs(m, "Wedge B (contrasting base)", "#1A1A1C")
        self.v_perchannel_fit = tk.BooleanVar()
        m.row(ttk.Checkbutton(m, text="fit td per RGB channel (needs ≥6 steps)",
                              variable=self.v_perchannel_fit))
        go = ttk.Frame(m)
        ttk.Button(go, text="Fit", style="Go.TButton",
                   command=self._do_fit).pack(side="left")
        self.btn_apply = ttk.Button(go, text="Apply to filament",
                                    command=self._apply_fit, state="disabled")
        self.btn_apply.pack(side="left", padx=8)
        m.row(go)

        self.t_fit = text_view(f, height=18)
        self.t_fit.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        return sc

    def _wedge_inputs(self, parent, title, default_base):
        """One wedge's base colour and patch list."""
        box = ttk.Frame(parent)
        parent.row(box)
        ttk.Label(box, text=title, style="Head.TLabel",
                  font=("TkDefaultFont", 9, "bold")).pack(anchor="w", pady=(6, 2))
        top = ttk.Frame(box)
        top.pack(fill="x")
        ttk.Label(top, text="Base").pack(side="left")
        v_base = tk.StringVar(value=default_base)
        ttk.Combobox(top, textvariable=v_base, width=22).pack(side="left", padx=6)
        ttk.Label(top, text="hex, or a filament id", style="Hint.TLabel").pack(side="left")
        txt = tk.Text(box, height=3, bg=BG3, fg=FG, insertbackground=FG,
                      relief="flat", borderwidth=0, padx=6, pady=4, wrap="word",
                      font=("TkFixedFont", 9))
        txt.pack(fill="x", pady=4)
        btns = ttk.Frame(box)
        btns.pack(fill="x")
        ttk.Button(btns, text="From photo…", width=13,
                   command=lambda: self._patches_from_photo(txt)).pack(side="left")
        ttk.Button(btns, text="Clear", width=7,
                   command=lambda: txt.delete("1.0", "end")).pack(side="left", padx=6)
        return {"base": v_base, "text": txt}

    def _build_status(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(8, 0))
        self.btn_save = ttk.Button(bar, text="Save", style="Go.TButton",
                                   command=self.save)
        self.btn_save.pack(side="left")
        ttk.Button(bar, text="Revert", command=self.revert).pack(side="left", padx=6)
        self.lbl_path = ttk.Label(bar, text="", style="Stat.TLabel")
        self.lbl_path.pack(side="left", padx=12)
        self.lbl_status = ttk.Label(bar, text="", style="Stat.TLabel")
        self.lbl_status.pack(side="right")
        self._status("ready")

    # -- selection and field binding --------------------------------------

    def select(self, fid):
        if fid not in self.db.filaments:
            return
        self.current = fid
        self._load_fields()
        self._highlight(fid)
        self._reset_fit()
        self.cb_wbase["values"] = sorted(self.db.filaments)
        if self.v_wbase.get() not in self.db.filaments:
            white = next((i for i in sorted(self.db.filaments) if "white" in i), None)
            self.v_wbase.set(white or fid)

    def _load_fields(self):
        """Pull every widget's value from the selected entry."""
        fil = self._fil()
        if fil is None:
            return
        self._loading = True
        try:
            self.v_id.set(fil.id)
            self.v_brand.set(fil.brand)
            self.v_series.set(fil.series)
            self.v_name.set(fil.name)
            self.v_color.set(fil.color)
            self.v_sku.set(fil.sku)
            self.v_finish.set(fil.finish)
            self.v_td.set(fil.td)
            self.v_prov.set(fil.provenance)
            self.v_measured.set(fil.measured_at)
            self.v_lhref.set(fil.layer_height_ref)
            self.v_tags.set(", ".join(fil.tags or []))
            self.t_notes.delete("1.0", "end")
            self.t_notes.insert("1.0", fil.notes or "")
            self.v_perch.set(bool(fil.td_rgb))
            for i in range(3):
                self.v_tdr[i].set(float(fil.td_rgb[i]) if fil.td_rgb else fil.td)
        finally:
            self._loading = False
        self._set_editor_state(True)
        self._toggle_perchannel(write=False)
        self._refresh_color_ui()
        self._refresh_preview()
        if getattr(self, "match", None):
            self.match.refresh()

    def _fil(self) -> Filament | None:
        return self.db.filaments.get(self.current) if self.current else None

    def _on_field(self, key, var, cast):
        if self._loading:
            return
        fil = self._fil()
        if fil is None:
            return
        try:
            val = var.get()
        except tk.TclError:                    # half-typed number
            return
        if cast:
            try:
                val = cast(val)
            except (TypeError, ValueError):
                return

        if key in ("tdr", "tdg", "tdb"):
            if not self.v_perch.get():
                return
            vals = []
            for v in self.v_tdr:
                try:
                    vals.append(float(v.get()))
                except (tk.TclError, ValueError):
                    return
            if min(vals) <= 0:
                return
            fil.td_rgb = [round(x, 4) for x in vals]
        elif key == "tags":
            fil.tags = [t.strip() for t in str(val).split(",") if t.strip()]
        elif key == "color":
            try:
                tdcolor.parse_hex(str(val))
            except ValueError:
                self.big_swatch.configure(bg=BG3, text="  ?  ")
                return
            fil.color = tdcolor.to_hex(tdcolor.parse_hex(str(val)))
            self._refresh_color_ui()
        elif key == "td":
            if val <= 0:
                return
            fil.td = round(float(val), 4)
        elif key == "finish":
            fil.finish = str(val)
            self._offer_finish_td(str(val))
        else:
            setattr(fil, key, val)

        self._touch()

    def _on_notes(self):
        fil = self._fil()
        if fil is None or self._loading:
            return
        fil.notes = self.t_notes.get("1.0", "end").strip()
        self._touch()

    def _touch(self):
        """Mark unsaved and push the change into the list row and preview."""
        self.dirty = True
        if self.current:
            self._refresh_row(self.current)
        self._refresh_preview()
        self._status("unsaved changes", WARN)
        if self.on_dirty:
            self.on_dirty(True)

    def _set_editor_state(self, on):
        state = "normal" if on else "disabled"
        for w in (self.e_id, self.e_color, self.t_notes):
            w.configure(state=state)

    def _refresh_color_ui(self):
        fil = self._fil()
        if fil is None:
            return
        try:
            rgb = fil.rgb()
        except ValueError:
            return
        self.big_swatch.configure(bg=tdcolor.to_hex(rgb), text="        ")

    def _offer_finish_td(self, finish):
        """Changing the finish label is usually a request for its starter td."""
        fil = self._fil()
        guess = TD_GUESS.get(finish)
        if fil is None or guess is None or abs(fil.td - guess) < 1e-9:
            return
        if fil.provenance == "measured":
            return                             # never overwrite a measurement
        if messagebox.askyesno(
            APP,
            f"Set td to the starter value for a {finish} filament "
            f"({guess} mm)?\n\nIt is a coarse guess — enough to slice something "
            f"today, not enough to trust the colour.",
            parent=self,
        ):
            self.v_td.set(guess)

    def _toggle_perchannel(self, write=True):
        on = self.v_perch.get()
        for e in self.e_tdr:
            e.configure(state="normal" if on else "disabled")
        fil = self._fil()
        if fil is None or not write or self._loading:
            return
        if on:
            try:
                vals = [float(v.get()) for v in self.v_tdr]
            except (tk.TclError, ValueError):
                vals = [fil.td] * 3
            if min(vals) <= 0:
                vals = [fil.td] * 3
            fil.td_rgb = [round(x, 4) for x in vals]
            self._loading = True
            for i, x in enumerate(vals):
                self.v_tdr[i].set(x)
            self._loading = False
        else:
            fil.td_rgb = None
        self._touch()

    # -- details actions ---------------------------------------------------

    def _commit_id(self):
        """Rename, keeping the dict key and the entry's own id in step."""
        fil = self._fil()
        if fil is None:
            return
        new = slugify(self.v_id.get())
        if not new or new == fil.id:
            self.v_id.set(fil.id)
            return
        if new in self.db.filaments:
            messagebox.showwarning(APP, f"{new} already exists.", parent=self)
            self.v_id.set(fil.id)
            return
        del self.db.filaments[fil.id]
        fil.id = new
        self.db.filaments[new] = fil
        self.current = new
        self.dirty = True
        self._render_list()
        self._status(f"renamed to {new}", WARN)

    def _reslug(self):
        fil = self._fil()
        if fil is None:
            return
        self.v_id.set(slugify(fil.brand, fil.series, fil.name))
        self._commit_id()

    def _pick_color(self):
        c = ask_color(self, self.v_color.get())
        if c:
            self.v_color.set(c)

    def _pick_from_photo(self):
        p = filedialog.askopenfilename(
            title="Photo of a printed swatch",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp"),
                       ("All files", "*.*")], parent=self)
        if not p:
            return
        picked = PhotoPicker(self.winfo_toplevel(), p).result
        if picked:
            self.v_color.set(picked)
            self._status(f"colour {picked} sampled from "
                         f"{os.path.basename(p)}", OK)

    # -- Polymaker catalogue ----------------------------------------------

    def _catalog(self):
        """The scraped wiki table, offering to fetch it if it is not there yet."""
        if self._cat is None:
            try:
                self._cat = polymaker.Catalog(self.catalog_path)
            except SystemExit as exc:
                messagebox.showerror(APP, str(exc), parent=self)
                return None
        if not self._cat.available:
            if not messagebox.askyesno(
                APP,
                f"No catalogue at {self.catalog_path}.\n\nDownload Polymaker's "
                f"hex and TD table from their wiki now?",
                parent=self,
            ):
                return None
            self._status("downloading the Polymaker wiki…")
            self.update_idletasks()
            try:
                self._cat = polymaker.refresh(self.catalog_path, verbose=False)
            except BaseException as exc:
                messagebox.showerror(APP, f"Could not fetch the catalogue:\n{exc}",
                                     parent=self)
                return None
            self._status(f"catalogue: {len(self._cat.products)} products", OK)
        return self._cat

    def _apply_product(self, p, fil):
        """Write one catalogue row into `fil` and refresh everything showing it."""
        try:
            _, warnings = polymaker.to_filament(
                p, fil, fid=fil.id, fetched_at=self._cat.fetched_at)
        except SystemExit as exc:
            messagebox.showwarning(APP, str(exc), parent=self)
            return False
        self._load_fields()
        self._refresh_row(fil.id)
        self.dirty = True
        if self.on_dirty:
            self.on_dirty(True)
        note = f"{p.sku}: {p.name} {fil.color}"
        note += f", td {fil.td} from TD {p.td}" if p.td else ", no TD published"
        want = slugify(fil.brand, fil.series, fil.name)
        if fil.id != want:
            note += f"  ·  id is still {fil.id}"
        self._status(note, WARN if warnings else OK)
        for w in warnings:
            messagebox.showinfo(APP, w, parent=self)
        return True

    def _lookup_sku(self):
        fil = self._fil()
        sku = self.v_sku.get().strip()
        if fil is None:
            return
        if not sku:
            self._browse_skus()
            return
        cat = self._catalog()
        if cat is None:
            return
        try:
            p = cat.get(sku)
        except SystemExit as exc:
            messagebox.showwarning(APP, str(exc), parent=self)
            return
        self._apply_product(p, fil)

    def _browse_skus(self):
        cat = self._catalog()
        if cat is None:
            return
        p = SkuBrowser(self.winfo_toplevel(), cat).result
        if p is None:
            return
        # An id derived from the product name is how the CLI import addresses
        # the same filament, so keep the two in step.
        fid = slugify("Polymaker", p.series(), p.name)
        fil = self._fil()
        if fil is not None and fil.id != fid and not messagebox.askyesno(
            APP,
            f"{p.sku} is {p.product} — {p.name}.\n\nAdd it as a new entry "
            f"({fid})?\n\nNo overwrites the selected entry, {fil.label()}, "
            f"instead.",
            parent=self,
        ):
            self._apply_product(p, fil)
            return
        created = fid not in self.db.filaments
        if not created:
            if not messagebox.askyesno(
                APP, f"{fid} already exists. Update it from the catalogue?",
                parent=self):
                return
            if (self.db.filaments[fid].provenance == "measured"
                    and not messagebox.askyesno(
                        APP,
                        f"{fid} is marked measured — a wedge was printed for "
                        f"it.\n\nReplace those numbers with vendor data?",
                        parent=self)):
                return
            target = self.db.filaments[fid]
        else:
            target = Filament(id=fid)
            self.db.filaments[fid] = target
            self._render_list()
        self.current = fid
        if self._apply_product(p, target):
            self._render_list()
            self.select(fid)
            self.nb.select(0)
        elif created:
            del self.db.filaments[fid]         # never leave a blank entry behind
            self.current = None
            self._render_list()

    def _guess_td(self):
        fil = self._fil()
        if fil is None:
            return
        cat = self._catalog()
        if cat is None:
            return
        try:
            est = cat.estimate_td(fil.color, fil.finish)
        except SystemExit as exc:
            messagebox.showwarning(APP, str(exc), parent=self)
            return
        if fil.provenance == "measured" and not messagebox.askyesno(
            APP,
            f"{fil.label()} is marked measured — a wedge was printed for it.\n\n"
            f"Replace td {fil.td} with the guess {est.td}?",
            parent=self,
        ):
            return
        self.v_td.set(est.td)
        self.v_perch.set(False)
        self._toggle_perchannel()
        note = (f"td estimated from {est.n} published Polymaker {est.group} TDs "
                f"by {est.method} (typically off by ~{est.typical_factor}x)")
        if note not in (fil.notes or ""):
            fil.notes = ((fil.notes + " | ").lstrip(" |") + note) if fil.notes else note
            self.t_notes.delete("1.0", "end")
            self.t_notes.insert("1.0", fil.notes)
        self._touch()
        name, hx, de, td = est.nearest
        self._status(f"td {est.td} from {est.n} {est.group} filaments "
                     f"({est.method}); nearest {name} {hx} dE {de:.0f}, TD {td}",
                     WARN)
        self.nb.select(1)

    def _import_hueforge(self):
        fil = self._fil()
        if fil is None:
            return
        try:
            hf = float(self.v_hueforge.get())
        except ValueError:
            messagebox.showwarning(APP, "Enter HueForge's TD number first.", parent=self)
            return
        if hf <= 0:
            return
        td = round(hf / 4.6, 4)
        self.v_td.set(td)
        self.v_perch.set(False)
        self._toggle_perchannel()
        fil.provenance = "vendor"
        self.v_prov.set("vendor")
        fil.notes = (fil.notes + " | ").lstrip(" |") + f"td converted from HueForge TD {hf}"
        self.t_notes.delete("1.0", "end")
        self.t_notes.insert("1.0", fil.notes)
        self._touch()
        self._status(f"td {td} from HueForge TD {hf}", OK)

    # -- preview -----------------------------------------------------------

    def _refresh_preview(self):
        fil = self._fil()
        if fil is None:
            return
        try:
            layers = int(self.v_pv_layers.get())
            lh = float(self.v_pv_layer.get())
        except (tk.TclError, ValueError):
            return
        self.preview.show(fil, layers, lh)

        t = self.t_optics
        t.configure(state="normal")
        t.delete("1.0", "end")
        try:
            tdv = fil.td_vec()
        except SystemExit as exc:
            t.insert("end", str(exc), "err")
            t.configure(state="disabled")
            return
        t.insert("end", f"{fil.label()}   {fil.color}   "
                        f"td {np.round(tdv, 4).tolist()}\n\n", "head")
        t.insert("end", "  layers      mm     transmittance R,G,B\n", "dim")
        for n in (1, 2, 4, 8, 16, 24):
            tr = fil.transmittance(n * lh)
            tag = "ok" if tr.max() < 0.01 else ""
            t.insert("end", f"  {n:6d}  {n*lh:6.2f}     "
                            f"{tr[0]:.4f} {tr[1]:.4f} {tr[2]:.4f}"
                            f"{'   opaque' if tr.max() < 0.01 else ''}\n", tag)
        if fil.provenance != "measured":
            t.insert("end",
                     "\n  These numbers are a guess, not a measurement. Print a "
                     "wedge from the Calibrate tab.\n", "warn")
        t.configure(state="disabled")

    # -- calibration -------------------------------------------------------

    def _write_wedge(self):
        fil = self._fil()
        if fil is None:
            return
        base_id = self.v_wbase.get()
        if base_id not in self.db.filaments:
            messagebox.showwarning(APP, "Choose a base filament.", parent=self)
            return
        base = self.db.filaments[base_id]
        p = filedialog.asksaveasfilename(
            title="Write step wedge", defaultextension=".3mf",
            filetypes=[("3MF", "*.3mf")], initialfile=f"wedge_{fil.id}.3mf",
            parent=self)
        if not p:
            return
        steps = int(self.v_steps.get())
        lh = float(self.v_layer.get())
        try:
            plate, decals, w, base_h = calibrate.build_wedge(
                steps, lh, int(self.v_baselayers.get()),
                float(self.v_stepw.get()), float(self.v_stepd.get()), 0.0)
            td3mf.get_writer(self.v_flavor.get())(p, [plate], {0: decals}, 1, "part")
        except Exception as exc:
            messagebox.showerror(APP, f"Could not write the wedge:\n{exc}", parent=self)
            return
        self._status(f"wrote {os.path.basename(p)}", OK)
        messagebox.showinfo(
            APP,
            f"Wrote {p}\n\n"
            f"{steps} steps, 1..{steps} layers of {fil.label()} over "
            f"{int(self.v_baselayers.get())} base layers of {base.label()}.\n"
            f"{w:.1f} × {float(self.v_stepd.get()):.1f} mm, "
            f"{base_h:.2f}..{base_h + steps*lh:.2f} mm tall.\n\n"
            f"Extruder 1 = {base.label()}\nExtruder 2 = {fil.label()}\n\n"
            f"Slice at layer height EXACTLY {lh} mm.\n\n"
            "Print a second wedge over a contrasting base if you can — one "
            "background cannot separate colour from opacity.",
            parent=self)

    def _patches_from_photo(self, txt):
        p = filedialog.askopenfilename(
            title="Photo of the printed wedge",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp"),
                       ("All files", "*.*")], parent=self)
        if not p:
            return
        n = int(self.v_steps.get())
        axis = "x" if messagebox.askyesno(
            APP, "Do the steps run left to right?\n\nNo = top to bottom.",
            parent=self) else "y"
        try:
            patches = calibrate.sample_image(p, n, axis)
        except Exception as exc:
            messagebox.showerror(APP, f"Could not sample that image:\n{exc}", parent=self)
            return
        txt.delete("1.0", "end")
        txt.insert("1.0", ",".join(tdcolor.to_hex(c) for c in patches))
        self._status(f"sampled {n} patches from {os.path.basename(p)}", OK)

    def _read_wedge(self, w):
        """(measured (n,3), base rgb) for one wedge, or None if it is empty."""
        raw = w["text"].get("1.0", "end").strip()
        if not raw:
            return None
        toks = [t for t in re.split(r"[,\s]+", raw) if t]
        meas = np.array([tdcolor.parse_hex(t) for t in toks], float)
        spec = w["base"].get().strip()
        try:
            base = np.array(tdcolor.parse_hex(spec), float)
        except ValueError:
            if spec not in self.db.filaments:
                raise ValueError(f"base {spec!r} is neither a hex colour nor a "
                                 f"filament id")
            base = self.db.filaments[spec].rgb()
        return meas, base

    def _reset_fit(self):
        self._fit = None
        self.btn_apply.configure(state="disabled")
        self.t_fit.configure(state="normal")
        self.t_fit.delete("1.0", "end")
        self.t_fit.configure(state="disabled")

    def _do_fit(self):
        fil = self._fil()
        if fil is None:
            return
        try:
            datasets = [d for d in (self._read_wedge(self.wedge_a),
                                    self._read_wedge(self.wedge_b)) if d is not None]
        except ValueError as exc:
            messagebox.showwarning(APP, str(exc), parent=self)
            return
        if not datasets:
            messagebox.showwarning(APP, "Type or sample the measured patches first.",
                                   parent=self)
            return
        if min(len(m) for m, _ in datasets) < 3:
            messagebox.showwarning(APP, "Need at least 3 steps to fit anything "
                                        "meaningful.", parent=self)
            return

        lh = float(self.v_layer.get())
        per_channel = self.v_perchannel_fit.get()
        try:
            td, col, des, preds, (span, nsets) = calibrate.fit_td(
                datasets, lh, per_channel, fil.rgb())
        except Exception as exc:
            messagebox.showerror(APP, f"Fit failed:\n{exc}", parent=self)
            return

        t = self.t_fit
        t.configure(state="normal")
        t.delete("1.0", "end")
        for (meas, base_rgb), pred, de in zip(datasets, preds, des):
            t.insert("end", f"{fil.label()}  over base {tdcolor.to_hex(base_rgb)} "
                            f"at {lh} mm layers\n", "head")
            t.insert("end", " step  layers   measured      model     dE\n", "dim")
            for i, (m, p, d) in enumerate(zip(meas, pred, de)):
                t.insert("end",
                         f" {i+1:4d}  {i+1:6d}   {tdcolor.to_hex(m):>8}   "
                         f"{tdcolor.to_hex(tdcolor.linear_to_srgb(p)):>8}  "
                         f"{d:5.1f}\n", "warn" if d > 5 else "")
            t.insert("end", "\n")

        all_de = np.concatenate(des)
        t.insert("end", f"  td      = {np.round(td, 4).tolist()}\n")
        t.insert("end", f"  colour  = {tdcolor.to_hex(col)}   (was {fil.color})\n")
        t.insert("end", f"  fit dE  : mean {all_de.mean():.1f}, "
                        f"max {all_de.max():.1f}\n",
                 "ok" if all_de.mean() <= 5 else "warn")

        blocked = False
        if per_channel and min(len(m) for m, _ in datasets) < 6:
            t.insert("end", "\n  ! fewer than 6 steps for a 6-parameter "
                            "per-channel fit; treat this with suspicion.\n", "warn")
        if all_de.mean() > 5:
            t.insert("end", "\n  ! poor fit. Check lighting and white balance, "
                            "confirm the wedge sliced at the stated layer "
                            "height, and that the base colour is right.\n", "warn")
        if nsets < 2 and span < 25:
            blocked = True
            t.insert("end",
                     f"\n  !! This wedge only spans dE {span:.0f} end to end.\n"
                     "     td and colour are under-determined at that contrast — "
                     "the fit above\n     may look tight and still be badly "
                     "wrong. Print a second wedge over a\n     CONTRASTING base "
                     "and fill in Wedge B.\n\n"
                     "     Refusing to apply a fit from a single low-contrast "
                     "wedge.\n", "err")
        t.configure(state="disabled")

        self._fit = None if blocked else {
            "td": td, "color": tdcolor.to_hex(col), "per_channel": per_channel,
            "layer_height": lh, "steps": int(min(len(m) for m, _ in datasets)),
            "nsets": nsets,
        }
        self.btn_apply.configure(state="disabled" if blocked else "normal")
        # The report is the payoff and it sits below the inputs, so bring it
        # into view rather than leaving the window looking like nothing happened.
        self.cal_scroll.update_idletasks()
        self.cal_scroll.canvas.yview_moveto(1.0)
        self._status(
            f"fit dE mean {all_de.mean():.1f}" + ("  — not applicable" if blocked else ""),
            ERR if blocked else (OK if all_de.mean() <= 5 else WARN))

    def _apply_fit(self):
        fil = self._fil()
        if fil is None or not self._fit:
            return
        r = self._fit
        fil.color = r["color"]
        if r["per_channel"]:
            fil.td_rgb = [round(float(v), 4) for v in r["td"]]
            fil.td = round(float(np.mean(r["td"])), 4)
        else:
            fil.td = round(float(r["td"][0]), 4)
            fil.td_rgb = None
        fil.provenance = "measured"
        fil.layer_height_ref = r["layer_height"]
        fil.measured_at = date.today().isoformat()
        fil.notes = (fil.notes + " | ").lstrip(" |") + (
            f"fit from {r['steps']}-step wedge"
            + (" over two bases" if r["nsets"] > 1 else ""))
        self._load_fields()                    # reload every field from the entry
        self._refresh_row(fil.id)
        self.dirty = True
        if self.on_dirty:
            self.on_dirty(True)
        self.btn_apply.configure(state="disabled")
        self._status("applied — press Save to write it to disk", WARN)
        self.nb.select(1)                      # show what those numbers look like

    # -- library actions ---------------------------------------------------

    def new_filament(self):
        fid = self._unique_id("new-filament")
        self.db.filaments[fid] = Filament(
            id=fid, brand="Polymaker", series="PLA Pro", name="New filament",
            color="#808080", td=TD_GUESS["opaque"], finish="opaque",
            provenance="estimated")
        self.dirty = True
        self._render_list()
        self.select(fid)
        self.nb.select(0)
        self.v_name.set("")
        self._status("new entry — name it, then set its colour", WARN)

    def duplicate(self):
        fil = self._fil()
        if fil is None:
            return
        copy = deepcopy(fil)
        copy.id = self._unique_id(fil.id + "-copy")
        copy.name = (fil.name + " copy").strip()
        self.db.filaments[copy.id] = copy
        self.dirty = True
        self._render_list()
        self.select(copy.id)
        self._status(f"duplicated {fil.id}", WARN)

    def _unique_id(self, stem):
        fid, n = stem, 2
        while fid in self.db.filaments:
            fid, n = f"{stem}-{n}", n + 1
        return fid

    def delete(self):
        fil = self._fil()
        if fil is None:
            return
        if not messagebox.askyesno(
            APP, f"Delete {fil.label()} ({fil.id})?\n\nAny command line naming it "
                 f"will stop working.", parent=self):
            return
        order = sorted(self.db.filaments)
        i = order.index(fil.id)
        del self.db.filaments[fil.id]
        self.dirty = True
        order.pop(i)
        self._render_list()
        if order:
            self.select(order[min(i, len(order) - 1)])
        else:
            self.current = None
            self._set_editor_state(False)
        self._status(f"deleted {fil.id}", WARN)

    def seed(self):
        brand = simpledialog.askstring(APP, "Brand for the starter set:",
                                       initialvalue="Polymaker", parent=self)
        if brand is None:
            return
        series = simpledialog.askstring(APP, "Series:", initialvalue="PLA Pro",
                                        parent=self)
        if series is None:
            return
        n = seed_db(self.db, brand, series)
        self.dirty = self.dirty or n > 0
        self._render_list()
        self._status(f"added {n} starter entries (all estimated)",
                     OK if n else FG_DIM)

    # -- persistence -------------------------------------------------------

    def save(self):
        bad = []
        for fil in self.db.filaments.values():
            try:
                tdcolor.parse_hex(fil.color)
            except ValueError:
                bad.append(f"{fil.id}: colour {fil.color!r} is not a hex colour")
            if fil.td <= 0:
                bad.append(f"{fil.id}: td must be positive")
            if fil.td_rgb and (len(fil.td_rgb) != 3 or min(fil.td_rgb) <= 0):
                bad.append(f"{fil.id}: td_rgb must be three positive numbers")
        if bad:
            messagebox.showerror(APP, "Not saved:\n\n" + "\n".join(bad), parent=self)
            return False
        try:
            self.db.save()
        except OSError as exc:
            messagebox.showerror(APP, f"Could not write {self.db.path}:\n{exc}",
                                 parent=self)
            return False
        self.dirty = False
        if self.on_dirty:
            self.on_dirty(False)
        self._status(f"saved {len(self.db.filaments)} filaments to "
                     f"{os.path.basename(self.db.path)}", OK)
        return True

    def save_as(self):
        p = filedialog.asksaveasfilename(
            title="Save filament database", defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialfile=os.path.basename(self.db.path), parent=self)
        if not p:
            return
        self.db.path = self.db_path = p
        self.save()
        self.lbl_path.config(text=self.db_path)

    def revert(self):
        if self.dirty and not messagebox.askyesno(
            APP, "Discard unsaved edits and reload from disk?", parent=self):
            return
        self.load(self.db_path)

    def load(self, path):
        try:
            db = DB(path)
        except SystemExit as exc:
            messagebox.showerror(APP, str(exc), parent=self)
            return
        self.db, self.db_path = db, path
        self.dirty = False
        if self.on_dirty:
            self.on_dirty(False)
        self.current = None
        self._render_list()
        self.lbl_path.config(text=path)
        if self.db.filaments:
            self.select(sorted(self.db.filaments)[0])
        else:
            self._set_editor_state(False)
            self._status("empty database — New, or Database ▸ Add starter set", WARN)
        self._reset_fit()

    def open_db(self):
        if not self.confirm_discard():
            return
        p = filedialog.askopenfilename(title="Open filament database",
                                       filetypes=[("JSON", "*.json")], parent=self)
        if p:
            self.load(p)

    def confirm_discard(self) -> bool:
        """True if it is safe to throw the current state away."""
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel(
            APP, "Save changes to the filament database first?", parent=self)
        if answer is None:
            return False
        return self.save() if answer else True

    def _status(self, msg, colour=FG_DIM):
        self.lbl_status.config(text=msg, foreground=colour)
        self.lbl_path.config(text=self.db_path)


# --------------------------------------------------------------------------
# windows
# --------------------------------------------------------------------------


def _build_menu(win, ed: FilamentEditor):
    m = tk.Menu(win)
    f = tk.Menu(m, tearoff=0)
    f.add_command(label="Open database…", accelerator="Ctrl+O", command=ed.open_db)
    f.add_command(label="Save", accelerator="Ctrl+S", command=ed.save)
    f.add_command(label="Save as…", command=ed.save_as)
    f.add_command(label="Revert", command=ed.revert)
    f.add_separator()
    f.add_command(label="Close", accelerator="Ctrl+Q",
                  command=lambda: win.event_generate("<<CloseRequest>>"))
    m.add_cascade(label="File", menu=f)

    d = tk.Menu(m, tearoff=0)
    d.add_command(label="New filament", accelerator="Ctrl+N", command=ed.new_filament)
    d.add_command(label="Duplicate", command=ed.duplicate)
    d.add_command(label="Delete", command=ed.delete)
    d.add_separator()
    d.add_command(label="Add from Polymaker SKU…", command=ed._browse_skus)
    d.add_command(label="Add starter set…", command=ed.seed)
    m.add_cascade(label="Database", menu=d)

    h = tk.Menu(m, tearoff=0)
    h.add_command(label="About", command=lambda: messagebox.showinfo(
        APP,
        "Filament library\n\n"
        "Colours and optical properties for the 3mf tools.\n\n"
        "td is the thickness in mm at which transmittance falls to 1/e "
        "(36.8%): T(t) = exp(-t/td). This is NOT HueForge's TD scale — use the "
        "converter on the Details tab rather than pasting values across.\n\n"
        "An entry is only as good as its provenance. 'estimated' means nobody "
        "measured it and the colours it produces will be approximate.",
        parent=win))
    m.add_cascade(label="Help", menu=h)
    win.config(menu=m)
    win.bind("<Control-s>", lambda e: ed.save())
    win.bind("<Control-o>", lambda e: ed.open_db())
    win.bind("<Control-n>", lambda e: ed.new_filament())


class App(tk.Tk):
    def __init__(self, db_path="filaments.json"):
        super().__init__()
        self.title(f"{APP} — {db_path}")
        self.geometry("1280x900")
        self.minsize(1040, 720)
        self.configure(bg=BG)
        guikit.apply_theme(self)

        self.ed = FilamentEditor(self, db_path, on_dirty=self._on_dirty)
        self.ed.pack(fill="both", expand=True)
        _build_menu(self, self.ed)
        self.bind("<<CloseRequest>>", lambda e: self._close())
        self.bind("<Control-q>", lambda e: self._close())
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _on_dirty(self, dirty):
        dot = "● " if dirty else ""
        self.title(f"{dot}{APP} — {self.ed.db_path}")

    def _close(self):
        if self.ed.confirm_discard():
            self.destroy()


def open_window(master, db_path="filaments.json", on_close=None):
    """Open the editor over an existing app (used by stackforge_gui)."""
    win = tk.Toplevel(master)
    win.title(f"{APP} — {db_path}")
    win.geometry("1240x880")
    win.configure(bg=BG)
    ed = FilamentEditor(win, db_path)
    ed.pack(fill="both", expand=True)
    _build_menu(win, ed)

    def close():
        if not ed.confirm_discard():
            return
        win.destroy()
        if on_close:
            on_close(ed.db_path)

    win.bind("<<CloseRequest>>", lambda e: close())
    win.protocol("WM_DELETE_WINDOW", close)
    return win


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "FILAMENT_DB", "filaments.json")
    App(path).mainloop()


if __name__ == "__main__":
    main()
