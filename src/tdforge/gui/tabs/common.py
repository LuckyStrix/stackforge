"""Shared plumbing for tabs made of generated forms."""
from __future__ import annotations

from tkinter import ttk

from tdforge.gui.argform.spec import introspect
from tdforge.gui.run.panel import ToolPanel


def leaf_commands(spec):
    """Command paths of every runnable command: () for a tool without subcommands."""
    return [path for path, sp in spec.walk() if not sp.subs]


class ToolTabs(ttk.Notebook):
    """A notebook with one ToolPanel per command.

    `items` is a list of (label, tool, parser builder, command path).
    """

    def __init__(self, master, items, project=None, presets=None, terminal=None, on_done=None):
        super().__init__(master)
        self.panels: dict[str, ToolPanel] = {}
        specs = {}
        for label, tool, builder, command in items:
            if tool not in specs:
                specs[tool] = introspect(builder(), tool)
            panel = ToolPanel(self, specs[tool], tool, command, project, presets=presets,
                              terminal=terminal, on_done=on_done)
            self.panels[label] = panel
            self.add(panel, text=label)

    @classmethod
    def for_tool(cls, master, tool, builder, **kw):
        spec = introspect(builder(), tool)
        paths = leaf_commands(spec)
        return cls(master, [(" ".join(p) or tool, tool, builder, p) for p in paths], **kw)
