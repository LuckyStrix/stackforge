# Plan: `surfacecolor` — colouring arbitrary 3D models

Where `stackforge` makes a flat plaque and `topdeco` paints what faces upward,
this covers the remaining case: **an existing 3D model, coloured anywhere on
its surface**, from an image, a procedural pattern, or a brush.

Status: build-order steps 1-3 are implemented in `surfacecolor.py` (patterns, wrapped images, shell
mask via distance transform). Step 4, the painting window, is not built. Later findings: the inside test
uses a winding number rather than parity, because real 3MFs contain overlapping open shells.

---

## 1. The core decision: volumetric, not surface-parametric

The obvious approach is UV mapping — unwrap the mesh, paint the texture, bake
back. It is the wrong tool here, for three reasons:

- **Arbitrary 3MFs have no UVs.** Almost nothing you download is unwrapped,
  and auto-unwrapping is a hard problem with ugly failure modes on the
  organic, high-poly meshes that make good prints.
- **Colour resolution would be capped by tessellation.** A 200-triangle sphere
  cannot carry a fine checker without subdividing first.
- **We do not need a surface representation at all.** Modifier volumes already
  work by *intersecting solids with the object*. The slicer does the geometry.

So the generalization is: **partition space, and let the slicer intersect.**
The model is never modified, never unwrapped, never even inspected beyond its
bounding box. This is the same insight that makes `topdeco` work, taken to its
conclusion — and it means one implementation covers spheres, busts, terrain
tiles and chainmail identically.

The cost is that colour resolution is set by voxel size rather than by the
mesh, which is a knob rather than a limitation.

## 2. Pipeline

```
3MF ──► bbox ──► voxel grid ──► pattern fn ──► filament index per voxel
                                                      │
                          shell mask (optional) ◄──────┤
                                                      ▼
                              greedy_rects per Z layer ──► boxes
                                                      ▼
                                 modifier volumes ──► 3MF out
```

Steps 1, 5 and 6 already exist in `td3mf`. Step 4 is `tdcolor.quantize`. The
genuinely new code is the pattern evaluator and the shell mask — realistically
**~250 lines**.

### 2.1 Voxel grid

Grid at `--resolution` (default 0.4 mm) over the model bbox. Evaluate the
pattern at voxel centres, vectorized over the whole grid.

A 100 mm model at 0.4 mm is 250³ = 15.6M voxels — fine as an `int8` array
(16 MB), and the pattern functions are all vectorized numpy.

### 2.2 Shell masking (optional but wanted)

Colour is only visible near the surface, so filling the interior with modifiers
wastes tool changes on infill.

- Voxelize the mesh to an inside/outside grid (ray parity along Z, vectorized
  per column — the same rasterizer `topdeco` already has, run per layer).
- `scipy.ndimage.binary_erosion` by `--depth / resolution` voxels.
- Shell = occupied AND NOT eroded.

Ship without this first (`--full-depth`, colour goes all the way through, which
for a checker is arguably nicer anyway), then add it as an optimization.

### 2.3 Emission

Per Z layer, `greedy_rects` per filament, boxes in the middle half of the
layer, one modifier volume per filament. Byte-for-byte the `stackforge`
approach.

## 3. Patterns

A registry of `fn(x, y, z) -> filament_index`, all vectorized. `x/y/z` are
voxel-centre arrays; `r/theta/phi` derived and passed too.

| pattern | parameters | notes |
|---|---|---|
| `checker3d` | `--scale` | `(floor(x/s)+floor(y/s)+floor(z/s)) % 2`. ~10 lines, works on any shape |
| `checker-sphere` | `--lat --lon --center` | `(floor(θ/π·lat) + floor(φ/2π·lon)) % 2`. The beach-ball look |
| `stripes` | `--axis --period` | trivial, surprisingly effective on vases |
| `image-cylindrical` | `--image --axis` | wrap around; for busts, vases, columns |
| `image-spherical` | `--image` | equirectangular → globes and planets |
| `image-planar` | `--image --axis` | what `topdeco` does, generalized to any axis |
| `gradient` | `--axis --stops` | hypsometric tinting for terrain |
| `expr` | `--expr "..."` | arbitrary numpy expression, the escape hatch |

`checker3d` and `checker-sphere` genuinely differ on a sphere: the lattice
version distorts toward the poles differently. Both are worth having.

**Priority given the NASA library: `image-spherical`.** NASA publishes Mars,
Moon and Earth maps as equirectangular images, which makes them directly
printable onto any sphere with no further work.

### CLI shape

```sh
surfacecolor.py sphere.3mf -o out.3mf --filaments white,black \
    --pattern checker-sphere --lat 8 --lon 16

surfacecolor.py sphere.3mf -o mars.3mf \
    --filaments white,red,orange,black \
    --pattern image-spherical --image mars_equirect.jpg --dither floyd

surfacecolor.py bust.3mf -o out.3mf --filaments white,black \
    --pattern expr --expr "(sin(x/4) * cos(z/4)) > 0"
```

Plus the shared flags: `--resolution --depth --layer-height --flavor
--part-type --preview --db`.

## 4. The painting window

The more interesting half, and the reason to build the volumetric core first
— **it is the same engine with an interactive front end.**

### Interaction

Render the model in an orthographic view. The user paints with a brush in
*screen* space; each stroke is unprojected to a ray, and voxels within the
brush radius of the first surface hit along that ray get tagged. That is a
depth-buffer lookup, which `topdeco.rasterize_top` already computes — just
generalized to an arbitrary view matrix rather than always +Z.

Orbit with the mouse, paint from any angle. No UVs, no seams, no unwrap. The
paint lives in the voxel grid, so it is view-independent and strokes from
different angles compose naturally.

### The filament-picking part

This is the piece worth getting right, and it inverts `stackforge`'s ranking.

The user paints with **arbitrary colours** — a colour wheel, not a filament
list. Then:

1. Collect the painted colour set, weighted by voxel count.
2. Search the filament database for the `--slots` subset minimising weighted
   dE against that palette. Exactly `stackforge.rank_subsets`, with the
   painted colours substituted for sampled image pixels.
3. Show the loadout, and re-render the model in **achievable** colours so the
   user sees the compromise immediately.

So the flow is: paint what you want → the tool tells you which spools to load
→ you see what it will actually look like before slicing. That is the natural
GUI expression of what the ranker already does headlessly.

For a *surface* colour the stack question mostly disappears — near the surface
you want the top layers to be one filament, so it reduces to nearest-filament
matching, not a stack solve. Optionally reuse the full `Gamut` for a thin
stack near the surface to widen the palette, but that is a later refinement
and probably not worth it on curved geometry where layer count varies with
surface angle.

### Reuse

| need | already exists |
|---|---|
| 3MF read, world-space bake | `td3mf.read_3mf` |
| depth buffer from a view | `topdeco.rasterize_top` (generalize the axis) |
| nearest filament in Lab | `tdcolor`, `Gamut.query` |
| subset ranking | `stackforge.rank_subsets` |
| box emission, 3MF write | `td3mf` |
| threading, previews, DB panel | `stackforge_gui` |

The painting window is mostly **assembly**, provided the volumetric core lands
first.

## 5. Build order

1. **`surfacecolor.py` with `checker3d` and `expr`.** Smallest thing that
   proves the voxel→modifier path on a real model. Half a day.
2. **`image-spherical` and `image-cylindrical`.** Unlocks the NASA maps.
3. **Shell masking.** Optimization; cuts tool changes on infill.
4. **Painting window.** Only after 1–3 are trustworthy.

## 6. Risks

- **Geometry volume.** A 100 mm model at 0.4 mm with a busy pattern could
  produce millions of boxes. `stackforge` already hits 2M triangles on a
  120 mm plaque. Mitigation: shell masking, coarser `--resolution` for the
  colour grid than for the print, and the existing box-count warning.
- **Thin features.** Voxels smaller than a wall thickness may not map cleanly
  onto extrusions; a modifier thinner than one perimeter may be ignored by the
  slicer. Needs a real test print on something with fine detail.
- **Curved surfaces and layer count.** On a near-vertical wall, `--depth`
  measured along Z is not depth into the surface. Genuine, and the reason to
  measure the shell with a distance transform rather than by Z. Worth doing
  properly in step 3 rather than faking it.
- **Orca modifier limits.** Unknown how many modifier volumes Orca-FlashForge
  handles gracefully. Same open question as everything else here — resolved by
  the first real slice.
