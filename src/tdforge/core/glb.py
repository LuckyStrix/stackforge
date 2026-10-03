"""Read a glTF/GLB model into a tdforge Item, keeping the colour information.

Hand-written on numpy + Pillow so the CLIs keep their small dependency set. Supports
what printable models actually use: triangle meshes in a node hierarchy, UVs with a
base-colour texture, vertex colours, and material base-colour factors. Compressed
geometry (Draco, meshopt) and KTX2 textures are refused with a clear message.

glTF is right-handed, Y-up, metres. 3MF is Z-up millimetres. (x, y, z) -> (x, -z, y)
rotates between them without mirroring, so triangle winding is preserved.
"""
from __future__ import annotations

import base64
import io
import json
import os
import struct
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

_GLB_MAGIC = 0x46546C67          # "glTF"
_COMPONENT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16,
              5125: np.uint32, 5126: np.float32}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
_UNSUPPORTED = ("KHR_draco_mesh_compression", "EXT_meshopt_compression", "KHR_texture_basisu")


@dataclass
class Material:
    """Base colour of one glTF material. `texture` is (H, W, 3) float sRGB 0..1 or None."""
    factor: np.ndarray = field(default_factory=lambda: np.ones(3))   # linear-ish factor, as stored
    texture: np.ndarray | None = None
    texcoord: int = 0


@dataclass
class Appearance:
    """Per-triangle colour source for a merged mesh.

    corner_uv   (T, 3, 2) UV at each triangle corner (zeros where the mesh has none)
    material    (T,) index into `materials`
    corner_col  (T, 3, 3) vertex colour, linear 0..1 (as the glTF spec defines it), or None
    """
    materials: list
    material: np.ndarray
    corner_uv: np.ndarray
    corner_col: np.ndarray | None = None


def is_glb_path(path: str) -> bool:
    return str(path).lower().endswith((".glb", ".gltf"))


def _srgb_to_lin(c):
    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _load_container(path):
    if not os.path.exists(path):
        raise SystemExit(f"{path}: no such file")
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:4] == b"glTF":
        magic, version, length = struct.unpack_from("<III", raw, 0)
        if version != 2:
            raise SystemExit(f"{path}: glTF version {version} is not supported (need 2)")
        off, doc, binchunk = 12, None, None
        while off + 8 <= min(len(raw), length):
            clen, ctype = struct.unpack_from("<II", raw, off)
            body = raw[off + 8: off + 8 + clen]
            if ctype == 0x4E4F534A:
                doc = json.loads(body.decode("utf-8"))
            elif ctype == 0x004E4942 and binchunk is None:
                binchunk = body
            off += 8 + ((clen + 3) & ~3)
        if doc is None:
            raise SystemExit(f"{path}: GLB has no JSON chunk")
        return doc, binchunk
    try:
        return json.loads(raw.decode("utf-8")), None
    except (ValueError, UnicodeDecodeError):
        raise SystemExit(f"{path}: not a glTF/GLB file") from None


class _Reader:
    def __init__(self, path, doc, binchunk):
        self.doc, self.dir = doc, os.path.dirname(os.path.abspath(path))
        self.buffers = []
        for i, b in enumerate(doc.get("buffers", [])):
            uri = b.get("uri")
            if uri is None:
                if i != 0 or binchunk is None:
                    raise SystemExit(f"{path}: buffer {i} has no data")
                self.buffers.append(binchunk)
            elif uri.startswith("data:"):
                self.buffers.append(base64.b64decode(uri.split(",", 1)[1]))
            else:
                from urllib.parse import unquote
                fp = os.path.join(self.dir, unquote(uri))
                if not os.path.exists(fp):
                    raise SystemExit(f"{path}: external buffer {uri} not found next to it")
                with open(fp, "rb") as fh:
                    self.buffers.append(fh.read())

    def view_bytes(self, vi):
        v = self.doc["bufferViews"][vi]
        buf = self.buffers[v.get("buffer", 0)]
        start = v.get("byteOffset", 0)
        return buf[start: start + v["byteLength"]], v.get("byteStride")

    def accessor(self, ai):
        a = self.doc["accessors"][ai]
        dt = np.dtype(_COMPONENT[a["componentType"]]).newbyteorder("<")
        n, nc = a["count"], _NCOMP[a["type"]]
        if "bufferView" in a:
            data, stride = self.view_bytes(a["bufferView"])
            off = a.get("byteOffset", 0)
            width = dt.itemsize * nc
            stride = stride or width
            if stride == width:
                arr = np.frombuffer(data, dtype=dt, count=n * nc, offset=off).reshape(n, nc)
            else:
                rows = np.frombuffer(data, dtype=np.uint8)
                idx = off + np.arange(n)[:, None] * stride + np.arange(width)[None, :]
                arr = rows[idx].copy().view(dt).reshape(n, nc)
        else:
            arr = np.zeros((n, nc), dtype=dt)
        if a.get("normalized"):
            if dt.kind == "u":
                arr = arr.astype(np.float64) / np.iinfo(dt).max
            else:
                arr = np.maximum(arr.astype(np.float64) / np.iinfo(dt).max, -1.0)
        return arr

    def image(self, ti):
        tex = self.doc["textures"][ti]
        if "source" not in tex:
            raise SystemExit("texture has no image source (compressed texture extensions "
                             "are not supported)")
        img = self.doc["images"][tex["source"]]
        if "bufferView" in img:
            data, _ = self.view_bytes(img["bufferView"])
        else:
            uri = img.get("uri", "")
            if uri.startswith("data:"):
                data = base64.b64decode(uri.split(",", 1)[1])
            else:
                from urllib.parse import unquote
                fp = os.path.join(self.dir, unquote(uri))
                if not os.path.exists(fp):
                    raise SystemExit(f"texture image {uri} not found next to the model")
                with open(fp, "rb") as fh:
                    data = fh.read()
        im = Image.open(io.BytesIO(bytes(data)))
        if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, im)
        return np.asarray(im.convert("RGB"), dtype=np.float64) / 255.0


def _node_matrix(node):
    if "matrix" in node:
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T   # column-major
    t = np.array(node.get("translation", [0, 0, 0]), dtype=np.float64)
    q = np.array(node.get("rotation", [0, 0, 0, 1]), dtype=np.float64)
    s = np.array(node.get("scale", [1, 1, 1]), dtype=np.float64)
    x, y, z, w = q / max(np.linalg.norm(q), 1e-12)
    rot = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    m = np.eye(4)
    m[:3, :3] = rot * s[None, :]
    m[:3, 3] = t
    return m


def _prim_triangles(rd, prim):
    mode = prim.get("mode", 4)
    if "indices" in prim:
        idx = rd.accessor(prim["indices"]).reshape(-1).astype(np.int64)
    else:
        idx = np.arange(len(rd.accessor(prim["attributes"]["POSITION"])), dtype=np.int64)
    if mode == 4:
        return idx[: len(idx) // 3 * 3].reshape(-1, 3)
    if mode == 5 and len(idx) >= 3:                       # triangle strip
        k = np.arange(len(idx) - 2)
        tri = np.stack([idx[k], idx[k + 1], idx[k + 2]], 1)
        odd = (k % 2) == 1
        tri[odd] = tri[odd][:, [1, 0, 2]]
        return tri
    if mode == 6 and len(idx) >= 3:                       # triangle fan
        k = np.arange(1, len(idx) - 1)
        return np.stack([np.full_like(k, idx[0]), idx[k], idx[k + 1]], 1)
    return np.zeros((0, 3), dtype=np.int64)               # points / lines carry no surface


def read_glb(path, scale_to=None, bed=None, report=print):
    """Load a glTF/GLB as a single merged `td3mf.Item` in print coordinates.

    `scale_to`: target size of the largest dimension in mm (default: metres -> mm).
    `bed`: (x, y) printable size in mm; the model is centred on it, otherwise its
    minimum corner is put at (0, 0). Either way it rests on z = 0.
    """
    from tdforge.core.td3mf import Item

    doc, binchunk = _load_container(path)
    bad = [e for e in doc.get("extensionsRequired", []) if e in _UNSUPPORTED]
    bad += [e for e in doc.get("extensionsUsed", []) if e in _UNSUPPORTED and e not in bad
            and e != "KHR_texture_basisu"]
    if bad:
        raise SystemExit(f"{path}: uses {', '.join(bad)}, which tdforge cannot read. Re-export "
                         f"the model without compression (e.g. glTF-Transform 'decompress', "
                         f"or Blender's glTF export with compression off).")
    rd = _Reader(path, doc, binchunk)

    materials, mat_cache = [Material()], {}

    def material_index(mi):
        if mi is None:
            return 0
        if mi in mat_cache:
            return mat_cache[mi]
        m = doc.get("materials", [])[mi] if mi < len(doc.get("materials", [])) else {}
        pbr = m.get("pbrMetallicRoughness", {})
        fac = np.array(pbr.get("baseColorFactor", [1, 1, 1, 1]), dtype=np.float64)[:3]
        mat = Material(factor=fac)
        bct = pbr.get("baseColorTexture")
        if bct is not None:
            mat.texture = rd.image(bct["index"])
            mat.texcoord = bct.get("texCoord", 0)
        materials.append(mat)
        mat_cache[mi] = len(materials) - 1
        return mat_cache[mi]

    verts_l, tris_l, uv_l, mat_l, col_l = [], [], [], [], []
    any_col = False
    nverts = 0

    def visit(ni, parent, depth=0):
        nonlocal nverts, any_col
        if depth > 64:
            raise SystemExit(f"{path}: node hierarchy is implausibly deep")
        node = doc["nodes"][ni]
        world = parent @ _node_matrix(node)
        if "mesh" in node:
            for prim in doc["meshes"][node["mesh"]].get("primitives", []):
                tris = _prim_triangles(rd, prim)
                if len(tris) == 0:
                    continue
                attrs = prim["attributes"]
                if "POSITION" not in attrs:
                    raise SystemExit(f"{path}: a primitive has no POSITION attribute")
                pos = rd.accessor(attrs["POSITION"]).astype(np.float64)
                pos = pos @ world[:3, :3].T + world[:3, 3]
                mat_i = material_index(prim.get("material"))
                tc = materials[mat_i].texcoord
                key = f"TEXCOORD_{tc}"
                uv = (rd.accessor(attrs[key]).astype(np.float64) if key in attrs
                      else np.zeros((len(pos), 2)))
                if "COLOR_0" in attrs:
                    col = rd.accessor(attrs["COLOR_0"]).astype(np.float64)[:, :3]
                    if col.max() > 1.0 + 1e-9:                      # un-normalised ints
                        col = col / 65535.0 if col.max() > 255 else col / 255.0
                    any_col = True
                else:
                    col = None
                if np.linalg.det(world[:3, :3]) < 0:                # mirrored node: keep winding outward
                    tris = tris[:, [0, 2, 1]]
                verts_l.append(pos)
                tris_l.append(tris + nverts)
                uv_l.append(uv[tris])
                col_l.append(col[tris] if col is not None else None)
                mat_l.append(np.full(len(tris), mat_i, dtype=np.int32))
                nverts += len(pos)
        for c in node.get("children", []):
            visit(c, world, depth + 1)

    scenes = doc.get("scenes", [])
    roots = (scenes[doc.get("scene", 0)].get("nodes", []) if scenes
             else list(range(len(doc.get("nodes", [])))))
    for r in roots:
        visit(r, np.eye(4))
    if not verts_l:
        raise SystemExit(f"{path}: no triangle geometry found")

    v = np.vstack(verts_l)
    t = np.vstack(tris_l)
    corner_uv = np.vstack(uv_l)
    material = np.concatenate(mat_l)
    corner_col = None
    if any_col:
        corner_col = np.vstack([c if c is not None else np.ones((len(m), 3, 3))
                                for c, m in zip(col_l, mat_l)])

    v = np.stack([v[:, 0], -v[:, 2], v[:, 1]], axis=1)       # Y-up -> Z-up, no mirroring
    size = v.max(0) - v.min(0)
    k = 1000.0
    if scale_to:
        if size.max() <= 0:
            raise SystemExit(f"{path}: model has zero size")
        k = scale_to / size.max()
    v = v * k
    lo, hi = v.min(0), v.max(0)
    shift = np.array([-lo[0], -lo[1], -lo[2]])
    if bed:
        shift[0] = bed[0] / 2 - (lo[0] + hi[0]) / 2
        shift[1] = bed[1] / 2 - (lo[1] + hi[1]) / 2
    v = v + shift
    dims = hi - lo
    report(f"  glb: {len(t)} triangles, {len(materials) - 1} material(s), "
           f"{int(sum(m.texture is not None for m in materials))} texture(s); "
           f"size {dims[0]:.1f} x {dims[1]:.1f} x {dims[2]:.1f} mm "
           f"({'scaled to ' + format(scale_to, 'g') + ' mm' if scale_to else 'metres -> mm'})")
    if not scale_to and dims.max() > 400:
        report("  ! that is over 400 mm; if the model is not in metres, use --scale-to")
    if not scale_to and dims.max() < 5:
        report("  ! that is under 5 mm; if the model is not in metres, use --scale-to")

    item = Item(name=os.path.splitext(os.path.basename(path))[0], verts=v, tris=t)
    item.appearance = Appearance(materials, material, corner_uv, corner_col)
    return item


# --------------------------------------------------------------------------
# colour sampling
# --------------------------------------------------------------------------


def _sample_texture(tex, uv):
    """Bilinear lookup with repeat wrapping; uv (N,2), v=0 at the image top (glTF)."""
    h, w = tex.shape[:2]
    u = (uv[:, 0] % 1.0) * w - 0.5
    vv = (uv[:, 1] % 1.0) * h - 0.5
    x0, y0 = np.floor(u).astype(int), np.floor(vv).astype(int)
    fx, fy = (u - x0)[:, None], (vv - y0)[:, None]
    x0w, x1w = x0 % w, (x0 + 1) % w
    y0w, y1w = y0 % h, (y0 + 1) % h
    top = tex[y0w, x0w] * (1 - fx) + tex[y0w, x1w] * fx
    bot = tex[y1w, x0w] * (1 - fx) + tex[y1w, x1w] * fx
    return top * (1 - fy) + bot * fy


def surface_samples(item, spacing, max_points=6_000_000):
    """Points on the surface with their linear-light colours: ((N,3) mm, (N,3) linear RGB).

    Triangles are sampled on a barycentric grid fine enough that neighbouring points
    are about `spacing` apart, then coloured from the texture, vertex colours and
    material factor. Spacing coarsens if the cap would be exceeded.
    """
    ap = item.appearance
    if ap is None:
        raise SystemExit("this model carries no colour information (not a GLB)")
    v, t = item.verts, item.tris
    a, b, c = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    longest = np.maximum.reduce([np.linalg.norm(b - a, axis=1), np.linalg.norm(c - b, axis=1),
                                 np.linalg.norm(a - c, axis=1)])

    def counts(sp):
        n = np.ceil(longest / sp).astype(int) + 1             # subdivisions along an edge
        return n, (n + 1) * (n + 2) // 2

    n_div, n_pts = counts(spacing)
    while n_pts.sum() > max_points:
        spacing *= 1.5
        n_div, n_pts = counts(spacing)

    pts, cols = [], []
    for nd in np.unique(n_div):
        sel = np.nonzero(n_div == nd)[0]
        ii, jj = np.meshgrid(np.arange(nd + 1), np.arange(nd + 1), indexing="ij")
        keep = (ii + jj) <= nd
        w1, w2 = ii[keep] / nd, jj[keep] / nd
        w0 = 1.0 - w1 - w2
        bary = np.stack([w0, w1, w2], 1)                      # (K, 3)
        for s in range(0, len(sel), 20000):
            tri = sel[s:s + 20000]
            p = np.einsum("kc,tcd->tkd", bary, np.stack([a[tri], b[tri], c[tri]], 1))
            uv = np.einsum("kc,tcd->tkd", bary, ap.corner_uv[tri])
            col = np.empty(p.shape[:2] + (3,))
            mats = ap.material[tri]
            for mi in np.unique(mats):
                rows = mats == mi
                m = ap.materials[mi]
                if m.texture is not None:
                    c_lin = _srgb_to_lin(_sample_texture(m.texture, uv[rows].reshape(-1, 2)))
                    c_lin = c_lin.reshape(-1, p.shape[1], 3)
                else:
                    c_lin = np.ones((int(rows.sum()), p.shape[1], 3))
                col[rows] = c_lin * m.factor                   # glTF factors are linear
            if ap.corner_col is not None:
                vc = np.einsum("kc,tcd->tkd", bary, ap.corner_col[tri])
                col = col * vc
            pts.append(p.reshape(-1, 3))
            cols.append(col.reshape(-1, 3))
    return np.vstack(pts), np.clip(np.vstack(cols), 0.0, 1.0)
