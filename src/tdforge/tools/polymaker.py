#!/usr/bin/env python3
"""polymaker -- look up a Polymaker SKU and turn it into a database entry.

Polymaker publish a HEX code and a TD for most of their filaments:

    https://wiki.polymaker.com/polymaker-products/more-about-our-products/hex-codes-and-transmission-distances

That page is scraped once into `polymaker_catalog.json` and read from there
afterwards, so lookups are instant, work offline, and diff in git like the
filament database does.

    polymaker.py refresh                 # re-scrape the wiki
    polymaker.py lookup CA02001
    polymaker.py search "silk blue"
    polymaker.py import CA02001 --write  # into the filament database

TWO SCALES, ONE NAME
    Their TD is HueForge's, which the wiki states plainly: "the approximate
    thickness of solid plastic (in millimeters) that light can penetrate before
    it is effectively blocked". This database's td is the 1/e thickness, which
    is a different number for the same filament -- see filamentdb.HUEFORGE_TD.
    Everything here converts on the way in; nothing stores a raw wiki TD in a
    `td` field.

WHAT VENDOR DATA IS WORTH
    A published HEX beats a guess and is a good place to start. It is still not
    a measurement of YOUR spool under YOUR lighting, and the wiki says as much
    about its own numbers ("it can be difficult to get a perfectly accurate HEX
    value for a physical object"). Imported entries are therefore marked
    `vendor`, never `measured`, and stackforge will keep flagging them until a
    wedge has been printed.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date

from tdforge.core import tdcolor
from tdforge.core.paths import data_path
from tdforge.core.filamentdb import DB, DEFAULT_DB, TD_GUESS, Filament, hueforge_td, slugify

URL = ("https://wiki.polymaker.com/polymaker-products/more-about-our-products/"
       "hex-codes-and-transmission-distances")
CACHE = os.environ.get("POLYMAKER_CATALOG") or data_path("polymaker_catalog.json")
CACHE_VERSION = 1

# The wiki is a GitBook page: every table cell is a div with role="cell" whose
# value is its first paragraph. Fragile by nature -- if Polymaker restyle the
# page this is what breaks, which is why `refresh` sanity-checks the parse
# before it overwrites a good cache.
ROW_RE = re.compile(r'role="row".*?(?=role="row"|$)', re.S)
CELL_RE = re.compile(r'role="cell".*?(?=role="cell"|$)', re.S)
PARA_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
TAG_RE = re.compile(r"<[^>]+>")
SKU_RE = re.compile(r"[A-Z]{2,4}[0-9]{4,9}")
HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")

# Finish inferred from the product and colour names. Only used to pick a
# starter td for SKUs the wiki has no TD for, and to label the entry.
FINISH_HINTS = [
    ("silk", "silk"),
    ("matte", "matte"),
    ("translucent", "translucent"),
    ("transparent", "translucent"),
    ("glow", "translucent"),
    ("uv shift", "translucent"),
    ("natural", "natural"),
    ("clear", "translucent"),
]


@dataclass
class Product:
    """One row of the wiki table."""

    sku: str
    product: str
    name: str
    hexes: list = field(default_factory=list)   # usually one; dual-colour has two
    td: float | None = None                     # Polymaker/HueForge scale, mm

    @property
    def hex(self) -> str:
        return self.hexes[0] if self.hexes else ""

    @property
    def dual(self) -> bool:
        return len(self.hexes) > 1

    def series(self) -> str:
        """Product name as a series: no brand prefix, no trademark signs."""
        s = self.product.replace("™", "").replace("®", "")
        s = re.sub(r"^\s*Polymaker\s+", "", s)
        return re.sub(r"\s+", " ", s).strip()

    def finish(self) -> str:
        hay = f"{self.product} {self.name}".lower()
        for needle, finish in FINISH_HINTS:
            if needle in hay:
                return finish
        return "opaque"

    def label(self) -> str:
        return f"{self.product} {self.name}"

    def summary(self) -> str:
        bits = [f"{self.sku:10} {self.product} — {self.name}"]
        bits.append(f"  hex {', '.join(self.hexes) or '(not published)'}")
        if self.td:
            bits.append(f"  TD {self.td} (Polymaker scale)  ->  "
                        f"td {hueforge_td(self.td):.4f} mm here")
        else:
            bits.append("  TD (not published for this SKU)")
        return "\n".join(bits)


# --------------------------------------------------------------------------
# scraping
# --------------------------------------------------------------------------


def fetch_html(url=URL, timeout=30, attempts=3, pause=2.0) -> str:
    """Download the wiki page. ~450 KB gzipped, ~50 MB if the server refuses.

    Retries a few times on network errors; if every attempt fails, exits with a message that
    says the cached catalogue is untouched and still usable offline."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (3mf_scripts filament catalogue)",
        "Accept-Encoding": "gzip",
        "Accept": "text/html",
    })
    err = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            return raw.decode("utf-8", errors="replace")
        except (OSError, EOFError) as e:  # URLError, HTTPError, timeouts and resets are OSErrors
            err = e
            if i + 1 < attempts:
                time.sleep(pause * (i + 1))
    raise SystemExit(f"could not fetch {url} after {attempts} attempts "
                     f"({type(err).__name__}: {err}). The cached catalogue is unchanged and "
                     f"still works offline.")


def _cell_text(chunk: str) -> str:
    m = PARA_RE.search(chunk)
    if not m:
        return ""
    return re.sub(r"\s+", " ", TAG_RE.sub("", m.group(1))).strip()


def parse_rows(html: str) -> list[Product]:
    """Pull the SKU / product / colour / hex / TD table out of the page."""
    out = []
    for row in ROW_RE.findall(html):
        cells = [_cell_text(c) for c in CELL_RE.findall(row)]
        if len(cells) != 5:
            continue
        sku, product, name, hexes, td = (c.strip() for c in cells)
        if not SKU_RE.fullmatch(sku):
            continue                       # header row, or something new
        # A TD of 0 is how the table spells "not applicable" (dual-colour
        # filaments carry it), not a filament that blocks all light.
        try:
            tdv = float(td) if td else None
        except ValueError:
            tdv = None
        out.append(Product(
            sku=sku, product=product, name=name,
            hexes=[h.upper() for h in HEX_RE.findall(hexes)],
            td=tdv if tdv and tdv > 0 else None,
        ))
    return out


# --------------------------------------------------------------------------
# the cached catalogue
# --------------------------------------------------------------------------


class Catalog:
    def __init__(self, path=CACHE):
        self.path = path
        self.products: dict[str, Product] = {}
        self.fetched_at = ""
        self.source = URL
        self._fits: dict = {}
        self.load()

    @property
    def available(self) -> bool:
        return bool(self.products)

    def load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path) as fh:
            raw = json.load(fh)
        if raw.get("version", 1) > CACHE_VERSION:
            raise SystemExit(
                f"{self.path}: catalogue version {raw['version']} is newer than "
                f"this tool understands ({CACHE_VERSION})")
        self.fetched_at = raw.get("fetched_at", "")
        self.source = raw.get("source", URL)
        for i, e in enumerate(raw.get("products", [])):
            sku = e.get("sku") if isinstance(e, dict) else None
            if not isinstance(sku, str) or not sku:
                print(f"  ! {self.path}: product #{i} has no sku; skipped", file=sys.stderr)
                continue
            td = e.get("td")
            if td is not None:
                try:
                    td = float(td)
                except (TypeError, ValueError):
                    td = None
                if td is None or not td > 0:
                    print(f"  ! {self.path}: {sku} has a bad td ({e.get('td')!r}); ignored",
                          file=sys.stderr)
                    td = None
            self.products[sku.upper()] = Product(
                sku=sku, product=e.get("product", ""), name=e.get("name", ""),
                hexes=e.get("hexes", []), td=td)

    def save(self):
        payload = {
            "version": CACHE_VERSION,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "td_scale": ("Polymaker/HueForge TD: approximate mm of solid plastic "
                         "light penetrates before it is effectively blocked. "
                         "Divide by 4.6 for the 1/e td this database stores."),
            "count": len(self.products),
            "products": [
                {k: v for k, v in asdict(p).items() if v not in (None, "", [])}
                for p in sorted(self.products.values(), key=lambda p: p.sku)
            ],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, self.path)

    # -- lookup ----------------------------------------------------------

    @staticmethod
    def _norm(sku: str) -> str:
        return re.sub(r"[^A-Z0-9]", "", sku.upper())

    def get(self, sku: str) -> Product:
        """Exact SKU, tolerant of case, spaces and dashes."""
        if not self.available:
            raise SystemExit(
                f"no catalogue at {self.path}. Run `polymaker.py refresh` "
                f"(needs network) to scrape it from the wiki.")
        want = self._norm(sku)
        for key, p in self.products.items():
            if self._norm(key) == want:
                return p
        near = self.search(sku, limit=5)
        hint = ("\n  did you mean:\n    " + "\n    ".join(
            f"{p.sku}  {p.label()}" for p in near)) if near else ""
        raise SystemExit(f"no Polymaker SKU {sku!r} in {self.path}{hint}")

    # -- td estimation ---------------------------------------------------

    def _training(self, group: str):
        """Deduped (Lab, log TD) for one group.

        Deduplicated on (hex, TD) because the rebrand left the same spool in
        the table twice under its old and new names, and counting it twice
        would make any accuracy estimate look better than it is.
        """
        import numpy as np

        seen = {}
        for p in self.products.values():
            if p.td and len(p.hexes) == 1 and _group_of(p.product) == group:
                seen.setdefault((p.hex, p.td), p)
        if not seen:
            return None
        lab = np.array([tdcolor.srgb_to_lab(np.array(tdcolor.parse_hex(p.hex), float))
                        for p in seen.values()])
        return lab, np.log(np.array([p.td for p in seen.values()])), list(seen.values())

    def _fit(self, group: str):
        """Pick the better of {colour regression, group median} by leave-one-out.

        Which one wins genuinely depends on the group -- colour predicts a
        pigmented PLA's TD reasonably and a metallic's hardly at all -- so this
        measures rather than assumes.
        """
        import numpy as np

        if group in self._fits:
            return self._fits[group]
        data = self._training(group)
        if data is None:
            self._fits[group] = None
            return None
        lab, lg, prods = data
        n = len(lg)
        A = np.column_stack([lab, np.ones(n)])

        err_reg, err_med = [], []
        for i in range(n):
            m = np.ones(n, bool)
            m[i] = False
            err_med.append(abs(np.median(lg[m]) - lg[i]))
            if m.sum() > 5:
                c = np.linalg.lstsq(A[m], lg[m], rcond=None)[0]
                err_reg.append(abs(A[i] @ c - lg[i]))
        f_med = float(np.exp(np.median(err_med)))
        f_reg = float(np.exp(np.median(err_reg))) if err_reg else float("inf")

        use_reg = n >= 20 and f_reg < f_med
        fit = {
            "group": group, "n": n, "lab": lab, "lg": lg, "prods": prods,
            "method": "colour regression" if use_reg else "group median",
            "factor": f_reg if use_reg else f_med,
            "coef": np.linalg.lstsq(A, lg, rcond=None)[0] if use_reg else None,
            "median": float(np.median(lg)),
            "lo": float(lg.min()), "hi": float(lg.max()),
        }
        self._fits[group] = fit
        return fit

    def estimate_td(self, color: str, finish="opaque") -> Estimate:
        """Guess a td for a filament with no published TD, from its colour."""
        import numpy as np

        group = FINISH_GROUP.get(finish, "pigmented")
        fit = self._fit(group) or self._fit("pigmented")
        if fit is None:
            raise SystemExit(
                f"no published TDs in {self.path} to learn from — run "
                f"`polymaker.py refresh`")

        lab = tdcolor.srgb_to_lab(np.array(tdcolor.parse_hex(color), float))
        if fit["coef"] is not None:
            lg = float(np.append(lab, 1.0) @ fit["coef"])
        else:
            lg = fit["median"]
        # Never extrapolate past what has actually been measured for the group.
        lg = min(max(lg, fit["lo"]), fit["hi"])
        td_poly = float(np.exp(lg))

        d = np.linalg.norm(fit["lab"] - lab, axis=1)
        j = int(d.argmin())
        near = fit["prods"][j]
        return Estimate(
            td=round(hueforge_td(td_poly), 4), td_polymaker=round(td_poly, 2),
            group=fit["group"], method=fit["method"], n=fit["n"],
            typical_factor=round(fit["factor"], 2),
            nearest=(near.name, near.hex, float(d[j]), near.td),
        )

    def search(self, text: str, limit=25) -> list[Product]:
        """Substring match over SKU, product and colour name, best first.

        Ranked, because "silk blue" otherwise buries the filament actually
        called Silk Blue under every Dual Silk whose name mentions blue.
        """
        q = text.lower().strip()
        needles = [t for t in q.split() if t]
        hits = [p for p in self.products.values()
                if all(n in f"{p.sku} {p.product} {p.name}".lower() for n in needles)]

        def rank(p):
            name = p.name.lower()
            return (
                0 if name == q else 1 if name.startswith(q) else 2 if q in name else 3,
                not bool(p.hexes) or p.dual,   # importable entries first
                not bool(p.td),                # then the ones with a published TD
                p.product, p.name,
            )

        hits.sort(key=rank)
        return hits[:limit]


# --------------------------------------------------------------------------
# estimating a td for a filament nobody published one for
# --------------------------------------------------------------------------

# Which published filaments a given finish should learn from. Effect finishes
# (metallic, galaxy, starlight) are their own world and predict badly from
# colour -- they get their own bucket so they cannot pollute the others.
FINISH_GROUP = {
    "opaque": "pigmented", "matte": "pigmented", "gloss": "pigmented",
    "silk": "silk", "translucent": "translucent", "natural": "translucent",
}


def _group_of(product: str) -> str:
    if any(k in product for k in ("Glow", "Luminous", "UV Shift")):
        return "glow"
    if any(k in product for k in ("Translucent", "Celestial", "Neon")):
        return "translucent"
    if "Silk" in product:
        return "silk"
    if any(k in product for k in ("Metallic", "Starlight", "Galaxy", "Marble")):
        return "effect"
    return "pigmented"


@dataclass
class Estimate:
    """A guessed td, with everything needed to judge how much to trust it."""

    td: float                  # 1/e mm, what the database stores
    td_polymaker: float        # the same guess on Polymaker's scale
    group: str
    method: str
    n: int                     # training filaments
    typical_factor: float      # leave-one-out median error of that method
    nearest: tuple | None = None   # (name, hex, dE, their td)

    def summary(self) -> str:
        out = [f"  td {self.td:.4f} mm  (Polymaker TD {self.td_polymaker:.1f})",
               f"  from {self.n} published {self.group} filaments by {self.method}",
               f"  typically off by about {self.typical_factor:.1f}x — a guess, "
               f"not a measurement"]
        if self.nearest:
            name, hx, de, td = self.nearest
            out.append(f"  nearest published colour: {name} {hx} (dE {de:.1f}), "
                       f"TD {td}")
        return "\n".join(out)


def refresh(path=CACHE, url=URL, verbose=True) -> Catalog:
    """Re-scrape the wiki into the cache, refusing an obviously broken parse."""
    if verbose:
        print(f"fetching {url}")
    rows = parse_rows(fetch_html(url))
    if verbose:
        print(f"  parsed {len(rows)} rows")
    # The page had ~1300 rows when this was written. A parse that collapses to a
    # handful means the markup moved, and silently replacing a good catalogue
    # with the wreckage is worse than failing.
    if len(rows) < 200:
        raise SystemExit(
            f"only {len(rows)} rows parsed out of that page — its markup has "
            f"probably changed. The cache at {path} has been left alone; "
            f"the table selectors are at the top of polymaker.py.")

    cat = Catalog(path)
    cat.products = {p.sku.upper(): p for p in rows}
    cat.fetched_at = date.today().isoformat()
    cat.source = url
    cat.save()
    if verbose:
        with_hex = sum(1 for p in rows if p.hexes)
        with_td = sum(1 for p in rows if p.td)
        both = sum(1 for p in rows if p.hexes and p.td)
        dual = sum(1 for p in rows if p.dual)
        print(f"  {len(rows)} products -> {path}")
        print(f"  {with_hex} with a hex, {with_td} with a TD, {both} with both")
        print(f"  {dual} dual-colour entries carry two hex codes and no usable TD")
    return cat


# --------------------------------------------------------------------------
# catalogue -> filament entry
# --------------------------------------------------------------------------


def to_filament(p: Product, existing: Filament | None = None, brand="Polymaker",
                fid: str | None = None, fetched_at="") -> tuple[Filament, list[str]]:
    """Build (or update) a database entry from a catalogue row.

    Returns the filament and a list of notes about what could not be filled in,
    so callers can show them rather than pretending the import was complete.
    """
    warnings = []
    if not p.hexes:
        raise SystemExit(
            f"{p.sku} ({p.label()}): the wiki publishes no HEX code for it. "
            f"Add it by hand, or measure it with calibrate.py.")
    if p.dual:
        raise SystemExit(
            f"{p.sku} ({p.label()}) is a dual-colour filament — the wiki lists "
            f"{' and '.join(p.hexes)}. The optical model here assumes one bulk "
            f"colour per filament, so it cannot be imported as a single entry.")

    fil = existing or Filament(id="")
    fil.id = fid or fil.id or slugify(brand, p.series(), p.name)
    fil.brand = brand
    fil.series = p.series()
    fil.name = p.name
    fil.color = tdcolor.to_hex(tdcolor.parse_hex(p.hex))
    fil.sku = p.sku
    fil.finish = p.finish()

    src = f"Polymaker wiki{f', retrieved {fetched_at}' if fetched_at else ''}"
    if p.td:
        fil.td = round(hueforge_td(p.td), 4)
        fil.td_rgb = None
        fil.provenance = "vendor"
        note = (f"SKU {p.sku} ({p.product}); hex and TD {p.td} from the {src}; "
                f"td = {p.td}/4.6 mm")
    else:
        # Colour is vendor data but td is still a guess, and the entry has to
        # keep saying so -- 'vendor' on a guessed td would be a lie about the
        # number that matters most.
        if existing is None or not existing.td:
            fil.td = TD_GUESS[fil.finish]
        fil.provenance = "estimated"
        note = (f"SKU {p.sku} ({p.product}); hex from the {src}, which "
                f"publishes no TD for this SKU; td is a guess for a "
                f"{fil.finish} finish")
        warnings.append(
            f"{p.sku} has no published TD — the colour is Polymaker's, the td "
            f"is a finish-based guess, so the entry stays 'estimated'.")

    if note not in (fil.notes or ""):       # re-importing must not grow the note
        fil.notes = ((fil.notes + " | ").lstrip(" |") + note) if fil.notes else note
    fil.tags = sorted(set(fil.tags or []) | {"polymaker"})
    return fil, warnings


def import_sku(db: DB, cat: Catalog, sku: str, fid=None, force=False,
               overwrite_measured=False):
    """Put one SKU into `db` (in memory). Returns (filament, row, warnings, verb)."""
    p = cat.get(sku)
    fid = fid or slugify("Polymaker", p.series(), p.name)
    existing = db.filaments.get(fid)
    if existing and not force:
        raise SystemExit(
            f"{fid} already exists. Pass --force to update it from the "
            f"catalogue, or --id to import alongside it.")
    if (existing and existing.provenance == "measured" and not overwrite_measured):
        raise SystemExit(
            f"{fid} is marked measured — a wedge was printed for it. Refusing "
            f"to replace that with vendor data; pass --overwrite-measured if "
            f"you really mean to.")
    fil, warnings = to_filament(p, existing, fid=fid, fetched_at=cat.fetched_at)
    db.filaments[fil.id] = fil
    return fil, p, warnings, "updated" if existing else "added"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cmd_refresh(args):
    refresh(args.catalog, args.url)


def cmd_lookup(args):
    cat = Catalog(args.catalog)
    for sku in args.sku:
        print(cat.get(sku).summary())
        print()


def cmd_search(args):
    cat = Catalog(args.catalog)
    hits = cat.search(" ".join(args.text), limit=args.limit)
    if not hits:
        raise SystemExit(f"nothing matching {' '.join(args.text)!r}")
    print(f"{'sku':10} {'hex':9} {'TD':>5}  product / colour")
    for p in hits:
        td = f"{p.td:5.1f}" if p.td else "    -"
        dual = "  [dual colour, not importable]" if p.dual else ""
        print(f"{p.sku:10} {p.hex or '-':9} {td}  {p.product} — {p.name}{dual}")
    print(f"\n{len(hits)} shown"
          + (f" (limit {args.limit})" if len(hits) == args.limit else ""))


def cmd_import(args):
    cat = Catalog(args.catalog)
    db = DB(args.db)
    for sku in args.sku:
        fil, p, warnings, verb = import_sku(
            db, cat, sku, fid=args.id, force=args.force,
            overwrite_measured=args.overwrite_measured)
        print(f"{verb} {fil.id}")
        print(f"  {fil.color}  td {fil.td}  {fil.provenance}  ({p.sku} {p.label()})")
        for w in warnings:
            print(f"  ! {w}", file=sys.stderr)
    if args.write:
        db.save()
        print(f"\nwrote {db.path}")
    else:
        print("\n(dry run — pass --write to save)")


def cmd_guess_td(args):
    cat = Catalog(args.catalog)
    db = DB(args.db)
    targets = (list(db.filaments.values()) if args.all
               else [db.get(k) for k in args.filament])
    changed = 0
    print(f"{'filament':30} {'colour':9} {'td now':>8} {'td guess':>9}  basis")
    for f in targets:
        if f.provenance == "measured" and not args.overwrite_measured:
            print(f"{f.id[:30]:30} {f.color:9} {f.td:8.4f} {'—':>9}  measured, left alone")
            continue
        if f.provenance == "vendor" and not args.overwrite_vendor:
            print(f"{f.id[:30]:30} {f.color:9} {f.td:8.4f} {'—':>9}  vendor td, left alone")
            continue
        est = cat.estimate_td(f.color, f.finish)
        name, hx, de, td = est.nearest
        print(f"{f.id[:30]:30} {f.color:9} {f.td:8.4f} {est.td:9.4f}  "
              f"{est.method}, {est.group} (n={est.n}); nearest {name} dE {de:.0f}")
        if args.write:
            f.td = est.td
            f.td_rgb = None
            f.provenance = "estimated"
            note = (f"td estimated from {est.n} published Polymaker {est.group} "
                    f"TDs by {est.method} (typically off by ~{est.typical_factor}x)")
            if note not in (f.notes or ""):
                f.notes = ((f.notes + " | ").lstrip(" |") + note) if f.notes else note
            changed += 1
    if args.write:
        db.save()
        print(f"\nupdated {changed} entries in {db.path}")
        print("All remain 'estimated' — these are guesses off Polymaker's "
              "published data, not measurements of your spools.")
    else:
        print("\n(dry run — pass --write to save)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog", default=CACHE, help="cached scrape of the wiki")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("refresh", help="re-scrape the wiki into the cache")
    p.add_argument("--url", default=URL)
    p.set_defaults(fn=cmd_refresh)

    p = sub.add_parser("lookup", help="show one or more SKUs")
    p.add_argument("sku", nargs="+")
    p.set_defaults(fn=cmd_lookup)

    p = sub.add_parser("search", help="find SKUs by product or colour name")
    p.add_argument("text", nargs="+")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("guess-td",
                       help="estimate td from colour, for filaments with no published TD")
    p.add_argument("filament", nargs="*", help="database ids")
    p.add_argument("--all", action="store_true", help="every entry in the database")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--overwrite-measured", action="store_true")
    p.add_argument("--overwrite-vendor", action="store_true",
                   help="also replace published (vendor) TDs with a colour-based estimate")
    p.add_argument("--write", action="store_true")
    p.set_defaults(fn=cmd_guess_td)

    p = sub.add_parser("import", help="write a SKU into the filament database")
    p.add_argument("sku", nargs="+")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--id", help="database id to use (default: from brand/series/name)")
    p.add_argument("--force", action="store_true", help="update an existing entry")
    p.add_argument("--overwrite-measured", action="store_true",
                   help="allow vendor data to replace a measured entry")
    p.add_argument("--write", action="store_true", help="save the database")
    p.set_defaults(fn=cmd_import)

    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
