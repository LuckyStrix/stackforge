# Working notes

**The rule:** stackforge's colour maths (`tools/stackforge.py`, `core/tdcolor.py`) is pure numpy; all 3MF
I/O goes through `core/td3mf.py`. The GUI is `tdforge-gui` (Qt, `gui/qt/`, theme in `gui/qt/theme.py`); `tdforge-gui-classic`
(tk, the rest of `gui/`, theme `gui/theme.py`) survives only for the filament editor, which is not
ported yet. Both wrap the CLIs. Forms are
generated from each tool's `build_parser()` by `gui/argform` (spec/argv need no tkinter; the
semantic widget kinds and project bindings are in `argform/overrides.py`). A new flag needs no GUI
change; `tests/test_argform.py` fails if an override names a flag that no longer exists.
Code is the `tdforge` package in `src/tdforge/` (`core/`, `tools/`, `gui/`); `pip install -e ".[dev]"`, then `pytest`. `ruff check src tests` is
clean and CI runs both. PySide6 is the `gui` extra; the CLIs need only numpy/Pillow/scipy.

## Load-bearing

- **Layer grid must match the slicer profile.** One modifier box per colour layer, mid-layer.
  `first_layer + (n-1)*layer`, not `n*layer`. Wrong grid => colour on alternating layers only.
  Both values come from `--template` (`td3mf.template_layer_settings`).
- **`--template` is required for Flash Studio.** Orca-derived slicers drop `model_settings.config`
  if the `Application` version is older than the running slicer. Read it from the template.
- **Solid infill is baked in** (`sparse_infill_density 100%`); sparse infill hides colour.
- **Opaque base.** Gamut assumes it; stackforge warns when the base passes >1% light.
- **`tests/test_slice.py` runs that slice automatically** when Flash Studio and the template exist
  (`FLASHSTUDIO_RUN`, `FLASHSTUDIO_TEMPLATE` override the paths; `TDFORGE_SKIP_SLICER=1` disables).
  It checks layer count and that all four tools are used.
- **Slice headlessly to verify, don't guess.** `~/Downloads/FlashStudio-1.7.8/run.sh --datadir
  <scratch> --slice 1 --outputdir <dir> file.3mf` (template: `~/Downloads/ffSample2.3mf`, 4 slots,
  0.12/0.25). Checked this way (2026-09-30): modifier extruder overrides ARE honoured (100% of
  extruded pixels on the designed tool).
- **One-pixel features don't print at 0.4 mm.** Each modifier region gets walls; a region one
  nozzle-width across gets no plastic (5-9% of colour pixels lost at `--resolution 0.4`, 0.1% at
  0.6). Default is 0.6 (`MIN_FEATURE_MM`). Dithers are mostly one-pixel features.
- **td is the reflectance-fit kind** (`tools/calibrate.py`: light crosses each layer twice). munki's
  transmission td is single-pass, ~2x for a clear absorber; never paste it in as `td_rgb`.

## Bite

- `filaments.json` tds are mostly estimates; never present them as measured. The shipped
  `filaments.json` and `polymaker_catalog.json` live in `src/tdforge/data/`; `core/paths.py`
  prefers a copy in the cwd, else the packaged one (so the defaults write into the repo when run
  from elsewhere with an editable install).
- `tools/polymaker.py` fetches from the network only in `refresh` (30 s timeout, 3 tries, cache
  untouched on failure); the catalogue is cached in `polymaker_catalog.json`.
- **surfacecolor voxelisation uses a winding number from above, not parity.** Overlapping open
  shells (the badge fixture) flip parity and leave a hollow; `tests/test_surfacecolor.py` covers it.
- **surfacecolor `--expr` goes through an AST whitelist** (`compile_expr`) before `eval`: no
  attributes, subscripts, lambdas, strings. Add a function by adding it to `_EXPR_NAMES`.
- **Dither gains are simulated.** Blurred dE improves most with shallow stacks, but the slicer
  drops the one-pixel features a dither is made of (see above). `mix_pairs` in `stackforge.py`
  averages two gamut states; nudging the target before the query changed nothing (the old
  implementation). Floyd diffusing error in linear light was measured too: not adopted.
- Unverified: PrusaSlicer honouring the per-object `layer_height`/`fill_density` that
  `--flavor prusa` writes (no Prusa profile is carried over, so nothing else sets them).
- **`tools/munki.py` is unverified on hardware.** Its spotread session is copied from calibration-suite (do not
  import it, the repos are independent). All-zero XYZ is refused: a stale ColorMunki dial prints zeros.
- **`--nospos` = patched Argyll with no dial check** (`~/.local/bin/argyll-nospos`, unconditional
  patch; `ARGYLL_NOSPOS=1` is only the wrapper's marker). Always go through the wrapper, never the
  patched binary's path: the wrapper isolates its calibration cache. The white-paper, repeat and
  wedge-reversal checks in munki.py stand in for the check the patch removed; keep them.
