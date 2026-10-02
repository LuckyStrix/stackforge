#!/usr/bin/env python3
"""Shared tkinter chrome for the desktop front ends.

The palette is deliberately dark and desaturated: every window here shows
filament swatches, and a neutral surround is the only way to judge a colour
against another colour. Nothing in this module knows about filaments, 3MFs or
colour science -- it is widgets and theme only.
"""

from __future__ import annotations

import numpy as np
import tkinter as tk
from PIL import Image, ImageTk
from tkinter import ttk

BG = "#1e1e22"
BG2 = "#26262c"
BG3 = "#2f2f37"
FG = "#e8e8ee"
FG_DIM = "#9a9aa6"
ACCENT = "#5b8dd6"
WARN = "#d6a25b"
ERR = "#d65b5b"
OK = "#6dbd7a"
LINE = "#3a3a44"


def apply_theme(root: tk.Misc):
    """Paint ttk's clam theme into the palette above."""
    s = ttk.Style(root)
    try:
        s.theme_use("clam")
    except tk.TclError:
        pass
    s.configure(".", background=BG, foreground=FG, fieldbackground=BG3,
                bordercolor=LINE, lightcolor=BG2, darkcolor=BG)
    s.configure("TFrame", background=BG)
    s.configure("TLabel", background=BG, foreground=FG)
    s.configure("Hint.TLabel", foreground=FG_DIM, font=("TkDefaultFont", 8))
    s.configure("Head.TLabel", font=("TkDefaultFont", 11, "bold"))
    s.configure("Stat.TLabel", foreground=FG_DIM, font=("TkFixedFont", 9))
    s.configure("Warn.TLabel", foreground=WARN, font=("TkDefaultFont", 8))
    s.configure("Err.TLabel", foreground=ERR, font=("TkDefaultFont", 8))
    s.configure("OK.TLabel", foreground=OK, font=("TkDefaultFont", 8))
    s.configure("TLabelframe", background=BG, bordercolor=LINE)
    s.configure("TLabelframe.Label", background=BG, foreground=ACCENT,
                font=("TkDefaultFont", 9, "bold"))
    s.configure("TButton", background=BG3, foreground=FG, borderwidth=0, padding=6)
    s.map("TButton", background=[("active", "#3d3d48"), ("disabled", BG2)],
          foreground=[("disabled", "#5a5a64")])
    s.configure("Go.TButton", background=ACCENT, foreground="#0d1420",
                font=("TkDefaultFont", 10, "bold"), padding=8)
    s.map("Go.TButton", background=[("active", "#6fa0e8"), ("disabled", BG2)])
    s.configure("Danger.TButton", background=BG3, foreground=ERR, borderwidth=0)
    s.map("Danger.TButton", background=[("active", "#4a2f33")])
    s.configure("TNotebook", background=BG, borderwidth=0)
    s.configure("TNotebook.Tab", background=BG2, foreground=FG_DIM, padding=(14, 7))
    s.map("TNotebook.Tab", background=[("selected", BG3)], foreground=[("selected", FG)])
    s.configure("TCheckbutton", background=BG, foreground=FG)
    s.map("TCheckbutton", background=[("active", BG)])
    s.configure("TRadiobutton", background=BG, foreground=FG)
    # Comboboxes and spinboxes need their field colours mapped per-state as well
    # as configured, or the readonly/selected states fall back to the platform
    # default and render light-on-light against this theme.
    s.configure("TCombobox", fieldbackground=BG3, background=BG3, foreground=FG,
                arrowcolor=FG, selectbackground=BG3, selectforeground=FG,
                borderwidth=0, padding=4)
    s.map("TCombobox",
          fieldbackground=[("readonly", BG3), ("disabled", BG2)],
          background=[("readonly", BG3), ("active", BG3)],
          foreground=[("readonly", FG), ("disabled", "#5a5a64")],
          selectbackground=[("readonly", BG3)],
          selectforeground=[("readonly", FG)],
          arrowcolor=[("active", ACCENT)])
    s.configure("TSpinbox", fieldbackground=BG3, background=BG3, foreground=FG,
                arrowcolor=FG, borderwidth=0, padding=4, insertcolor=FG)
    s.map("TSpinbox",
          fieldbackground=[("disabled", BG2), ("readonly", BG3)],
          foreground=[("disabled", "#5a5a64")],
          arrowcolor=[("active", ACCENT)])
    s.configure("TEntry", fieldbackground=BG3, foreground=FG, insertcolor=FG,
                borderwidth=0, padding=4)
    s.map("TEntry", fieldbackground=[("disabled", BG2)])
    s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=BG2,
                borderwidth=0)
    s.configure("TSeparator", background=LINE)

    # The combobox dropdown is a classic tk Listbox, reachable only through the
    # option database.
    root.option_add("*TCombobox*Listbox.background", BG3)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#0d1420")
    root.option_add("*TCombobox*Listbox.borderWidth", 0)
    return s


def spin(parent, var, frm, to, inc, command=None, width=10):
    """A number box.

    Classic tk.Spinbox rather than ttk: clam's TSpinbox ignores
    fieldbackground, so a themed one renders light-on-light against this theme.
    """
    return tk.Spinbox(
        parent, textvariable=var, from_=frm, to=to, increment=inc, width=width,
        command=command, bg=BG3, fg=FG, insertbackground=FG,
        buttonbackground=BG3, readonlybackground=BG3, highlightthickness=0,
        relief="flat", borderwidth=4, font=("TkDefaultFont", 9),
        selectbackground=ACCENT, selectforeground="#0d1420",
    )


def scrolled_text(parent, height=14, wrap="none", **kw):
    """A tk.Text inside a frame with scrollbars; returns the Text.

    The Text's pack/grid/place are redirected to the frame, so callers lay it out as if it
    were the bare widget. The horizontal bar is only added for wrap="none".
    """
    frame = ttk.Frame(parent)
    t = tk.Text(frame, height=height, wrap=wrap, **kw)
    ys = ttk.Scrollbar(frame, orient="vertical", command=t.yview)
    t.configure(yscrollcommand=ys.set)
    ys.pack(side="right", fill="y")
    if wrap == "none":
        xs = ttk.Scrollbar(frame, orient="horizontal", command=t.xview)
        t.configure(xscrollcommand=xs.set)
        xs.pack(side="bottom", fill="x")
    t.pack(side="left", fill="both", expand=True)
    t.pack, t.grid, t.place = frame.pack, frame.grid, frame.place
    t.pack_forget, t.grid_forget, t.grid_remove = frame.pack_forget, frame.grid_forget, frame.grid_remove
    t.frame = frame
    return t


def text_view(parent, height=14, **kw):
    """A read-only monospace pane for reports."""
    t = scrolled_text(parent, height=height, bg=BG2, fg=FG, insertbackground=FG,
                      relief="flat", borderwidth=0, padx=10, pady=8,
                      font=("TkFixedFont", 9), **kw)
    t.tag_configure("dim", foreground=FG_DIM)
    t.tag_configure("warn", foreground=WARN)
    t.tag_configure("err", foreground=ERR)
    t.tag_configure("ok", foreground=OK)
    t.tag_configure("head", foreground=ACCENT)
    return t


def readable_on(rgb) -> str:
    """Black or white text, whichever survives on this background."""
    r, g, b = (float(v) for v in rgb)
    return "#101014" if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else "#f0f0f4"


def swatch_image(rgb, w=22, h=14, border=LINE):
    im = Image.new("RGB", (w, h), tuple(int(v) for v in rgb))
    d = im.load()
    bc = tuple(int(border[i:i + 2], 16) for i in (1, 3, 5))
    for x in range(w):
        d[x, 0] = d[x, h - 1] = bc
    for y in range(h):
        d[0, y] = d[w - 1, y] = bc
    return ImageTk.PhotoImage(im)


class ImageView(ttk.Frame):
    """A canvas that keeps one image scaled to fit, without distorting it."""

    def __init__(self, master, placeholder="nothing yet"):
        super().__init__(master)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._src: Image.Image | None = None
        self._tk = None
        self._placeholder = placeholder
        self.canvas.bind("<Configure>", lambda e: self._redraw())

    def set_image(self, arr_or_img):
        if arr_or_img is None:
            self._src = None
        elif isinstance(arr_or_img, np.ndarray):
            self._src = Image.fromarray(arr_or_img.astype(np.uint8))
        else:
            self._src = arr_or_img
        self._redraw()

    def _redraw(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 4 or h < 4:
            return
        if self._src is None:
            c.create_text(w // 2, h // 2, text=self._placeholder, fill=FG_DIM,
                          font=("TkDefaultFont", 11))
            return
        iw, ih = self._src.size
        scale = min((w - 16) / iw, (h - 16) / ih)
        nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
        resample = Image.NEAREST if scale > 2 else Image.LANCZOS
        self._tk = ImageTk.PhotoImage(self._src.resize((nw, nh), resample))
        c.create_image(w // 2, h // 2, image=self._tk)


class Section(ttk.LabelFrame):
    """A titled block of labelled controls on a two-column grid."""

    def __init__(self, master, title, padding=(10, 8)):
        super().__init__(master, text=title, padding=padding)
        self.columnconfigure(1, weight=1)
        self._row = 0

    def field(self, label, widget, hint=None):
        ttk.Label(self, text=label).grid(row=self._row, column=0, sticky="w", pady=3)
        widget.grid(row=self._row, column=1, sticky="ew", padx=(10, 0), pady=3)
        self._row += 1
        if hint:
            ttk.Label(self, text=hint, style="Hint.TLabel", wraplength=280).grid(
                row=self._row, column=0, columnspan=2, sticky="w", pady=(0, 4))
            self._row += 1
        return widget

    def row(self, widget, span=2):
        widget.grid(row=self._row, column=0, columnspan=span, sticky="ew", pady=3)
        self._row += 1
        return widget

    def note(self, text, style="Hint.TLabel", wraplength=280):
        lbl = ttk.Label(self, text=text, style=style, wraplength=wraplength,
                        justify="left")
        return self.row(lbl)


class CollapsibleSection(ttk.Frame):
    """A Section whose body can be folded away; `.body` is the Section to fill."""

    def __init__(self, master, title, description="", collapsed=True):
        super().__init__(master)
        self._title = title
        self._btn = ttk.Button(self, command=self.toggle, style="Toolbutton")
        self._btn.pack(fill="x")
        self.body = Section(self, "")
        if description:
            self.body.note(description)
        self._open = not collapsed
        self._sync()

    def toggle(self):
        self._open = not self._open
        self._sync()

    def _sync(self):
        self._btn.config(text=("\u25be " if self._open else "\u25b8 ") + self._title)
        if self._open:
            self.body.pack(fill="x", pady=(2, 6))
        else:
            self.body.pack_forget()


class ScrollFrame(ttk.Frame):
    """A vertically scrolling container. Put children in `.inner`."""

    def __init__(self, master, width=320):
        super().__init__(master)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, width=width)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind(
            "<Configure>", lambda e: self.canvas.itemconfig(self._win, width=e.width))
        # bind_all so the wheel works over the child widgets too; add="+" so
        # several ScrollFrames in one window do not evict each other.
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.bind_all(seq, self._wheel, add="+")

    def _wheel(self, e):
        w = e.widget.winfo_containing(e.x_root, e.y_root)
        while w is not None:
            if w is self.canvas:
                num = getattr(e, "num", 0)
                step = 1 if num == 5 else -1 if num == 4 else (-1 if e.delta > 0 else 1)
                self.canvas.yview_scroll(step, "units")
                return
            w = getattr(w, "master", None)
