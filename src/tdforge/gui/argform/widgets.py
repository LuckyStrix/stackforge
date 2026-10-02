"""FieldSpec -> tk widget. Each builder returns a FieldWidget with get/set/disable.

Semantic pickers (filament, palette) fall back to an entry for now; they plug in here by
kind once gui/pickers exists.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, ttk

from tdforge.gui import theme
from tdforge.gui.argform.spec import FieldSpec

IMAGE_TYPES = [("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"), ("All files", "*.*")]
MODEL_TYPES = [("3MF", "*.3mf"), ("All files", "*.*")]


class FieldWidget:
    """Uniform handle over whatever widget a kind needs. `get()` returns raw text (or bool)."""

    def __init__(self, frame, getter, setter, controls=()):
        self.frame = frame
        self._get, self._set = getter, setter
        self._controls = list(controls)

    def get(self):
        return self._get()

    def set(self, v):
        self._set(v)

    def enable(self, on: bool):
        for c in self._controls:
            try:
                c.state(["!disabled"] if on else ["disabled"])
            except (AttributeError, tk.TclError):
                c.config(state="normal" if on else "disabled")

    def readonly(self, on: bool):
        for c in self._controls:
            try:
                c.config(state="readonly" if on else "normal")
            except tk.TclError:
                pass


def _text(v):
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return " ".join(str(x) for x in v)
    return str(v)


def _entry_widget(parent, f: FieldSpec, on_change, browse=None, width=28):
    var = tk.StringVar()
    var.trace_add("write", lambda *_: on_change())
    box = ttk.Frame(parent)
    ent = ttk.Entry(box, textvariable=var, width=width)
    ent.pack(side="left", fill="x", expand=True)
    controls = [ent]
    if browse:
        b = ttk.Button(box, text="Browse…", width=8, command=lambda: browse(var))
        b.pack(side="left", padx=(4, 0))
        controls.append(b)
    w = FieldWidget(box, lambda: var.get().strip(), lambda v: var.set(_text(v)), controls)
    w.var = var
    return w


def _open(types, title):
    def go(var):
        p = filedialog.askopenfilename(title=title, filetypes=types)
        if p:
            var.set(p)
    return go


def _save(var):
    p = filedialog.asksaveasfilename()
    if p:
        var.set(p)


def _dir(var):
    p = filedialog.askdirectory()
    if p:
        var.set(p)


def build(parent, f: FieldSpec, kind: str, on_change) -> FieldWidget:
    """Build the widget for `kind` (see overrides.KINDS; project kinds arrive as 'entry')."""
    if kind == "check":
        var = tk.BooleanVar(value=bool(f.default))
        var.trace_add("write", lambda *_: on_change())
        cb = ttk.Checkbutton(parent, variable=var)
        return FieldWidget(cb, lambda: var.get(), lambda v: var.set(bool(v)), [cb])
    if kind == "combo":
        var = tk.StringVar()
        var.trace_add("write", lambda *_: on_change())
        vals = ([""] if not f.required else []) + [str(c) for c in f.choices]
        cb = ttk.Combobox(parent, textvariable=var, values=vals, state="readonly", width=26)
        w = FieldWidget(cb, lambda: var.get(), lambda v: var.set(_text(v)), [cb])
        return w
    if kind in ("int", "float"):
        w = _entry_widget(parent, f, on_change, width=12)
        return w
    if kind == "repeat":
        txt = tk.Text(parent, height=3, width=30, bg=theme.BG3, fg=theme.FG,
                      insertbackground=theme.FG, relief="flat", borderwidth=4,
                      highlightthickness=0, font=("TkDefaultFont", 9))
        txt.bind("<<Modified>>", lambda e: (txt.edit_modified(False), on_change()))

        def get():
            return [ln.strip() for ln in txt.get("1.0", "end").splitlines() if ln.strip()]

        def set_(v):
            txt.delete("1.0", "end")
            txt.insert("1.0", "\n".join(str(x) for x in (v or [])))
        return FieldWidget(txt, get, set_, [txt])
    if kind in ("file_in", "model_3mf"):
        return _entry_widget(parent, f, on_change,
                             _open(MODEL_TYPES if kind == "model_3mf" else [("All files", "*.*")],
                                   f.flag))
    if kind == "image":
        return _entry_widget(parent, f, on_change, _open(IMAGE_TYPES, "Image"))
    if kind == "file_out":
        return _entry_widget(parent, f, on_change, _dir if "dir" in f.dest else _save)
    # entry, list, hex_list, filament_id(s), project:* -> text entry
    return _entry_widget(parent, f, on_change)
