#!/usr/bin/env python3
"""Generate test 3MFs: a tiled 'chainmail' sheet and a domed badge."""
import sys, zipfile, math
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
        vs.append(v); ts.append(t + off); off += len(v)
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


def main():
    # chainmail: 40x40 tiles of 3mm on a 4mm pitch, 1.2mm thick
    parts = []
    pitch, tile, th = 4.0, 3.0, 1.2
    for iy in range(40):
        for ix in range(40):
            x, y = ix * pitch, iy * pitch
            parts.append(box(x, y, 0, x + tile, y + tile, th))
    write("fabric.3mf", [merge(parts)])

    # badge: a 60mm dome on a 70mm plinth (tests non-flat top + occlusion)
    write("badge.3mf", [merge([box(0, 0, 0, 70, 70, 3), dome(35, 35, 30, 12)])])


if __name__ == "__main__":
    main()
