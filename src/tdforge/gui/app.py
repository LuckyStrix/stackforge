"""tdforge-gui: one window for every tool. Project bar on top, one tab per area."""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

from tdforge.gui import theme
from tdforge.gui.project import Project
from tdforge.gui.projectbar import ProjectBar
from tdforge.gui.settings import PresetStore, Settings

APP = "tdforge"


class HostApp(tk.Tk):
    """Hosts tabs. A tab is a ttk.Frame with a `title` and, optionally:
    shortcuts() -> {sequence: fn}, build_menu(root) -> tk.Menu, confirm_close() -> bool,
    on_show().
    """

    def __init__(self, image_path=None):
        super().__init__()
        self.settings = Settings()
        self.project = Project(self.settings)
        self.presets = PresetStore()
        self.title(APP)
        self.geometry(self.settings.get("geometry") or "1500x950")
        self.minsize(1180, 760)
        self.configure(bg=theme.BG)
        theme.apply_theme(self)

        self.bar = ProjectBar(self, self.project)
        self.bar.pack(fill="x")
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        self.tabs: dict[str, ttk.Frame] = {}
        self._menus: dict = {}
        self._bound: set = set()
        self._db = self.project.get("db")

        self._add_tabs(image_path)
        self.project.subscribe(self._project_changed)
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed())
        last = self.settings.get("last_tab")
        if last in self.tabs:
            self.show_tab(last)
        else:
            self._tab_changed()
        self.bind("<<CloseRequest>>", lambda e: self._close())
        self.bind("<Control-q>", lambda e: self._close())
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ---- tabs --------------------------------------------------------------------------
    def _add_tabs(self, image_path):
        from tdforge.gui.tabs.filaments.tab import FilamentsTab
        from tdforge.gui.tabs.plaque.tab import PlaqueTab
        self.add_tab(PlaqueTab(self.nb, image_path, self.project.get("db"), host=self))
        self.add_tab(FilamentsTab(self.nb, self.project))

    def add_tab(self, tab):
        self.tabs[tab.title] = tab
        self.nb.add(tab, text=tab.title)
        for seq in getattr(tab, "shortcuts", lambda: {})():
            if seq not in self._bound:
                self._bound.add(seq)
                self.bind(seq, lambda e, s=seq: self._dispatch(s))

    def show_tab(self, title: str):
        self.nb.select(self.tabs[title])

    def current(self):
        sel = self.nb.select()
        return self.nametowidget(sel) if sel else None

    def _dispatch(self, seq):
        tab = self.current()
        fn = getattr(tab, "shortcuts", lambda: {})().get(seq)
        if fn:
            fn()
            return "break"

    def _tab_changed(self):
        tab = self.current()
        if tab is None:
            return
        if tab not in self._menus:
            mk = getattr(tab, "build_menu", None)
            self._menus[tab] = mk(self) if mk else self._default_menu()
        self.config(menu=self._menus[tab])
        self.set_title(None)
        on_show = getattr(tab, "on_show", None)
        if on_show:
            on_show()

    def _default_menu(self):
        m = tk.Menu(self)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="Quit", accelerator="Ctrl+Q", command=self._close)
        m.add_cascade(label="File", menu=f)
        return m

    def set_title(self, text):
        self.title(text or APP)

    # ---- project -----------------------------------------------------------------------
    def _project_changed(self):
        db = self.project.get("db")
        if db == self._db:
            return
        self._db = db
        plaque, fil = self.tabs.get("Plaque"), self.tabs.get("Filaments")
        if fil is not None:
            fil.reload_db(db)
        if plaque is not None:
            plaque.db_path = db
            plaque._reload_filaments()

    # ---- lifecycle ---------------------------------------------------------------------
    def _close(self):
        for tab in self.tabs.values():
            cc = getattr(tab, "confirm_close", None)
            if cc and not cc():
                return
        self.settings.set("geometry", self.geometry())
        cur = self.current()
        if cur is not None:
            self.settings.set("last_tab", cur.title)
        self.destroy()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    image = argv[0] if argv and not argv[0].startswith("-") else None
    HostApp(image).mainloop()


if __name__ == "__main__":
    main()
