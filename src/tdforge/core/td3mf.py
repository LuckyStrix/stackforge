#!/usr/bin/env python3
"""Shared 3MF I/O: reading build items into world space, and writing objects
with per-extruder modifier volumes for Orca/Bambu and PrusaSlicer."""

from __future__ import annotations

import datetime
import json
import re
import os
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape

import numpy as np

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
SLIC3R_NS = "http://schemas.slic3r.org/3mf/2017/06"
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
MODEL_REL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"

IDENTITY = np.eye(4)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


@dataclass
class Mesh:
    verts: np.ndarray
    tris: np.ndarray


@dataclass
class Obj:
    key: tuple
    name: str
    mesh: Mesh | None = None
    components: list = field(default_factory=list)


@dataclass
class Item:
    """A placed instance of an object, baked into world space."""

    name: str
    verts: np.ndarray
    tris: np.ndarray
    appearance: object = None     # glb.Appearance when the model carries colour, else None


def parse_transform(text: str | None) -> np.ndarray:
    """3MF stores a 4x3 row-major matrix, row-vector convention."""
    if not text:
        return IDENTITY.copy()
    v = [float(x) for x in text.replace(",", " ").split()]
    if len(v) != 12:
        return IDENTITY.copy()
    m = np.eye(4)
    m[0, :3], m[1, :3], m[2, :3], m[3, :3] = v[0:3], v[3:6], v[6:9], v[9:12]
    return m


def apply_transform(mat: np.ndarray, pts: np.ndarray) -> np.ndarray:
    if np.array_equal(mat, IDENTITY):
        return pts
    return pts @ mat[:3, :3] + mat[3, :3]


def _norm_path(p: str, base: str = "") -> str:
    if not p.startswith("/"):
        p = os.path.join(os.path.dirname(base), p) if base else p
    return p.lstrip("/")


def read_model(path: str, scale_to=None, bed=None) -> list[Item]:
    """Load a 3MF or a glTF/GLB. GLB items carry `appearance` (UVs, textures, colours)."""
    from tdforge.core import glb
    if glb.is_glb_path(path):
        return [glb.read_glb(path, scale_to=scale_to, bed=bed)]
    return read_3mf(path)


def read_3mf(path: str) -> list[Item]:
    """Load every build item as a world-space mesh."""
    if not os.path.exists(path):
        raise SystemExit(f"{path}: no such file")
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        model_paths = [n for n in names if n.lower().endswith(".model")]
        if not model_paths:
            raise SystemExit(f"{path}: no .model part found; is this really a 3MF?")

        root_path = None
        if "_rels/.rels" in names:
            for rel in ET.fromstring(zf.read("_rels/.rels")).iter(
                f"{{{RELS_NS}}}Relationship"
            ):
                if rel.get("Type") == MODEL_REL:
                    cand = _norm_path(rel.get("Target", ""))
                    if cand in names:
                        root_path = cand
        if root_path is None:
            root_path = (
                "3D/3dmodel.model" if "3D/3dmodel.model" in names else model_paths[0]
            )

        roots = {}
        for mp in model_paths:
            try:
                roots[mp] = ET.fromstring(zf.read(mp))
            except ET.ParseError:
                pass

    objects: dict[tuple, Obj] = {}
    for mp, root in roots.items():
        for obj in root.iter(f"{{{CORE_NS}}}object"):
            o = Obj(key=(mp, obj.get("id")), name=obj.get("name") or f"object_{obj.get('id')}")
            mesh_el = obj.find(f"{{{CORE_NS}}}mesh")
            if mesh_el is not None:
                o.mesh = _read_mesh(mesh_el)
            comp_el = obj.find(f"{{{CORE_NS}}}components")
            if comp_el is not None:
                for c in comp_el.iter(f"{{{CORE_NS}}}component"):
                    cpath = c.get(f"{{{PROD_NS}}}path")
                    o.components.append(
                        (
                            (_norm_path(cpath, mp) if cpath else mp, c.get("objectid")),
                            parse_transform(c.get("transform")),
                        )
                    )
            objects[o.key] = o

    build = roots[root_path].find(f"{{{CORE_NS}}}build")
    if build is None:
        raise SystemExit(f"{path}: no <build> section")

    items: list[Item] = []
    for it in build.iter(f"{{{CORE_NS}}}item"):
        ipath = it.get(f"{{{PROD_NS}}}path")
        key = (_norm_path(ipath, root_path) if ipath else root_path, it.get("objectid"))
        parts: list = []
        _flatten(objects, key, parse_transform(it.get("transform")), parts, set())
        if not parts:
            continue
        verts, tris = concat_meshes(parts)
        items.append(
            Item(
                name=objects[key].name if key in objects else f"item_{len(items)}",
                verts=verts,
                tris=tris,
            )
        )
    if not items:
        raise SystemExit(f"{path}: build section referenced no usable geometry")
    return items


def _read_mesh(mesh_el):
    vs = mesh_el.find(f"{{{CORE_NS}}}vertices")
    ts = mesh_el.find(f"{{{CORE_NS}}}triangles")
    if vs is None or ts is None:
        return None
    verts = np.array(
        [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in vs], np.float64
    )
    tris = np.array(
        [(int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))) for t in ts], np.int64
    )
    if len(verts) == 0 or len(tris) == 0:
        return None
    return Mesh(verts, tris)


def _flatten(objects, key, mat, out, seen):
    o = objects.get(key)
    if o is None or key in seen:
        return
    seen = seen | {key}
    if o.mesh is not None:
        out.append((apply_transform(mat, o.mesh.verts), o.mesh.tris))
    for child_key, child_mat in o.components:
        _flatten(objects, child_key, child_mat @ mat, out, seen)


def concat_meshes(parts):
    verts, tris, off = [], [], 0
    for v, t in parts:
        verts.append(v)
        tris.append(t + off)
        off += len(v)
    return np.vstack(verts), np.vstack(tris)


# --------------------------------------------------------------------------
# box geometry
# --------------------------------------------------------------------------

BOX_TRIS = np.array(
    [
        [0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
        [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
        [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7],
    ],
    dtype=np.int64,
)


def box_verts(x0, y0, z0, x1, y1, z1):
    return np.array(
        [
            [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
            [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
        ],
        dtype=np.float64,
    )


class BoxBuilder:
    """Accumulates axis-aligned boxes into a single mesh."""

    def __init__(self):
        self.verts, self.tris = [], []

    def add(self, x0, y0, z0, x1, y1, z1):
        self.tris.append(BOX_TRIS + 8 * len(self.verts))
        self.verts.append(box_verts(x0, y0, z0, x1, y1, z1))

    def __len__(self):
        return len(self.verts)

    def mesh(self):
        if not self.verts:
            return None
        return np.vstack(self.verts), np.vstack(self.tris)


def greedy_rects(label: np.ndarray, valid: np.ndarray):
    """Axis-aligned rectangles over equal labels. Yields (row, col, h, w, label)."""
    h, w = label.shape
    used = ~valid
    for y in range(h):
        x = 0
        while x < w:
            if used[y, x]:
                x += 1
                continue
            v = label[y, x]
            rw = 1
            while x + rw < w and not used[y, x + rw] and label[y, x + rw] == v:
                rw += 1
            rh = 1
            while y + rh < h:
                row = slice(x, x + rw)
                if used[y + rh, row].any() or (label[y + rh, row] != v).any():
                    break
                rh += 1
            used[y : y + rh, x : x + rw] = True
            yield y, x, rh, rw, int(v)
            x += rw


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def _fmt(x):
    return f"{x:.6g}"


def _mesh_xml(verts, tris, indent="    "):
    vs = "".join(
        f'{indent}  <vertex x="{_fmt(a)}" y="{_fmt(b)}" z="{_fmt(c)}"/>\n' for a, b, c in verts
    )
    ts = "".join(f'{indent}  <triangle v1="{a}" v2="{b}" v3="{c}"/>\n' for a, b, c in tris)
    return (
        f"{indent}<mesh>\n{indent} <vertices>\n{vs}{indent} </vertices>\n"
        f"{indent} <triangles>\n{ts}{indent} </triangles>\n{indent}</mesh>\n"
    )


# Mirrors FlashForge's bundled 3MFs: neither of them declares a "config"
# content type even though both ship .config parts, so we don't either.
CONTENT_TYPES = f"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="{CT_NS}">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="png" ContentType="image/png"/>
</Types>
"""

DOT_RELS = f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{RELS_NS}">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="{MODEL_REL}"/>
</Relationships>
"""


# Only settings are taken from a template. Anything describing geometry or a
# specific plate (cut_information.xml, plate thumbnails, per-object slice info)
# would still be pointing at the template's objects, which we have replaced --
# a stale reference is a likelier way to break the load than a missing one.
TEMPLATE_PARTS = (
    "Metadata/project_settings.config",
    "Metadata/slice_info.config",
)


def _zip_out(out_path, model, config, config_name, template=None, colors=None,
             layer_height=None, first_layer_height=None, solid=False,
             app_version=None):
    """Write the package, optionally carrying a template project's settings.

    A full `Metadata/project_settings.config` is a ~450-key printer profile.
    Synthesizing one would mean inventing a printer, so instead we copy it
    verbatim from a project the user exported from their own slicer and only
    replace the geometry and the part/extruder mapping. Their print profile,
    filaments and bed stay exactly as they configured them.
    """
    extra = {}
    ct, rels = CONTENT_TYPES, DOT_RELS
    if template:
        with zipfile.ZipFile(template) as tz:
            names = set(tz.namelist())
            for n in TEMPLATE_PARTS:
                if n in names:
                    extra[n] = tz.read(n)
            if not extra:
                raise SystemExit(
                    f"{template}: no settings parts found "
                    f"({', '.join(TEMPLATE_PARTS)}). Export it from your slicer "
                    f"as a PROJECT (.3mf), not as a plain model."
                )
        key = "Metadata/project_settings.config"
        if key in extra and (colors or layer_height or first_layer_height or solid):
            extra[key] = _patch_settings(
                extra[key], colors, layer_height, first_layer_height, solid
            )
        sinfo = "Metadata/slice_info.config"
        if sinfo in extra:
            extra[sinfo] = _patch_slice_info(extra[sinfo], app_version)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("[Content_Types].xml", ct)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("3D/3dmodel.model", model)
        zf.writestr(config_name, config)
        for n, data in sorted(extra.items()):
            zf.writestr(n, data)


def _patch_slice_info(raw: bytes, version: str | None) -> bytes:
    """Fill in an empty X-BBL-Client-Version.

    Flash Studio writes this field blank in its own project exports. Copying a
    blank into a file that is then version-checked reads as "very old", which
    is enough to trigger the stale-generator warning even when everything else
    is current.
    """
    if not version:
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    return re.sub(
        r'(key="X-BBL-Client-Version"\s+value=")(")',
        lambda m: m.group(1) + version + m.group(2),
        text,
    ).encode()


def _fmt_setting(v) -> str:
    """Profile numbers are stored as strings; keep that convention."""
    return f"{float(v):g}"


def _patch_settings(raw: bytes, colors=None, layer_height=None,
                    first_layer_height=None, solid=False) -> bytes:
    """Rewrite the template profile to describe the file we actually built.

    Baking the layer geometry in matters: the modifier boxes are placed for one
    specific layer grid, and if the profile that opens alongside them says
    something else, colours silently drop out on the layers that miss. Writing
    the heights we generated for means opening the project cannot disagree with
    the geometry inside it.
    """
    try:
        settings = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return raw

    if layer_height is not None:
        lo = _first_num(settings.get("min_layer_height"))
        hi = _first_num(settings.get("max_layer_height"))
        if lo is not None and layer_height < lo - 1e-9:
            print(f"  ! layer height {layer_height} is below the profile's "
                  f"minimum {lo}; the slicer may refuse it")
        if hi is not None and layer_height > hi + 1e-9:
            print(f"  ! layer height {layer_height} is above the profile's "
                  f"maximum {hi}; the slicer may refuse it")
        settings["layer_height"] = _fmt_setting(layer_height)
    if first_layer_height is not None:
        settings["initial_layer_print_height"] = _fmt_setting(first_layer_height)

    if solid:
        # The optical model treats every colour layer as a continuous film.
        # Stock profiles make only the top shell solid (5 layers / 1 mm here)
        # and leave the rest at 15% sparse infill, so most of the stack would
        # be full of holes and the colour maths would not describe the print.
        was = settings.get("sparse_infill_density")
        settings["sparse_infill_density"] = "100%"
        # Combining infill across layers would merge neighbouring colour
        # layers into one extrusion, which is exactly what must not happen.
        settings["infill_combination"] = "0"
        if was and was != "100%":
            print(f"  - infill forced to 100% (profile had {was}); every colour "
                  f"layer must be solid or the stack has holes in it")

    if colors:
        _patch_colors(settings, colors)
        if len(colors) > 1:
            _fit_prime_tower(settings)
    return json.dumps(settings, indent=4).encode()


def _first_num(v):
    if isinstance(v, list):
        v = v[0] if v else None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Depth the prime tower grew to for a 4-filament plaque in Flash Studio 1.7.8
# (from y=220.6 it reached y=269.3), plus a margin.
PRIME_TOWER_DEPTH = 55.0


def _fit_prime_tower(settings) -> None:
    """Pull the prime tower in from the back edge if it cannot fit there.

    A single-colour template never grows a tower, so its saved position can sit
    too close to the edge: Flash Studio 1.7.8 then refuses to slice ("G-code in
    unprintable area"). Moving it is what the user would do by hand.
    """
    def num(v):
        v = v[0] if isinstance(v, list) and v else v
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    if num(settings.get("enable_prime_tower")) != 1:
        return
    y = num(settings.get("wipe_tower_y"))
    area = settings.get("printable_area")
    try:
        bed_y = max(float(p.split("x")[1]) for p in area)
    except (TypeError, ValueError, IndexError, AttributeError):
        return
    if y is None or bed_y - y >= PRIME_TOWER_DEPTH:
        return
    new = max(0.0, bed_y - PRIME_TOWER_DEPTH)
    raw = settings["wipe_tower_y"]
    settings["wipe_tower_y"] = [f"{new:g}"] * len(raw) if isinstance(raw, list) else f"{new:g}"
    print(f"  - prime tower moved from y={y:g} to y={new:g}: at {y:g} mm it runs off "
          f"the {bed_y:g} mm bed once it has colour changes to prime")


def _attr(v) -> str:
    """Escape for a double-quoted XML attribute (object names come from users)."""
    return _xml_escape(str(v), {'"': "&quot;"})


def _patch_colors(settings, colors) -> None:
    """Point the template's filament swatches at the filaments we actually used.

    Only the first len(colors) entries are touched: the array length is tied to
    the printer's extruder count in the profile, so resizing it would describe a
    machine the user does not have.
    """
    existing = settings.get("filament_colour")
    if not isinstance(existing, list) or not existing:
        return
    if len(existing) < len(colors):
        # Dozens of per-filament arrays in this profile are indexed in
        # lockstep (type, temperature, flow, retraction, ...). Growing just
        # this one would desynchronise them, so let the slicer add the slots.
        # Sliced in Flash Studio 1.7.8, the missing extruders collapse onto
        # extruder 1: those colours print in the base, so refuse outright.
        raise SystemExit(
            f"template has only {len(existing)} filament slot(s) but "
            f"{len(colors)} filaments are in use; extruders "
            f"{len(existing)+1}-{len(colors)} would print as extruder 1.\n"
            f"Set up all {len(colors)} filaments in your slicer and re-export "
            f"the template."
        )
    # filament_multi_colour mirrors filament_colour in newer profiles; leaving
    # it stale shows the template's swatches instead of ours.
    for key in ("filament_colour", "filament_multi_colour"):
        arr = settings.get(key)
        if isinstance(arr, list) and len(arr) >= len(colors):
            for i, hexcol in enumerate(colors):
                arr[i] = hexcol


def write_prusa(out_path, items, decals, base_ext, part_type="modifier",
                template=None, colors=None, layer_height=None,
                first_layer_height=None, solid=False, object_settings=None):
    """One object per item; extra volumes are triangle ranges in the same mesh.

    `object_settings` become per-object overrides, PrusaSlicer's equivalent of
    the Orca ones: the template's Prusa profile is not carried over, so they
    are the only way a setting reaches the slicer.
    """
    vtype = "ModifierMesh" if part_type == "modifier" else "ModelPart"
    res_xml, cfg_xml, build_xml = [], [], []
    for oid, item in enumerate(items, start=1):
        verts, tris = [item.verts], [item.tris]
        voff, toff = len(item.verts), len(item.tris)
        vols = [
            f'  <volume firstid="0" lastid="{toff - 1}">\n'
            f'   <metadata type="volume" key="name" value="{_attr(item.name)}"/>\n'
            f'   <metadata type="volume" key="volume_type" value="ModelPart"/>\n'
            f'   <metadata type="volume" key="extruder" value="{base_ext}"/>\n'
            f"  </volume>\n"
        ]
        for ext, dv, dt in decals.get(oid - 1, []):
            verts.append(dv)
            tris.append(dt + voff)
            vols.append(
                f'  <volume firstid="{toff}" lastid="{toff + len(dt) - 1}">\n'
                f'   <metadata type="volume" key="name" value="deco_e{ext}"/>\n'
                f'   <metadata type="volume" key="volume_type" value="{vtype}"/>\n'
                f'   <metadata type="volume" key="extruder" value="{ext}"/>\n'
                f"  </volume>\n"
            )
            voff += len(dv)
            toff += len(dt)

        res_xml.append(
            f'  <object id="{oid}" type="model">\n'
            + _mesh_xml(np.vstack(verts), np.vstack(tris))
            + "  </object>\n"
        )
        cfg_xml.append(
            f' <object id="{oid}">\n'
            f'  <metadata type="object" key="name" value="{_attr(item.name)}"/>\n'
            + "".join(f'  <metadata type="object" key="{_attr(k)}" value="{_attr(v)}"/>\n'
                      for k, v in (object_settings or {}).items())
            + "".join(vols)
            + " </object>\n"
        )
        build_xml.append(f'  <item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n')

    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE_NS}" '
        f'xmlns:slic3rpe="{SLIC3R_NS}">\n'
        ' <metadata name="Application">3mf_scripts</metadata>\n'
        " <resources>\n" + "".join(res_xml) + " </resources>\n"
        " <build>\n" + "".join(build_xml) + " </build>\n</model>\n"
    )
    config = '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n' + "".join(cfg_xml) + "</config>\n"
    _zip_out(out_path, model, config, "Metadata/Slic3r_PE_model.config",
             template, colors, layer_height, first_layer_height, solid,
             app_version=_version_of(_template_app(template) or ORCA_APP))


# Bambu/Orca-derived slicers version-gate the project loader: they parse the
# Application metadata into a generator version and, if it reads as too old (or
# fails to parse), fall back to "loading geometry data only" -- silently
# discarding model_settings.config, which is where every part and extruder
# assignment lives.
#
# The comparison is against the RUNNING slicer's version, so the string has to
# be at least as new as the installed build -- not merely well-formed. Flash
# Studio 1.7.x writes "BambuStudio-2.3.2"; the calibration 3MFs it bundles say
# "BambuStudio-02.00.02.01", which parses as 2.0.2.1 and is therefore *older*
# than the slicer shipping them. Copying a bundled file's string is exactly the
# trap that produced "generated by an old OrcaSlicer version" here.
#
# So when a template is supplied we echo back whatever it declares, which is by
# construction the version of the slicer that wrote it. The constant below is
# only the fallback for template-less output.
ORCA_APP = "BambuStudio-2.3.2"
ORCA_3MF_VERSION = "1"


def template_layer_settings(template):
    """(layer_height, initial_layer_height) from a template project, or (None, None).

    Per-layer modifier boxes only land correctly if they sit on the same grid
    the slicer will actually use, so these have to come from the profile that
    will do the slicing rather than from a default.
    """
    try:
        with zipfile.ZipFile(template) as tz:
            if "Metadata/project_settings.config" not in tz.namelist():
                return None, None
            settings = json.loads(tz.read("Metadata/project_settings.config"))
    except (zipfile.BadZipFile, OSError, ValueError):
        return None, None

    def num(key):
        v = settings.get(key)
        if isinstance(v, list):
            v = v[0] if v else None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return num("layer_height"), num("initial_layer_print_height")


def check_template(template) -> None:
    """Refuse an unusable --template before any work is done, not at write time."""
    try:
        with zipfile.ZipFile(template) as tz:
            names = set(tz.namelist())
    except (zipfile.BadZipFile, OSError) as exc:
        raise SystemExit(f"--template {template}: not a readable 3MF ({exc})")
    if "Metadata/project_settings.config" not in names:
        raise SystemExit(
            f"--template {template}: no Metadata/project_settings.config. Export it "
            f"from your slicer as a PROJECT (.3mf), not as a plain model.")


def template_bed_size(template):
    """(x, y) extent of the printable area in mm, or None."""
    try:
        with zipfile.ZipFile(template) as tz:
            settings = json.loads(tz.read("Metadata/project_settings.config"))
        pts = [tuple(float(v) for v in p.split("x")) for p in settings["printable_area"]]
    except (KeyError, zipfile.BadZipFile, OSError, ValueError, TypeError, AttributeError):
        return None
    xs, ys = zip(*pts)
    return max(xs) - min(xs), max(ys) - min(ys)


def template_top_solid_depth(template, layer_height):
    """How deep the profile's solid top shell reaches, in mm.

    Orca takes whichever of top_shell_layers / top_shell_thickness gives more
    material, so this is the max of the two. Below it, infill goes sparse.
    """
    try:
        with zipfile.ZipFile(template) as tz:
            if "Metadata/project_settings.config" not in tz.namelist():
                return None
            settings = json.loads(tz.read("Metadata/project_settings.config"))
    except (zipfile.BadZipFile, OSError, ValueError):
        return None

    if str(settings.get("sparse_infill_density", "")).rstrip("%") == "100":
        return float("inf")  # everything is solid already
    layers = _first_num(settings.get("top_shell_layers"))
    thick = _first_num(settings.get("top_shell_thickness"))
    depths = [d for d in ((layers or 0) * layer_height, thick or 0) if d]
    return max(depths) if depths else None


def _version_of(app: str | None) -> str | None:
    """"BambuStudio-2.3.2" -> "2.3.2"."""
    if not app:
        return None
    m = re.search(r'([\d.]+)\s*$', app)
    return m.group(1) if m else None


def _template_app(template) -> str | None:
    """The Application string the template's own slicer wrote."""
    if not template:
        return None
    try:
        with zipfile.ZipFile(template) as tz:
            for n in ("3D/3dmodel.model",):
                if n in tz.namelist():
                    head = tz.read(n)[:4096].decode("utf-8", "replace")
                    m = re.search(r'<metadata name="Application">([^<]+)</metadata>', head)
                    if m:
                        return m.group(1).strip()
    except (zipfile.BadZipFile, OSError):
        pass
    return None
IDENTITY_16 = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"


def _uuid(n: int, kind: str = "o") -> str:
    """Deterministic production-extension UUID.

    Declaring requiredextensions="p" obliges every object, component and build
    item to carry a p:UUID. Deriving them from the id keeps output byte-stable
    across runs, which matters for diffing and for republishing the same file.
    """
    tag = {"o": "0001", "c": "0002", "b": "0003", "build": "0004"}.get(kind, "0000")
    return f"{n:08x}-81cb-4c03-9d28-{tag}05dfa1dc"  # 8-4-4-4-12


def write_orca(out_path, items, decals, base_ext, part_type="modifier",
               template=None, colors=None, layer_height=None,
               first_layer_height=None, solid=False, object_settings=None):
    """Each part is its own object; an assembly object binds them per item."""
    subtype = "modifier_part" if part_type == "modifier" else "normal_part"
    res_xml, cfg_xml, build_xml, asm_xml = [], [], [], []
    plate_objects = []
    next_id = 1
    for i, item in enumerate(items):
        parts = [(base_ext, "normal_part", item.name, item.verts, item.tris)]
        for ext, dv, dt in decals.get(i, []):
            parts.append((ext, subtype, f"deco_e{ext}", dv, dt))

        part_ids = []
        for ext, st, name, v, t in parts:
            res_xml.append(
                f'  <object id="{next_id}" p:UUID="{_uuid(next_id)}" type="model">\n'
                + _mesh_xml(v, t)
                + "  </object>\n"
            )
            part_ids.append((next_id, ext, st, name))
            next_id += 1

        asm_id = next_id
        next_id += 1
        comps = "".join(
            f'    <component objectid="{pid}" p:UUID="{_uuid(pid, "c")}" '
            f'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>\n'
            for pid, _, _, _ in part_ids
        )
        res_xml.append(
            f'  <object id="{asm_id}" p:UUID="{_uuid(asm_id)}" type="model">\n'
            f"   <components>\n{comps}   </components>\n  </object>\n"
        )
        faces = {pid: len(t) for (pid, _, _, _), (_, _, _, _, t) in zip(part_ids, parts)}
        # Per-object overrides live in model_settings.config, which is applied
        # whatever print preset the slicer resolves to. Values written into
        # project_settings.config are discarded when its preset name matches an
        # installed system preset, so anything that must survive belongs here.
        overrides = "".join(
            f'  <metadata key="{_attr(k)}" value="{_attr(v)}"/>\n'
            for k, v in (object_settings or {}).items()
        )
        cfg_xml.append(
            f' <object id="{asm_id}">\n'
            f'  <metadata key="name" value="{_attr(item.name)}"/>\n'
            f'  <metadata key="extruder" value="{base_ext}"/>\n'
            + overrides
            + f'  <metadata face_count="{sum(faces.values())}"/>\n'
            + "".join(
                f'  <part id="{pid}" subtype="{st}">\n'
                f'   <metadata key="name" value="{_attr(name)}"/>\n'
                f'   <metadata key="matrix" value="{IDENTITY_16}"/>\n'
                f'   <metadata key="source_file" value=""/>\n'
                f'   <metadata key="source_object_id" value="0"/>\n'
                f'   <metadata key="source_volume_id" value="0"/>\n'
                f'   <metadata key="source_offset_x" value="0"/>\n'
                f'   <metadata key="source_offset_y" value="0"/>\n'
                f'   <metadata key="source_offset_z" value="0"/>\n'
                f'   <metadata key="extruder" value="{ext}"/>\n'
                f'   <mesh_stat face_count="{faces[pid]}" edges_fixed="0" '
                f'degenerate_facets="0" facets_removed="0" '
                f'facets_reversed="0" backwards_edges="0"/>\n'
                f"  </part>\n"
                for pid, ext, st, name in part_ids
            )
            + " </object>\n"
        )
        build_xml.append(
            f'  <item objectid="{asm_id}" p:UUID="{_uuid(asm_id, "b")}" '
            f'transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/>\n'
        )
        plate_objects.append(asm_id)
        asm_xml.append(
            f'  <assemble_item object_id="{asm_id}" instance_id="0" '
            f'transform="1 0 0 0 1 0 0 0 1 0 0 0" offset="0 0 0"/>\n'
        )

    # Header field order and key spelling mirror FlashForge's bundled 3MFs
    # exactly. Thumbnail_* is deliberately omitted: it points at Metadata PNGs
    # we do not write, and a dangling reference is worse than none.
    app = _template_app(template) or ORCA_APP
    stamp = datetime.date.today().isoformat()
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE_NS}" '
        f'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
        f'xmlns:p="{PROD_NS}" requiredextensions="p">\n'
        f' <metadata name="Application">{app}</metadata>\n'
        f' <metadata name="BambuStudio:3mfVersion">{ORCA_3MF_VERSION}</metadata>\n'
        ' <metadata name="Copyright"></metadata>\n'
        f' <metadata name="CreationDate">{stamp}</metadata>\n'
        ' <metadata name="Description">3mf_scripts</metadata>\n'
        ' <metadata name="Designer"></metadata>\n'
        ' <metadata name="DesignerCover"></metadata>\n'
        ' <metadata name="DesignerUserId"></metadata>\n'
        ' <metadata name="License"></metadata>\n'
        f' <metadata name="ModificationDate">{stamp}</metadata>\n'
        ' <metadata name="Origin"></metadata>\n'
        ' <metadata name="Title">3mf_scripts</metadata>\n'
        " <resources>\n" + "".join(res_xml) + " </resources>\n"
        f' <build p:UUID="{_uuid(0, "build")}">\n'
        + "".join(build_xml) + " </build>\n</model>\n"
    )
    plate_xml = (
        " <plate>\n"
        '  <metadata key="plater_id" value="1"/>\n'
        '  <metadata key="plater_name" value=""/>\n'
        '  <metadata key="locked" value="false"/>\n'
        + "".join(
            "  <model_instance>\n"
            f'   <metadata key="object_id" value="{oid}"/>\n'
            '   <metadata key="instance_id" value="0"/>\n'
            "  </model_instance>\n"
            for oid in plate_objects
        )
        + " </plate>\n"
    )
    config = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        + "".join(cfg_xml)
        + plate_xml
        + " <assemble>\n" + "".join(asm_xml) + " </assemble>\n"
        + "</config>\n"
    )
    _zip_out(out_path, model, config, "Metadata/model_settings.config",
             template, colors, layer_height, first_layer_height, solid,
             app_version=_version_of(_template_app(template) or ORCA_APP))


def get_writer(flavor):
    return write_orca if flavor == "orca" else write_prusa
