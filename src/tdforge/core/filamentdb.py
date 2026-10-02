#!/usr/bin/env python3
"""Filament database -- colors and optical properties, grown over time.

Storage is one plain JSON file (default ./filaments.json) so it diffs cleanly
in git and you can hand-edit it. Every entry records where its numbers came
from, so estimated placeholders never get mistaken for measured values.

TRANSMISSION DISTANCE (td)
    Defined here as the thickness in millimetres at which transmittance falls
    to 1/e (36.8%). Physically: T(t) = exp(-t / td).

    This is NOT the same scale as HueForge's TD, which is closer to "thickness
    at which the filament reads as opaque". If you are copying a number from
    there, use `import-hueforge` (or divide by ~4.6) rather than pasting it in
    directly.

    Small td  = blocks light fast = effectively opaque (blacks, whites).
    Large td  = lets light through many layers (translucents, naturals).

USAGE
    filamentdb.py list
    filamentdb.py add --brand Polymaker --series "PLA Pro" --name Teal \
                      --color "#00808A" --td 0.32 --provenance measured
    filamentdb.py show polymaker-pla-pro-teal
    filamentdb.py set polymaker-pla-pro-teal --td 0.29 --provenance measured
    filamentdb.py rm polymaker-pla-pro-teal
    filamentdb.py seed          # writes a starter set of Polymaker PLA Pro
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date

import numpy as np

from tdforge.core import tdcolor

DEFAULT_DB = os.environ.get("FILAMENT_DB", "filaments.json")
SCHEMA_VERSION = 1

# "matched" sits between a measurement and a guess: somebody printed the
# filament and compared it by eye against the model's prediction. Good to
# roughly +/-15%, which is far better than a guess and far worse than a wedge
# read with an instrument -- so it is still dimmed and still flagged.
PROVENANCE = ("measured", "matched", "vendor", "estimated")

# HueForge's TD -- and Polymaker's, who publish on the same scale -- is roughly
# the thickness at which light stops getting through. Ours is the 1/e
# thickness. Reading "stops getting through" as 1% transmission puts them
# ln(100) = 4.605 apart, which is where this number comes from.
HUEFORGE_TD_RATIO = 4.6


def hueforge_td(td: float) -> float:
    """A HueForge/Polymaker TD in mm -> the 1/e thickness this database stores."""
    return float(td) / HUEFORGE_TD_RATIO

# Rough starting td by finish, in mm (1/e thickness). These are deliberately
# coarse -- they exist so you can slice something today, not so you can trust
# the color. Replace them with measured values via calibrate.py.
#
# Chosen so pigmented PLA goes effectively opaque (T < 1%) in roughly 0.6 mm,
# which is ~8 layers at 0.08 mm. That matters more than it looks: this whole
# technique lives on PARTIAL transmission. Too opaque and the reachable gamut
# collapses to the filament colors themselves with nothing in between; too
# transmissive and the base leaks through even at full stack depth, so
# saturated darks become unreachable. Opacity at ~8 layers leaves room for
# both -- saturated colors at depth, blended intermediates above them.
TD_GUESS = {
    "opaque": 0.13,
    "matte": 0.13,
    "gloss": 0.15,
    "silk": 0.17,
    "translucent": 0.70,
    "natural": 1.00,
}


@dataclass
class Filament:
    id: str
    brand: str = ""
    series: str = ""
    name: str = ""
    color: str = "#808080"        # sRGB hex of the bulk (fully opaque) color
    td: float = 0.30              # mm, 1/e transmittance thickness
    td_rgb: list | None = None    # optional per-channel [r,g,b] override
    finish: str = "opaque"
    provenance: str = "estimated"
    sku: str = ""                 # vendor part number, e.g. a Polymaker SKU
    measured_at: str = ""
    layer_height_ref: float = 0.0  # layer height the measurement was made at
    notes: str = ""
    tags: list = field(default_factory=list)

    # -- derived ---------------------------------------------------------

    def rgb(self) -> np.ndarray:
        return np.array(tdcolor.parse_hex(self.color), dtype=np.float64)

    def linear(self) -> np.ndarray:
        return tdcolor.srgb_to_linear(self.rgb())

    def td_vec(self) -> np.ndarray:
        """Per-channel transmission distance, mm."""
        if self.td_rgb:
            v = np.asarray(self.td_rgb, dtype=np.float64)
            # None/NaN (munki writes null for a channel it cannot fit) would
            # pass `v <= 0` and surface later as a KD-tree "not finite" error.
            if v.shape != (3,) or not np.isfinite(v).all() or (v <= 0).any():
                raise SystemExit(f"{self.id}: td_rgb must be three positive numbers")
            return v
        if self.td <= 0:
            raise SystemExit(f"{self.id}: td must be positive")
        return np.full(3, float(self.td))

    def transmittance(self, thickness: float) -> np.ndarray:
        """Fraction of light surviving `thickness` mm, per channel."""
        return np.exp(-float(thickness) / self.td_vec())

    def label(self) -> str:
        bits = [b for b in (self.brand, self.series, self.name) if b]
        return " ".join(bits) or self.id


def slugify(*parts) -> str:
    s = "-".join(str(p) for p in parts if p)
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return re.sub(r"-{2,}", "-", s)


class DB:
    def __init__(self, path=DEFAULT_DB):
        self.path = path
        self.filaments: dict[str, Filament] = {}
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path) as fh:
            raw = json.load(fh)
        ver = raw.get("version", 1)
        if ver > SCHEMA_VERSION:
            raise SystemExit(
                f"{self.path}: schema version {ver} is newer than this tool "
                f"understands ({SCHEMA_VERSION})"
            )
        known = set(Filament.__dataclass_fields__)
        for entry in raw.get("filaments", []):
            extra = set(entry) - known
            if extra:
                print(f"  ! {entry.get('id')}: ignoring unknown fields {sorted(extra)}",
                      file=sys.stderr)
            fid = entry.get("id")
            if not fid:
                raise SystemExit(f"{self.path}: a filament entry has no id: {entry}")
            try:
                fil = Filament(**{k: v for k, v in entry.items() if k in known})
                tdcolor.parse_hex(fil.color)
                fil.td = float(fil.td)
            except (TypeError, ValueError) as exc:
                raise SystemExit(f"{self.path}: filament {fid!r}: {exc}")
            self.filaments[fid] = fil

    def save(self):
        payload = {
            "version": SCHEMA_VERSION,
            "filaments": [
                {k: v for k, v in asdict(f).items() if v not in (None, "", [], 0.0)
                 or k in ("td", "color", "id")}
                for f in sorted(self.filaments.values(), key=lambda f: f.id)
            ],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, self.path)

    # -- lookup ----------------------------------------------------------

    def get(self, key: str) -> Filament:
        if key in self.filaments:
            return self.filaments[key]
        k = key.lower()
        # An exact name match beats a substring hit, so "blue" is not ambiguous
        # merely because "Dark Blue" also exists.
        exact = [f for f in self.filaments.values() if f.name.lower() == k]
        if len(exact) == 1:
            return exact[0]
        hits = exact or [f for f in self.filaments.values() if k in f.label().lower()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise SystemExit(
                f"no filament matching {key!r}. Run `filamentdb.py list` to see what's loaded."
            )
        raise SystemExit(
            f"{key!r} is ambiguous:\n  " + "\n  ".join(f"{f.id}  ({f.label()})" for f in hits)
        )

    def resolve(self, spec: str) -> list[Filament]:
        """Comma-separated ids/substrings -> filaments, in the given order."""
        out = [self.get(k.strip()) for k in spec.split(",") if k.strip()]
        if not out:
            raise SystemExit("no filaments selected")
        seen = set()
        for f in out:
            if f.id in seen:
                raise SystemExit(f"{f.id} listed twice")
            seen.add(f.id)
        return out

    def add(self, fil: Filament, overwrite=False):
        if fil.id in self.filaments and not overwrite:
            raise SystemExit(f"{fil.id} already exists; use `set` to edit it")
        self.filaments[fil.id] = fil


# --------------------------------------------------------------------------
# A starter set. Colors are eyeballed from Polymaker's swatches and every td
# is a finish-based guess -- provenance says so. Measure before you trust it.
# --------------------------------------------------------------------------

SEED = [
    ("White",        "#F4F5F0", "opaque"),
    ("Black",        "#1A1A1C", "opaque"),
    ("Grey",         "#8A8D8F", "opaque"),
    ("Red",          "#C02027", "opaque"),
    ("Orange",       "#E8590F", "opaque"),
    ("Yellow",       "#F2C200", "opaque"),
    ("Green",        "#118244", "opaque"),
    ("Teal",         "#00757F", "opaque"),
    ("Blue",         "#1B4FA0", "opaque"),
    ("Dark Blue",    "#123064", "opaque"),
    ("Purple",       "#6A2E8C", "opaque"),
    ("Magenta",      "#C0166B", "opaque"),
    ("Beige",        "#D8C6A6", "opaque"),
    ("Natural",      "#E8E2D2", "natural"),
]


def seed_db(db: DB, brand="Polymaker", series="PLA Pro"):
    n = 0
    for name, color, finish in SEED:
        fid = slugify(brand, series, name)
        if fid in db.filaments:
            continue
        db.add(
            Filament(
                id=fid, brand=brand, series=series, name=name, color=color,
                td=TD_GUESS[finish], finish=finish, provenance="estimated",
                notes="starter entry; color eyeballed, td is a finish-based guess",
            )
        )
        n += 1
    return n


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _swatch(rgb):
    r, g, b = (int(v) for v in rgb)
    fg = 30 if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else 97
    return f"\x1b[48;2;{r};{g};{b}m\x1b[{fg}m  \x1b[0m"


def cmd_list(db, args):
    if not db.filaments:
        print(f"{db.path} is empty. Run `filamentdb.py seed` for a starter set.")
        return
    rows = sorted(db.filaments.values(), key=lambda f: (f.brand, f.series, f.name))
    print(f"{'':2} {'id':38} {'color':8} {'td':>6}  {'prov':9} finish")
    for f in rows:
        if args.filter and args.filter.lower() not in f.label().lower():
            continue
        mark = "" if f.provenance == "measured" else "\x1b[2m"
        print(
            f"{_swatch(f.rgb())} {mark}{f.id:38}\x1b[0m {f.color:8} "
            f"{f.td:6.3f}  {f.provenance:9} {f.finish}"
        )
    est = sum(1 for f in db.filaments.values() if f.provenance != "measured")
    if est:
        print(f"\n{est} of {len(db.filaments)} entries are not measured "
              f"(dimmed above). Colors from those will be approximate.")


def cmd_show(db, args):
    f = db.get(args.id)
    for k, v in asdict(f).items():
        if v not in (None, "", []):
            print(f"  {k:16} {v}")
    print(f"  {'td_rgb (used)':16} {np.round(f.td_vec(), 4).tolist()}")
    print(f"\n  transmittance through N layers at {args.layer_height} mm:")
    for n in (1, 2, 4, 8, 16):
        t = f.transmittance(n * args.layer_height)
        print(f"    {n:2d} layers ({n*args.layer_height:5.2f} mm)  "
              f"{np.round(t, 4).tolist()}  {'opaque' if t.max() < 0.01 else ''}")


def cmd_add(db, args):
    fid = args.id or slugify(args.brand, args.series, args.name)
    tdcolor.parse_hex(args.color)
    td = args.td if args.td is not None else TD_GUESS.get(args.finish, 0.30)
    db.add(
        Filament(
            id=fid, brand=args.brand, series=args.series, name=args.name,
            color=args.color, td=td, td_rgb=args.td_rgb, finish=args.finish,
            provenance=args.provenance,
            measured_at=args.measured_at or (date.today().isoformat()
                                             if args.provenance == "measured" else ""),
            layer_height_ref=args.layer_height_ref or 0.0,
            notes=args.notes or "", tags=args.tags or [],
        ),
        overwrite=args.force,
    )
    db.save()
    print(f"added {fid}  {args.color}  td={td}  ({args.provenance})")


def cmd_set(db, args):
    f = db.get(args.id)
    changed = []
    for key in ("brand", "series", "name", "color", "td", "finish", "provenance",
                "notes", "measured_at", "layer_height_ref", "td_rgb", "tags", "sku"):
        v = getattr(args, key, None)
        if v is not None:
            if key == "color":
                tdcolor.parse_hex(v)
            setattr(f, key, v)
            changed.append(f"{key}={v}")
    if not changed:
        raise SystemExit("nothing to change; pass at least one field")
    if args.provenance == "measured" and not args.measured_at:
        f.measured_at = date.today().isoformat()
    db.save()
    print(f"{f.id}: " + ", ".join(changed))


def cmd_rm(db, args):
    f = db.get(args.id)
    del db.filaments[f.id]
    db.save()
    print(f"removed {f.id}")


def cmd_seed(db, args):
    n = seed_db(db, args.brand, args.series)
    db.save()
    print(f"seeded {n} new entries into {db.path}")
    print("All are provenance=estimated. Run calibrate.py to measure them.")


def cmd_import_sku(db, args):
    """Fill an entry in from a vendor part number."""
    from tdforge.tools import polymaker            # imports filamentdb, so it cannot load at top

    cat = polymaker.Catalog(args.catalog)
    fil, p, warnings, verb = polymaker.import_sku(
        db, cat, args.sku, fid=args.id, force=args.force,
        overwrite_measured=args.overwrite_measured)
    print(f"{verb} {fil.id}")
    print(f"  {fil.color}  td {fil.td}  {fil.provenance}  ({p.sku} {p.label()})")
    for w in warnings:
        print(f"  ! {w}", file=sys.stderr)
    db.save()
    print(f"wrote {db.path}")


def cmd_import_hueforge(db, args):
    """HueForge TD ~ 'thickness to opacity'; ours is the 1/e thickness."""
    f = db.get(args.id)
    f.td = round(hueforge_td(args.hueforge_td), 4)
    f.provenance = "vendor"
    f.notes = (f.notes + " | ").lstrip(" |") + f"td converted from HueForge TD {args.hueforge_td}"
    db.save()
    print(f"{f.id}: td={f.td:.4f} (from HueForge TD {args.hueforge_td})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, help="path to the JSON database")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list all filaments")
    p.add_argument("--filter", help="substring match on brand/series/name")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="detail for one filament")
    p.add_argument("id")
    p.add_argument("--layer-height", type=float, default=0.08)
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("add", help="add a filament")
    p.add_argument("--id")
    p.add_argument("--brand", default="Polymaker")
    p.add_argument("--series", default="PLA Pro")
    p.add_argument("--name", required=True)
    p.add_argument("--color", required=True, help="bulk color as RRGGBB hex")
    p.add_argument("--td", type=float, help="1/e thickness in mm; default by finish")
    p.add_argument("--td-rgb", type=float, nargs=3, help="per-channel td override")
    p.add_argument("--finish", default="opaque", choices=sorted(TD_GUESS))
    p.add_argument("--provenance", default="estimated", choices=PROVENANCE)
    p.add_argument("--measured-at", default="")
    p.add_argument("--layer-height-ref", type=float, default=0.0)
    p.add_argument("--notes", default="")
    p.add_argument("--tags", nargs="*")
    p.add_argument("--force", action="store_true", help="overwrite an existing id")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("set", help="edit fields on an existing filament")
    p.add_argument("id")
    for k in ("brand", "series", "name", "color", "finish", "notes", "measured_at",
              "sku"):
        p.add_argument(f"--{k.replace('_','-')}")
    p.add_argument("--td", type=float)
    p.add_argument("--td-rgb", type=float, nargs=3)
    p.add_argument("--layer-height-ref", type=float)
    p.add_argument("--provenance", choices=PROVENANCE)
    p.add_argument("--tags", nargs="*")
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("rm", help="delete a filament")
    p.add_argument("id")
    p.set_defaults(fn=cmd_rm)

    p = sub.add_parser("seed", help="write a starter Polymaker PLA Pro set")
    p.add_argument("--brand", default="Polymaker")
    p.add_argument("--series", default="PLA Pro")
    p.set_defaults(fn=cmd_seed)

    p = sub.add_parser("import-sku",
                       help="fill an entry in from a Polymaker SKU")
    p.add_argument("sku")
    p.add_argument("--catalog", default="polymaker_catalog.json")
    p.add_argument("--id", help="database id to use (default: from the product name)")
    p.add_argument("--force", action="store_true", help="update an existing entry")
    p.add_argument("--overwrite-measured", action="store_true",
                   help="allow vendor data to replace a measured entry")
    p.set_defaults(fn=cmd_import_sku)

    p = sub.add_parser("import-hueforge", help="convert a HueForge TD value")
    p.add_argument("id")
    p.add_argument("hueforge_td", type=float)
    p.set_defaults(fn=cmd_import_hueforge)

    args = ap.parse_args(argv)
    args.fn(DB(args.db), args)


if __name__ == "__main__":
    main()
