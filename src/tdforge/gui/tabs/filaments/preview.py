"""Optical preview: what N layers of a filament look like, and the photo colour picker."""

from __future__ import annotations

import numpy as np
import tkinter as tk
from PIL import Image
from PIL import ImageTk
from tkinter import ttk

from tdforge.core import tdcolor
from tdforge.core.filamentdb import Filament
from tdforge.gui.theme import ACCENT, BG, BG3, ERR, FG, FG_DIM, OK, WARN, readable_on
from tdforge.gui.tabs.filaments import PREVIEW_BASES


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

        self.src = np.asarray(tdcolor.open_image(path), dtype=np.float64)
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
