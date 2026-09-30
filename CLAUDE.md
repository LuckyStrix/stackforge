# Working notes

**The rule:** stackforge's colour maths (`stackforge.py`, `tdcolor.py`) is pure numpy; all 3MF
I/O goes through `td3mf.py`. GUIs (`*_gui.py`) only wrap the CLIs and share `guikit.py`.

## Load-bearing

- **Layer grid must match the slicer profile.** One modifier box per colour layer, mid-layer.
  `first_layer + (n-1)*layer`, not `n*layer`. Wrong grid => colour on alternating layers only.
  Both values come from `--template` (`td3mf.template_layer_settings`).
- **`--template` is required for Flash Studio.** Orca-derived slicers drop `model_settings.config`
  if the `Application` version is older than the running slicer. Read it from the template.
- **Solid infill is baked in** (`sparse_infill_density 100%`); sparse infill hides colour.
- **Opaque base.** Gamut assumes it; stackforge warns when the base passes >1% light.
- **Slice headlessly to verify, don't guess.** `~/Downloads/FlashStudio-1.7.8/run.sh --datadir
  <scratch> --slice 1 --outputdir <dir> file.3mf` (template: `~/Downloads/ffSample2.3mf`, 4 slots,
  0.12/0.25). Checked this way (2026-09-30): modifier extruder overrides ARE honoured (100% of
  extruded pixels on the designed tool).
- **One-pixel features don't print at 0.4 mm.** Each modifier region gets walls; a region one
  nozzle-width across gets no plastic (5-9% of colour pixels lost at `--resolution 0.4`, 0.1% at
  0.6). Default is 0.6 (`MIN_FEATURE_MM`). Dithers are mostly one-pixel features.
- **td is the reflectance-fit kind** (`calibrate.py`: light crosses each layer twice). munki's
  transmission td is single-pass, ~2x for a clear absorber; never paste it in as `td_rgb`.

## Bite

- `filaments.json` tds are mostly estimates; never present them as measured.
- `polymaker.py` fetches from the network; the catalogue is cached in `polymaker_catalog.json`.
- **surfacecolor voxelisation uses a winding number from above, not parity.** Overlapping open
  shells (the badge fixture) flip parity and leave a hollow; `tests/test_surfacecolor.py` covers it.
- **surfacecolor `--expr` is `eval`.** Builtins stripped, dunders refused; still only for trusted input.
- **Dither gains are simulated.** Blurred dE improves most with shallow stacks, but the slicer
  drops the one-pixel features a dither is made of (see above). `mix_pairs` in `stackforge.py`
  averages two gamut states; nudging the target before the query changed nothing (the old
  implementation). Floyd diffusing error in linear light was measured too: not adopted.
- Unverified: PrusaSlicer honouring the per-object `layer_height`/`fill_density` that
  `--flavor prusa` writes (no Prusa profile is carried over, so nothing else sets them).
- **munki.py is unverified on hardware.** Its spotread session is copied from calibration-suite (do not
  import it, the repos are independent). All-zero XYZ is refused: a stale ColorMunki dial prints zeros.
