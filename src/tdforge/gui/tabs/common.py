"""Shared plumbing for tabs made of generated forms."""
from __future__ import annotations

from PySide6.QtWidgets import QTabWidget

from tdforge.gui.argform.spec import introspect
from tdforge.gui.panel import ToolPanel


def leaf_commands(spec):
    """Command paths of every runnable command: () for a tool without subcommands."""
    return [path for path, sp in spec.walk() if not sp.subs]


class ToolTabs(QTabWidget):
    """A tab widget with one ToolPanel per command.

    `items` is a list of (label, tool, parser builder, command path).
    """

    def __init__(self, items, project=None, presets=None, terminal=None, on_done=None, parent=None):
        super().__init__(parent)
        self.panels: dict[str, ToolPanel] = {}
        specs = {}
        for label, tool, builder, command in items:
            if tool not in specs:
                specs[tool] = introspect(builder(), tool)
            panel = ToolPanel(specs[tool], tool, command, project, presets=presets,
                              terminal=terminal, on_done=on_done)
            self.panels[label] = panel
            self.addTab(panel, label)

    @classmethod
    def for_tool(cls, tool, builder, **kw):
        spec = introspect(builder(), tool)
        return cls([(" ".join(p) or tool, tool, builder, p) for p in leaf_commands(spec)], **kw)

    def select(self, panel):
        self.setCurrentWidget(panel)
