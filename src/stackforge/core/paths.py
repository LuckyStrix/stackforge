"""Where the shipped data files (filament database, Polymaker catalogue) live.

A copy in the working directory wins, so a project can carry its own. The filament database
is edited, so it is never written inside the package: the first time it is needed it is
copied to the user's config folder ($XDG_CONFIG_HOME/stackforge, default ~/.config/stackforge)
and used from there (`user_data_path`). Environment overrides (`FILAMENT_DB`,
`POLYMAKER_CATALOG`) are applied by the callers, before this.
"""
from __future__ import annotations

import os
import shutil

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def packaged(name: str) -> str:
    """Absolute path of the copy shipped inside the package."""
    return os.path.join(DATA_DIR, name)


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "stackforge")


def user_data_path(name: str) -> str:
    """`./name` if it exists, else the user's own copy, made from the packaged one if missing.

    Edits (new filaments, calibration fits) then land in the user's folder, not in the
    package, where a reinstall or `git pull` would overwrite or conflict with them. If the
    config folder cannot be written, the packaged copy is used as before.
    """
    if os.path.exists(name):
        return name
    mine = os.path.join(config_dir(), name)
    if not os.path.exists(mine):
        try:
            os.makedirs(os.path.dirname(mine), exist_ok=True)
            shutil.copy2(packaged(name), mine)
        except OSError:
            return data_path(name)
    return mine


def data_path(name: str) -> str:
    """`./name` if it exists, else the packaged copy. Returns a path either way."""
    if os.path.exists(name):
        return name
    pk = packaged(name)
    return pk if os.path.exists(pk) else name
