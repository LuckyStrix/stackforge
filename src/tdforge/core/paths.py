"""Where the shipped data files (filament database, Polymaker catalogue) live.

A copy in the working directory wins, so a project can carry its own; otherwise the copy
packaged with tdforge is used. Environment overrides (`FILAMENT_DB`, `POLYMAKER_CATALOG`) are
applied by the callers, before this.
"""
from __future__ import annotations

import os

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def packaged(name: str) -> str:
    """Absolute path of the copy shipped inside the package."""
    return os.path.join(DATA_DIR, name)


def data_path(name: str) -> str:
    """`./name` if it exists, else the packaged copy. Returns a path either way."""
    if os.path.exists(name):
        return name
    pk = packaged(name)
    return pk if os.path.exists(pk) else name
