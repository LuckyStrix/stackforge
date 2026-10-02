"""FilamentsTab: FilamentEditor as a panel of the host app."""
from __future__ import annotations

from tkinter import messagebox, ttk

from tdforge.gui.tabs.filaments import APP
from tdforge.gui.tabs.filaments.editor import FilamentEditor

ABOUT = (
    "Filament library\n\n"
    "Colours and optical properties for the 3mf tools.\n\n"
    "td is the thickness in mm at which transmittance falls to 1/e "
    "(36.8%): T(t) = exp(-t/td). This is NOT HueForge's TD scale — use the "
    "converter on the Details tab rather than pasting values across.\n\n"
    "An entry is only as good as its provenance. 'estimated' means nobody "
    "measured it and the colours it produces will be approximate.")


class FilamentsTab(ttk.Frame):
    title = "Filaments"

    def __init__(self, master, project=None, on_dirty=None):
        super().__init__(master)
        self.project = project
        db = project.get("db") if project else "filaments.json"
        self.ed = FilamentEditor(self, db, on_dirty=on_dirty)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(6, 0))
        for text, cmd in (("Open…", self.ed.open_db), ("Save", self.ed.save),
                          ("Save as…", self.ed.save_as), ("Revert", self.ed.revert),
                          ("New", self.ed.new_filament), ("Duplicate", self.ed.duplicate),
                          ("Delete", self.ed.delete),
                          ("Add from Polymaker SKU…", self.ed._browse_skus),
                          ("Starter set…", self.ed.seed)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="About", command=lambda: messagebox.showinfo(
            APP, ABOUT, parent=self)).pack(side="right")
        self.ed.pack(fill="both", expand=True)

    def shortcuts(self) -> dict:
        return {"<Control-s>": self.ed.save, "<Control-o>": self.ed.open_db,
                "<Control-n>": self.ed.new_filament}

    def confirm_close(self) -> bool:
        return self.ed.confirm_discard()

    @property
    def dirty(self) -> bool:
        return self.ed.dirty

    def reload_db(self, path=None):
        """Re-read the database (after another tab changed it); keeps unsaved edits safe."""
        path = path or self.ed.db_path
        if self.ed.dirty and not self.ed.confirm_discard():
            return
        self.ed.load(path)
