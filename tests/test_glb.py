"""GLB input: container parsing, Y-up metres -> Z-up mm, colour sampling, dithered quantising,
and regression tests for the fixes made alongside it."""
import argparse
import io
import json
import os
import struct
import tempfile
import unittest

import numpy as np
from PIL import Image

from tdforge.core import glb, td3mf, tdcolor
from tdforge.tools import surfacecolor as sc

PALETTE = np.array([[255, 0, 0], [0, 0, 255], [255, 255, 255], [0, 0, 0]], float)


def _png(rgb_rows):
    im = Image.fromarray(np.array(rgb_rows, dtype=np.uint8))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def cube_arrays(size=0.01):
    """24-vertex cube centred on the origin, Y-up, triangles wound outward.
    Returns positions, normals-by-face, per-vertex face id, indices."""
    h = size / 2
    pos, face, idx = [], [], []
    for f, (axis, sign) in enumerate([(a, s) for a in range(3) for s in (1, -1)]):
        u, v = [i for i in range(3) if i != axis]
        corners = []
        for du, dv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            p = [0.0, 0.0, 0.0]
            p[axis], p[u], p[v] = sign * h, du * h, dv * h
            corners.append(p)
        base = len(pos)
        pos += corners
        face += [f] * 4
        for tri in ((0, 1, 2), (0, 2, 3)):
            a, b, c = (np.array(corners[i]) for i in tri)
            n = np.cross(b - a, c - a)
            want = np.zeros(3)
            want[axis] = sign
            order = tri if n @ want > 0 else (tri[0], tri[2], tri[1])
            idx += [base + i for i in order]
    return np.array(pos, np.float32), np.array(face), np.array(idx, np.uint16)


def build_glb(positions, indices, uvs=None, colors=None, texture_png=None, factor=None,
              extensions_required=None, node_extra=None):
    """Assemble a one-mesh GLB in memory."""
    chunks, views, accessors = bytearray(), [], []

    def add(arr, comp, typ, target=None):
        while len(chunks) % 4:
            chunks.append(0)
        views.append({"buffer": 0, "byteOffset": len(chunks), "byteLength": arr.nbytes})
        chunks.extend(arr.tobytes())
        acc = {"bufferView": len(views) - 1, "componentType": comp, "count": len(arr), "type": typ}
        if typ == "VEC3" and comp == 5126:
            acc["min"], acc["max"] = arr.min(0).tolist(), arr.max(0).tolist()
        accessors.append(acc)
        return len(accessors) - 1

    attrs = {"POSITION": add(positions.astype(np.float32), 5126, "VEC3")}
    if uvs is not None:
        attrs["TEXCOORD_0"] = add(uvs.astype(np.float32), 5126, "VEC2")
    if colors is not None:
        attrs["COLOR_0"] = add(colors.astype(np.float32), 5126, "VEC3")
    prim = {"attributes": attrs, "indices": add(indices.astype(np.uint16), 5123, "SCALAR")}
    doc = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
           "nodes": [dict({"mesh": 0}, **(node_extra or {}))],
           "meshes": [{"primitives": [prim]}], "accessors": accessors}
    mat = {"pbrMetallicRoughness": {}}
    if factor is not None:
        mat["pbrMetallicRoughness"]["baseColorFactor"] = list(factor)
    if texture_png is not None:
        while len(chunks) % 4:
            chunks.append(0)
        views.append({"buffer": 0, "byteOffset": len(chunks), "byteLength": len(texture_png)})
        chunks.extend(texture_png)
        doc["images"] = [{"bufferView": len(views) - 1, "mimeType": "image/png"}]
        doc["textures"] = [{"source": 0}]
        mat["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
    prim["material"] = 0
    doc["materials"] = [mat]
    doc["bufferViews"] = views
    doc["buffers"] = [{"byteLength": len(chunks)}]
    if extensions_required:
        doc["extensionsRequired"] = extensions_required
        doc["extensionsUsed"] = extensions_required
    js = json.dumps(doc).encode()
    js += b" " * (-len(js) % 4)
    bn = bytes(chunks) + b"\0" * (-len(chunks) % 4)
    total = 12 + 8 + len(js) + 8 + len(bn)
    return (struct.pack("<III", glb._GLB_MAGIC, 2, total) + struct.pack("<II", len(js), 0x4E4F534A)
            + js + struct.pack("<II", len(bn), 0x004E4942) + bn)


def quadrant_cube():
    """Cube whose top (+Y) is the red texel, bottom the black one, sides the blue one."""
    pos, face, idx = cube_arrays()
    tex = _png([[[255, 0, 0], [0, 0, 255]], [[255, 255, 255], [0, 0, 0]]])
    uv = np.zeros((len(pos), 2))
    for i, f in enumerate(face):      # faces are (axis, sign) pairs: Y is face 2 (+) and 3 (-)
        uv[i] = {2: (0.25, 0.25), 3: (0.75, 0.75)}.get(int(f), (0.75, 0.25))
    return build_glb(pos, idx, uvs=uv, texture_png=tex)


def write(tmp, name, data):
    p = os.path.join(tmp, name)
    with open(p, "wb") as fh:
        fh.write(data)
    return p


def label(item, res=0.5, depth=1.2, pattern="texture", dither=None):
    lo, hi = item.verts.min(0), item.verts.max(0)
    bounds = (lo[0], lo[1], hi[0], hi[1])
    edges = sc.layer_edges(hi[2], 0.2, 0.2)
    occ = sc.voxelize(item, bounds, res, edges)
    shell = sc.shell_mask(occ, res, 0.2, depth)
    a = argparse.Namespace(size=(hi[0] - lo[0], hi[1] - lo[1], hi[2]), pattern=pattern,
                           dither=dither, dither_strength=1.0)
    return sc.label_grid(occ, shell, bounds, res, edges, pattern, a, PALETTE, item), occ, edges


class Reader(unittest.TestCase):
    def test_orientation_units_and_placement(self):
        with tempfile.TemporaryDirectory() as d:
            item = glb.read_glb(write(d, "c.glb", quadrant_cube()), report=lambda *_: None)
        self.assertAlmostEqual(float(item.verts[:, 2].min()), 0.0, places=6)    # rests on the bed
        self.assertAlmostEqual(float(item.verts[:, 0].min()), 0.0, places=6)
        size = item.verts.max(0) - item.verts.min(0)
        np.testing.assert_allclose(size, [10, 10, 10], atol=1e-4)               # 0.01 m -> 10 mm

    def test_scale_to_and_bed_centering(self):
        with tempfile.TemporaryDirectory() as d:
            item = glb.read_glb(write(d, "c.glb", quadrant_cube()), scale_to=20,
                                bed=(200, 100), report=lambda *_: None)
        lo, hi = item.verts.min(0), item.verts.max(0)
        np.testing.assert_allclose(hi - lo, [20, 20, 20], atol=1e-4)
        self.assertAlmostEqual(float((lo[0] + hi[0]) / 2), 100, places=4)
        self.assertAlmostEqual(float((lo[1] + hi[1]) / 2), 50, places=4)
        self.assertAlmostEqual(float(lo[2]), 0.0, places=6)

    def test_winding_stays_outward(self):
        """A rotation, not a mirror: the signed volume must stay positive."""
        with tempfile.TemporaryDirectory() as d:
            item = glb.read_glb(write(d, "c.glb", quadrant_cube()), report=lambda *_: None)
        v, t = item.verts, item.tris
        vol = np.einsum("ij,ij->i", v[t[:, 0]], np.cross(v[t[:, 1]], v[t[:, 2]])).sum() / 6
        self.assertAlmostEqual(float(vol), 1000.0, delta=1.0)

    def test_node_transform_applies(self):
        pos, _, idx = cube_arrays()
        data = build_glb(pos, idx, node_extra={"scale": [2, 1, 1]})
        with tempfile.TemporaryDirectory() as d:
            item = glb.read_glb(write(d, "c.glb", data), report=lambda *_: None)
        np.testing.assert_allclose(item.verts.max(0) - item.verts.min(0), [20, 10, 10], atol=1e-4)

    def test_draco_is_refused_clearly(self):
        pos, _, idx = cube_arrays()
        data = build_glb(pos, idx, extensions_required=["KHR_draco_mesh_compression"])
        with tempfile.TemporaryDirectory() as d, self.assertRaises(SystemExit) as cm:
            glb.read_glb(write(d, "c.glb", data), report=lambda *_: None)
        self.assertIn("KHR_draco_mesh_compression", str(cm.exception))

    def test_not_a_gltf(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(SystemExit):
            glb.read_glb(write(d, "x.glb", b"hello world"), report=lambda *_: None)

    def test_missing_file(self):
        with self.assertRaises(SystemExit):
            td3mf.read_model("/nonexistent/model.glb")
        with self.assertRaises(SystemExit):
            td3mf.read_3mf("/nonexistent/model.3mf")


class Colour(unittest.TestCase):
    def test_texture_faces_get_their_texels(self):
        with tempfile.TemporaryDirectory() as d:
            item = glb.read_glb(write(d, "c.glb", quadrant_cube()), report=lambda *_: None)
        lab, occ, edges = label(item, depth=1.0)
        top = np.nonzero(occ.any(axis=(1, 2)))[0].max()
        cy = cx = lab.shape[1] // 2
        self.assertEqual(int(lab[top, cy, cx]), 0)                       # top face: red
        self.assertEqual(int(lab[0, cy, cx]), 3)                         # bottom face: black
        mid = lab.shape[0] // 2
        self.assertEqual(int(lab[mid, cy, 0]), 1)                        # side wall: blue
        self.assertEqual(int(lab[mid, 0, cx]), 1)

    def test_vertex_colours_without_texture(self):
        pos, face, idx = cube_arrays()
        cols = np.zeros((len(pos), 3))
        cols[:] = (0.0, 0.0, 1.0)                                         # linear blue
        cols[face == 2] = (1.0, 0.0, 0.0)                                 # top red
        with tempfile.TemporaryDirectory() as d:
            item = glb.read_glb(write(d, "c.glb", build_glb(pos, idx, colors=cols)),
                                report=lambda *_: None)
        lab, occ, _ = label(item, depth=1.0)
        top = np.nonzero(occ.any(axis=(1, 2)))[0].max()
        self.assertEqual(int(lab[top, lab.shape[1] // 2, lab.shape[2] // 2]), 0)
        self.assertEqual(int(lab[lab.shape[0] // 2, lab.shape[1] // 2, 0]), 1)

    def test_material_factor_only(self):
        pos, _, idx = cube_arrays()
        with tempfile.TemporaryDirectory() as d:
            item = glb.read_glb(write(d, "c.glb", build_glb(pos, idx, factor=(0, 0, 1, 1))),
                                report=lambda *_: None)
        lab, _, _ = label(item, depth=1.0)
        self.assertEqual(set(np.unique(lab[lab >= 0])), {1})

    def test_texture_pattern_needs_colours(self):
        v, t = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0.0]]), np.array([[0, 1, 2]])
        a = argparse.Namespace(dither=None, dither_strength=1.0, pattern="texture")
        cd = sc.Coords(np.zeros(1), np.zeros(1), np.zeros(1), (1, 1, 1),
                       item=td3mf.Item("t", v, t), res=1.0)
        with self.assertRaises(SystemExit):
            sc.p_texture(cd, a, 4, PALETTE)


class Dither(unittest.TestCase):
    def test_mid_grey_mixes_black_and_white(self):
        pal = tdcolor.srgb_to_linear(np.array([[0, 0, 0], [255, 255, 255]], float))
        target = np.full((20000, 3), 0.5)                                 # half-way in linear light
        ii = np.indices((40, 40, 13)).reshape(3, -1).T[:20000]
        idx, blend = tdcolor.quantize_dither(target, pal, ii)
        frac = idx.mean()
        self.assertAlmostEqual(float(frac), 0.5, delta=0.03)
        np.testing.assert_allclose(blend, 0.5, atol=1e-6)

    def test_strength_zero_is_nearest(self):
        pal = tdcolor.srgb_to_linear(PALETTE)
        tg = tdcolor.srgb_to_linear(np.array([[250, 10, 10], [10, 10, 240]], float))
        idx, _ = tdcolor.quantize_dither(tg, pal, np.zeros((2, 3), int), strength=0)
        self.assertEqual(list(idx), [0, 1])

    def test_exact_palette_colour_is_never_dithered(self):
        pal = tdcolor.srgb_to_linear(PALETTE)
        tg = np.tile(pal[2], (500, 1))
        ii = np.indices((10, 10, 5)).reshape(3, -1).T
        idx, _ = tdcolor.quantize_dither(tg, pal, ii)
        self.assertTrue((idx == 2).all())


class Fixes(unittest.TestCase):
    def test_voxelize_empty_mesh(self):
        item = td3mf.Item("e", np.zeros((3, 3)), np.zeros((0, 3), dtype=np.int64))
        occ = sc.voxelize(item, (0, 0, 1, 1), 0.5, sc.layer_edges(1.0, 0.2, 0.2))
        self.assertFalse(occ.any())

    def test_winding_accumulator_does_not_wrap(self):
        self.assertGreater(np.iinfo(np.int32).max, 40000)
        # 40k coincident faces over one column used to wrap an int16 accumulator to zero
        v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0.0]])
        t = np.array([[0, 1, 2], [1, 3, 2]] * 20000)
        item = td3mf.Item("x", v, t)
        edges = np.array([0.0, -1.0])
        occ = sc.voxelize(item, (0, 0, 1, 1), 0.5, edges)
        self.assertEqual(occ.shape[0], 1)

    def test_nonpositive_resolution_is_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            sc.main(["x.3mf", "-o", "o.3mf", "--palette", "#000,#fff", "--pattern", "stripes",
                     "--resolution", "0"])
        self.assertIn("--resolution", str(cm.exception))

    def test_settings_zero_is_not_ignored(self):
        raw = b'{"layer_height": "0.2", "min_layer_height": "0.0", "max_layer_height": "0.5"}'
        out = json.loads(td3mf._patch_settings(raw, layer_height=0.0, first_layer_height=0.0))
        self.assertEqual(float(out["layer_height"]), 0.0)
        self.assertEqual(float(out["initial_layer_print_height"]), 0.0)

    def test_catalog_load_skips_bad_entries(self):
        from tdforge.tools.polymaker import Catalog
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cat.json")
            with open(path, "w") as fh:
                json.dump({"version": 1, "products": [
                    {"hexes": ["#FF0000"], "td": 1.0},
                    {"sku": "BADTD", "hexes": ["#00FF00"], "td": "abc"},
                    {"sku": "NEGTD", "hexes": ["#00FF00"], "td": -2},
                    {"sku": "OK", "hexes": ["#0000FF"], "td": 1.5}]}, fh)
            cat = Catalog(path)
            cat.load()
        self.assertEqual(set(cat.products), {"BADTD", "NEGTD", "OK"})
        self.assertIsNone(cat.products["BADTD"].td)
        self.assertIsNone(cat.products["NEGTD"].td)
        self.assertEqual(cat.products["OK"].td, 1.5)

    def test_missing_image_is_a_clean_error(self):
        with self.assertRaises(SystemExit):
            tdcolor.open_image("/nonexistent/pic.png")


if __name__ == "__main__":
    unittest.main()
