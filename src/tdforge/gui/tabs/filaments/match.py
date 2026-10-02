"""Match-by-eye tab: fit a filament's td by comparing a printed wedge against the model."""

from __future__ import annotations

from datetime import date

import numpy as np
import tkinter as tk
from tkinter import ttk

from tdforge.core import tdcolor
from tdforge.gui.theme import ACCENT, BG, FG, FG_DIM, LINE, OK, WARN, spin


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
