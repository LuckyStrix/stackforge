"""Polymaker catalogue browser."""

from __future__ import annotations

import threading

import tkinter as tk
from tkinter import messagebox, ttk

from tdforge.tools import polymaker
from tdforge.core import tdcolor
from tdforge.core.filamentdb import hueforge_td
from tdforge.gui.theme import BG, ERR, FG, FG_DIM, OK, WARN, ScrollFrame, swatch_image
from tdforge.gui.tabs.filaments import APP


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
