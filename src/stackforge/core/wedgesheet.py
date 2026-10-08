"""Wedge sheets and readings: what calibrate and measure hand each other, as plain JSON.

`calibrate wedge` writes a sheet next to the 3MF: the grid, the step count and every strip
(one row of steps over one base) in print order. `measure measure-wedge --sheet` walks those
strips and writes a readings file: the sheet plus, per strip, the bare-base reading and the
step hexes. Calibrate loads that to fill the fit. The files travel between machines, so they
carry labels and colours, not just ids: the measuring machine may have no filament library.
"""
from __future__ import annotations

import json
import os

SHEET_KIND = "stackforge-wedge-sheet"
READINGS_KIND = "stackforge-wedge-readings"
VERSION = 1


def _stem(path: str) -> str:
    for ext in (".sheet.json", ".readings.json", ".3mf", ".json"):
        if path.lower().endswith(ext):
            return path[: -len(ext)]
    return path


def sheet_path_for(model_path: str) -> str:
    """wedge_teal.3mf -> wedge_teal.sheet.json"""
    return _stem(model_path) + ".sheet.json"


def readings_path_for(sheet_path: str) -> str:
    """wedge_teal.sheet.json -> wedge_teal.readings.json"""
    return _stem(sheet_path) + ".readings.json"


def filament_ref(fil) -> dict:
    return {"id": fil.id, "label": fil.label(), "color": fil.color}


def strip_title(strip: dict) -> str:
    return (f"Wedge {strip['wedge']} ({strip['where']}): {strip['filament']['label']} "
            f"over {strip['base']['label']}")


def write(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)


def _load(path: str, kind: str, what: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{os.path.basename(path)}: not a readable {what} ({exc})")
    if not isinstance(data, dict) or data.get("kind") != kind:
        found = data.get("kind") if isinstance(data, dict) else None
        hint = (" (that is a readings file: load it in Calibrate)" if found == READINGS_KIND else
                " (that is a wedge sheet: give it to Measure first)" if found == SHEET_KIND else
                "")
        raise ValueError(f"{os.path.basename(path)} is not a {what}{hint}")
    if data.get("version", 0) > VERSION:
        raise ValueError(f"{os.path.basename(path)} was written by a newer stackforge")
    if not data.get("strips"):
        raise ValueError(f"{os.path.basename(path)} lists no wedges")
    return data


def load_sheet(path: str) -> dict:
    return _load(path, SHEET_KIND, "wedge sheet")


def load_readings(path: str) -> dict:
    data = _load(path, READINGS_KIND, "wedge readings file")
    for s in data["strips"]:
        if not s.get("hex"):
            raise ValueError(f"{os.path.basename(path)}: {strip_title(s)} has no readings")
    return data


def strips_for(readings: dict, filament_id: str | None = None) -> list[dict]:
    """The strips of one filament: `filament_id`'s, else the first filament's. Front first."""
    strips = readings["strips"]
    fid = filament_id if any(s["filament"]["id"] == filament_id for s in strips) else \
        strips[0]["filament"]["id"]
    return [s for s in strips if s["filament"]["id"] == fid]
