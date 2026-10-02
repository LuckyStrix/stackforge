"""PresetBar: Save / Load / Delete for one form, over a PresetStore."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from tdforge.gui.argform.form import CommandForm
from tdforge.gui.settings import PresetStore


class PresetBar(ttk.Frame):
    def __init__(self, master, form: CommandForm, store: PresetStore):
        super().__init__(master)
        self.form, self.store = form, store
        self.var = tk.StringVar()
        ttk.Label(self, text="Preset").pack(side="left", padx=(0, 4))
        self.combo = ttk.Combobox(self, textvariable=self.var, width=18, state="readonly")
        self.combo.pack(side="left")
        ttk.Button(self, text="Load", width=6, command=self.load).pack(side="left", padx=(4, 0))
        ttk.Button(self, text="Save…", width=6, command=self.save).pack(side="left", padx=(4, 0))
        ttk.Button(self, text="Delete", width=6, command=self.delete).pack(side="left", padx=(4, 0))
        self.refresh()

    def refresh(self):
        names = self.store.names(self.form.tool, self.form.command)
        self.combo.config(values=names)
        if self.var.get() not in names:
            self.var.set("")

    def save(self):
        name = simpledialog.askstring("Save preset", "Preset name:", initialvalue=self.var.get(),
                                      parent=self)
        if not name:
            return
        try:
            self.store.save(self.form.tool, self.form.command, name, self.form.preset_values())
        except ValueError as e:
            messagebox.showerror("Preset", str(e), parent=self)
            return
        self.refresh()
        self.var.set(self.store.clean(name))

    def load(self):
        if not self.var.get():
            return
        values = self.store.load(self.form.tool, self.form.command, self.var.get())
        skipped = self.form.set_values(values)
        if skipped:
            messagebox.showinfo("Preset", "Skipped keys that no longer exist: " + ", ".join(skipped),
                                parent=self)

    def delete(self):
        if self.var.get():
            self.store.delete(self.form.tool, self.form.command, self.var.get())
            self.refresh()
