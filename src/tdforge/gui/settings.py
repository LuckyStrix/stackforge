"""Global settings and per-tool presets (JSON on disk). No tkinter.

Settings live in $XDG_CONFIG_HOME/tdforge/settings.json (default ~/.config/tdforge/). A
missing or corrupt file falls back to defaults and is never fatal. Presets are one JSON per
name under presets/<tool>[/<cmd>]/<name>.json holding {dest: value}.
"""
from __future__ import annotations

import json
import os
import re

DEFAULTS = {
    "db": "", "template": "", "catalog": "", "flavor": "orca", "part_type": "modifier",
    "layer_override": False, "layer_height": "", "first_layer": "",
    "geometry": "", "last_tab": "", "recent": [],
}
MAX_RECENT = 12


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "tdforge")


class Settings:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(config_dir(), "settings.json")
        self.data = dict(DEFAULTS)
        self.data["recent"] = []
        self.load()

    def load(self):
        try:
            with open(self.path) as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in DEFAULTS and isinstance(v, type(DEFAULTS[k])):
                    self.data[k] = v

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.data, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass            # settings are a convenience; never fatal

    def get(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def set(self, key, value):
        if key not in DEFAULTS:
            raise KeyError(key)
        if self.data.get(key) != value:
            self.data[key] = value
            self.save()

    def add_recent(self, path: str):
        rec = [p for p in self.data["recent"] if p != path]
        self.set("recent", ([path] + rec)[:MAX_RECENT])


_BAD = re.compile(r"[^\w .+-]")


class PresetStore:
    def __init__(self, root: str | None = None):
        self.root = root or os.path.join(config_dir(), "presets")

    def _dir(self, tool, command=()):
        return os.path.join(self.root, tool, *command)

    @staticmethod
    def clean(name: str) -> str:
        name = _BAD.sub("_", name.strip()).strip(". ")
        if not name:
            raise ValueError("preset needs a name")
        return name

    def names(self, tool, command=()) -> list:
        try:
            return sorted(f[:-5] for f in os.listdir(self._dir(tool, command)) if f.endswith(".json"))
        except OSError:
            return []

    def save(self, tool, command, name, values: dict):
        d = self._dir(tool, command)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, self.clean(name) + ".json"), "w") as fh:
            json.dump(values, fh, indent=2, sort_keys=True)

    def load(self, tool, command, name) -> dict:
        with open(os.path.join(self._dir(tool, command), self.clean(name) + ".json")) as fh:
            return json.load(fh)

    def delete(self, tool, command, name):
        try:
            os.remove(os.path.join(self._dir(tool, command), self.clean(name) + ".json"))
        except OSError:
            pass
