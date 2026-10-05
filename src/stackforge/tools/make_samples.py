#!/usr/bin/env python3
"""Generate test models: a tiled 'chainmail' sheet and a domed badge (3MF), and a textured
globe (GLB) for trying the realistic-colour path without hunting for a model."""
import argparse, os, zipfile, math
import numpy as np

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def box(x0, y0, z0, x1, y1, z1):
    v = np.array([[x0,y0,z0],[x1,y0,z0],[x1,y1,z0],[x0,y1,z0],
                  [x0,y0,z1],[x1,y0,z1],[x1,y1,z1],[x0,y1,z1]], float)
    t = np.array([[0,2,1],[0,3,2],[4,5,6],[4,6,7],[0,1,5],[0,5,4],
                  [1,2,6],[1,6,5],[2,3,7],[2,7,6],[3,0,4],[3,4,7]])
    return v, t


def dome(cx, cy, r, hgt, n=24):
    verts = [[cx, cy, hgt]]
    for ring in range(1, n + 1):
        phi = (ring / n) * (math.pi / 2)
        rr, zz = r * math.sin(phi), hgt * math.cos(phi)
        for i in range(n * 2):
            th = 2 * math.pi * i / (n * 2)
            verts.append([cx + rr * math.cos(th), cy + rr * math.sin(th), zz])
    verts = np.array(verts)
    tris = []
    per = n * 2
    for i in range(per):
        tris.append([0, 1 + i, 1 + (i + 1) % per])
    for ring in range(n - 1):
        b0, b1 = 1 + ring * per, 1 + (ring + 1) * per
        for i in range(per):
            j = (i + 1) % per
            tris.append([b0 + i, b1 + i, b1 + j])
            tris.append([b0 + i, b1 + j, b0 + j])
    base = 1 + (n - 1) * per
    for i in range(1, per - 1):
        tris.append([base, base + i + 1, base + i])
    return verts, np.array(tris)


def merge(parts):
    vs, ts, off = [], [], 0
    for v, t in parts:
        vs.append(v)
        ts.append(t + off)
        off += len(v)
    return np.vstack(vs), np.vstack(ts)


def write(path, objs):
    res, build = [], []
    for oid, (v, t) in enumerate(objs, 1):
        vx = "".join(f'<vertex x="{a:.5g}" y="{b:.5g}" z="{c:.5g}"/>' for a, b, c in v)
        tx = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in t)
        res.append(f'<object id="{oid}" type="model"><mesh><vertices>{vx}</vertices>'
                   f'<triangles>{tx}</triangles></mesh></object>')
        build.append(f'<item objectid="{oid}"/>')
    model = (f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{CORE}">'
             f'<resources>{"".join(res)}</resources><build>{"".join(build)}</build></model>')
    ct = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)
    print(f"{path}: {len(objs)} objects, {sum(len(t) for _, t in objs)} triangles")


def globe_glb(path, radius_m=0.03, n_lat=48, n_lon=96, tex=(256, 512)):
    """A UV sphere (metres, Y-up) with a procedural ocean/land/ice texture, as a GLB."""
    import io, json, struct
    from PIL import Image
    th, tw = tex
    yy, xx = np.mgrid[0:th, 0:tw]
    lat = (0.5 - (yy + 0.5) / th) * math.pi
    lon = ((xx + 0.5) / tw - 0.5) * 2 * math.pi
    land = (np.sin(3 * lon + 1) * np.cos(2 * lat) + 0.6 * np.sin(5 * lat + 2 * lon)
            + 0.4 * np.cos(7 * lon - 3 * lat)) > 0.55
    img = np.zeros((th, tw, 3), np.uint8)
    img[:] = (20, 70, 170)
    img[land] = (50, 140, 60)
    img[land & (np.abs(lat) < 0.35)] = (190, 150, 70)
    img[np.abs(lat) > 1.25] = (245, 245, 250)
    png = io.BytesIO()
    Image.fromarray(img).save(png, "PNG")

    la, lo = np.meshgrid(np.linspace(0, 1, n_lat + 1), np.linspace(0, 1, n_lon + 1), indexing="ij")
    phi, theta = (0.5 - la) * math.pi, (lo - 0.5) * 2 * math.pi
    pos = np.stack([radius_m * np.cos(phi) * np.sin(theta), radius_m * np.sin(phi),
                    radius_m * np.cos(phi) * np.cos(theta)], -1).reshape(-1, 3).astype(np.float32)
    uv = np.stack([lo, la], -1).reshape(-1, 2).astype(np.float32)
    idx = []
    for i in range(n_lat):
        for j in range(n_lon):
            a = i * (n_lon + 1) + j
            b = a + n_lon + 1
            idx += [a, a + 1, b, a + 1, b + 1, b]
    idx = np.array(idx, np.uint32)
    # keep the winding outward whichever way the grid runs
    t = idx.reshape(-1, 3)
    n = np.cross(pos[t[:, 1]] - pos[t[:, 0]], pos[t[:, 2]] - pos[t[:, 0]])
    if (n * pos[t[:, 0]]).sum() < 0:
        idx = t[:, [0, 2, 1]].reshape(-1)
    blob = bytearray()
    views = []
    for arr in (pos, uv, idx, np.frombuffer(png.getvalue(), np.uint8)):
        while len(blob) % 4:
            blob.append(0)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": arr.nbytes})
        blob += arr.tobytes()
    doc = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "indices": 2,
                                       "material": 0}]}],
           "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
           "textures": [{"source": 0}], "images": [{"bufferView": 3, "mimeType": "image/png"}],
           "accessors": [
               {"bufferView": 0, "componentType": 5126, "count": len(pos), "type": "VEC3",
                "min": pos.min(0).tolist(), "max": pos.max(0).tolist()},
               {"bufferView": 1, "componentType": 5126, "count": len(uv), "type": "VEC2"},
               {"bufferView": 2, "componentType": 5125, "count": len(idx), "type": "SCALAR"}],
           "bufferViews": views, "buffers": [{"byteLength": len(blob)}]}
    js = json.dumps(doc).encode()
    js += b" " * (-len(js) % 4)
    blob += b"\0" * (-len(blob) % 4)
    total = 12 + 8 + len(js) + 8 + len(blob)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(js), 0x4E4F534A)
                 + js + struct.pack("<II", len(blob), 0x004E4942) + bytes(blob))
    print(f"{path}: textured globe, {len(idx) // 3} triangles")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=".", help="directory for fabric.3mf, badge.3mf and globe.glb")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    # chainmail: 40x40 tiles of 3mm on a 4mm pitch, 1.2mm thick
    parts = []
    pitch, tile, th = 4.0, 3.0, 1.2
    for iy in range(40):
        for ix in range(40):
            x, y = ix * pitch, iy * pitch
            parts.append(box(x, y, 0, x + tile, y + tile, th))
    write(os.path.join(args.out_dir, "fabric.3mf"), [merge(parts)])

    # badge: a 60mm dome on a 70mm plinth (tests non-flat top + occlusion)
    write(os.path.join(args.out_dir, "badge.3mf"), [merge([box(0, 0, 0, 70, 70, 3), dome(35, 35, 30, 12)])])
    globe_glb(os.path.join(args.out_dir, "globe.glb"))


if __name__ == "__main__":
    main()
