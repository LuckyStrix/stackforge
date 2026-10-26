# Working notes

**The rule:** plaque's colour maths (`tools/plaque.py`, `core/colormath.py`) is pure numpy; all 3MF
I/O goes through `core/threemf.py`. The GUI is `stackforge` (Qt, all of `gui/`; one module per tab in `gui/tabs/`, filament editor in
`gui/filaments/`, shared theme in `gui/theme.py`) and wraps the CLIs. Forms are
generated from each tool's `build_parser()` by `gui/argform` (spec, argv and overrides need no GUI toolkit; the
semantic widget kinds and project bindings are in `argform/overrides.py`). A new flag needs no GUI
change; `tests/test_argform.py` fails if an override names a flag that no longer exists.
Code is the `stackforge` package in `src/stackforge/` (`core/`, `tools/`, `gui/`); `pip install -e ".[dev]"`, then `pytest`. `ruff check src tests` is
clean and CI runs both. PySide6 is the `gui` extra; the CLIs need only numpy/Pillow/scipy.

## Load-bearing

- **Layer grid must match the slicer profile.** One modifier box per colour layer, mid-layer.
  `first_layer + (n-1)*layer`, not `n*layer`. Wrong grid => colour on alternating layers only.
  Both values come from `--template` (`threemf.template_layer_settings`).
- **`--template` is required for Flash Studio.** Orca-derived slicers drop `model_settings.config`
  if the `Application` version is older than the running slicer. Read it from the template.
- **Solid infill is baked in** (`sparse_infill_density 100%`); sparse infill hides colour.
- **Opaque base.** Gamut assumes it; `tools/plaque.py` warns when the base passes >1% light.
- **`tests/test_slice.py` runs that slice automatically** when Flash Studio and the template exist
  (`FLASHSTUDIO_RUN`, `FLASHSTUDIO_TEMPLATE` override the paths; `STACKFORGE_SKIP_SLICER=1` disables).
  It checks layer count and that all four tools are used.
- **Slice headlessly to verify, don't guess.** `~/Downloads/FlashStudio-1.7.8/run.sh --datadir
  <scratch> --slice 1 --outputdir <dir> file.3mf` (template: `~/Downloads/ffSample2.3mf`, 4 slots,
  0.12/0.25). Checked this way (2026-09-30): modifier extruder overrides ARE honoured (100% of
  extruded pixels on the designed tool).
- **The headless slice ignores object/part extruders** (Flash Studio 1.7.13, 2026-10-17): every
  object prints on extruder 1, whatever model_settings says; modifiers are honoured. The GUI
  honours them (the over-black wedges read dark on their bare-base patches). So a headless
  check is only valid with each base on extruder 1; paint puts the base there when it picks
  a subset (a full list keeps the user's slot order).
- **One-pixel features don't print at 0.4 mm** (plaque, classic walls; paint writes Arachne). Each modifier region gets walls; a region one
  nozzle-width across gets no plastic (5-9% of colour pixels lost at `--resolution 0.4`, 0.1% at
  0.6). Default is 0.6 (`MIN_FEATURE_MM`). Dithers are mostly one-pixel features.
- **Wedge step n carries n layers of filament** (`layer_height` each): the thick first layer is
  in the base under the steps. `fit_td` models exactly that; chips (no base) are the ones on
  `first + (n-1)*layer`. Wedges and chips are written solid, like the plaque.
- **Wedges are written concentric** (`calibrate.concentric_settings`, per-object: top surface and
  internal solid infill). Line fills turn 90 degrees a layer and the meter sees it: the 2026-10-12 orange
  wedges zigzag ~7 dE between odd and even steps. Slice-checked (every top/solid move axis-aligned);
  that it removes the zigzag is unverified until a concentric wedge is measured.
- **Wedge sheet -> readings round trip** (`core/wedgesheet.py`, plain JSON): calibrate writes
  `*.sheet.json` beside the 3MF, measure `--sheet` writes `*.readings.json`, `stackforge-spectral fit
  --readings` consumes it (guided "Load readings" opens that fit in the Spectra tab). The files
  cross machines, so they carry labels and colours, not just ids. Slot 0 of each wedge is a bare-base patch; its reading replaces the library colour.
- **td is the reflectance-fit kind** (`tools/calibrate.py`: light crosses each layer twice). measure's
  transmission td is single-pass, ~2x for a clear absorber; never paste it in as `td_rgb`.
- **Calibration is spectral only on this branch.** `stackforge-calibrate fit` (the RGB td fit) is
  gone from the CLI and the Guided page: `main` exits with a pointer to `stackforge-spectral fit`.
  `fit_td` stays as a tested library function with no entry point; do not wire it back in.
  `plaque --optics` and the Designer default to `spectral`; `rgb` has to be asked for (the RGB
  tests pass `--optics rgb`). One exception: the Designer opens on `rgb` when no filament in
  the library is spectrally calibrated (a fresh install), and a saved preset still wins.
- **The bundled app has no python** (`packaging/`, PyInstaller one-file). The GUI's tool
  subprocess is the executable again with `--run-tool <tool>` (`runner.tool_command`,
  dispatched in `packaging/launcher.py` before Qt loads); tools are found by name, so the
  spec collects every `stackforge` submodule. Built and smoke-tested on Windows only
  (2026-10-25); the Linux and macOS builds in `release.yml` are untested.
- **Spectral optics take calibrated filaments only** (`core/spectral.py`, `docs/spectral.md`).
  `plaque --optics spectral` and `SpectralGamut` refuse any filament or base without a
  `Filament.spectral` block (Kubelka-Munk K, S per 10 nm band, 380-730), which only
  `stackforge-spectral fit` writes; it is trustworthy when every band changes on some wedge (`signal`, MIN_SIGNAL:
  white over black alone is enough, a colour needs white and black), and warns otherwise. It never touches color/td/td_rgb/provenance.
  `Gamut` is split into `_setup`/`_add_layer`/`_linear` hooks: the RGB path must stay byte-identical
  (checked against the pre-refactor build). CIE tables in `data/cie/` are the CIE's files, md5-tested.
- **Spectrum scale is unverified on hardware** (Argyll documents 0..100; 0..1 is warned about): decided per readings file (never per patch: a dark
  patch on 0..100 reads below 1), cross-checked against each base patch's XYZ. No fit check refuses `--write`
  (2026-10-13): weak signal, <4 steps, scale mismatch and reversed steps print a NOT TRUSTWORTHY box and are saved in `spectral["warnings"]`. `demo-*` filaments are SYNTHETIC: watermarked, never written to a library by the viewer.
- **No exception may escape a `paintEvent`**: on Windows PySide it kills the process (access
  violation). `gui/spectrum_chart.py` draws the error instead. numpy 2 keeps `ceil()` of an int integral.

## Bite

- `filaments.json` tds are mostly estimates; never present them as measured. The shipped
  `filaments.json` and `polymaker_catalog.json` live in `src/stackforge/data/`. The filament
  library users edit is `core/paths.user_data_path`: `./filaments.json` if present, else
  `~/.config/stackforge/filaments.json`, seeded from the packaged copy (never written to).
  `tests/conftest.py` points `XDG_CONFIG_HOME` at a temp dir. The catalogue still uses the
  packaged copy (`data_path`).
- `tools/polymaker.py` fetches from the network only in `refresh` (30 s timeout, 3 tries, cache
  untouched on failure); the catalogue is cached in `polymaker_catalog.json`.
- **paint voxelisation uses a winding number from above, not parity.** Overlapping open
  shells (the badge fixture) flip parity and leave a hollow; `tests/test_paint.py` covers it.
- **GLB input is unverified on a print.** `core/glb.py` (hand-written reader, numpy + Pillow) keeps UVs,
  textures and vertex colours on `Item.appearance`; `paint --pattern texture` samples the surface
  densely into a KD-tree and dithers (`colormath.quantize_dither`, 3D R3 ordered dither, simulated gains only).
  Y-up metres -> Z-up mm is `(x,-z,y)` (a rotation: do not mirror, winding must stay outward). glTF
  vertex colours and `baseColorFactor` are linear; textures are sRGB. Slice-checked (all four tools used),
  never printed.
- **paint ranks (set, base) pairs** (`rank_sets`, `pick_best`): colour patterns are a target fn
  (`TARGETS`) then `_pick`, so every item is voxelised before the set is chosen.
  `printed_palette`: calibrated filaments are `--depth` mm over the base (KM); uncalibrated keep
  the swatch. Swatches are badly off (light brown measures 21 dE darker); never rank on them.
- **paint stacks along the surface normal** (`tools/paint_stack.py`): a top face stacks per
  layer, a wall in bands >= 0.3 mm with Arachne walls (classic printed nothing under 0.5 mm:
  sliced cube, 2026-10-17). Every stackable pair is screened with stacks: the flat ranking sees
  filaments as opaque and never shortlists the translucent sets stacking is for.
- **paint's walls: boxes overhang, Arachne per object** (`overhang`, `wall_settings`). The
  voxel staircase stops short of a curved surface; the sliver left is base filament, which
  classic walls drop (21% of a head pitted) and Arachne prints (30-50% base). Boxes reach 1.5
  voxels out (the slicer clips modifiers to the object; parts are not, so they never overhang) and `min_bead_width` 50% (85% left
  27% bare; `min_feature_size` changed nothing): 95% of the surface has plastic, 94% of it
  the designed filament. Per-object keys are honoured headless (identical to the project's).
- **Check a slice by position, not by `;TYPE:Outer wall`.** The slicer types the outer loop
  of every colour region "Outer wall", buried ones included; the 2026-10-17 "walls lose
  dithered features" finding was that artefact. Walk the unpainted model's outer wall as the
  true surface and look up what the painted slice prints there.
- **Tallying G-code: count only moves with X or Y.** Unretracts (`G1 E.8`) were 30-45% of all
  positive E on a busy model and skewed every share; the 2026-10-15 wall numbers had that error.
- **paint's tool-change count is a bound** (`tools/paint_budget.py`). Flash Studio starts a
  layer on the tool it holds and orders the rest itself (flush volumes), so `step` carries the
  set of possible last tools. Sliced (Creator 5 template, the head, 2026-10-18): no limit 2697
  vs 2700 counted, `--max-toolchanges 900` 827 vs 836, 1350 1327 vs 1338. Plans are chosen on
  their evaluated count (`Budget.choose`): re-solving mixed columns adds ~20 changes to the
  subset-solve prediction. A layer's tools
  come from its labels plus the base wherever a solid voxel is uncoloured (`layer_tools`).
  On Windows the slicer is `C:/Program Files/Flashforge/Flash Studio Desktop/flash studio.exe`
  (same `--datadir --slice 1 --outputdir` flags); the template is `~/Downloads/stackforge.3mf`.
  Small cKDTree queries with `workers=-1` cost ~1 ms each in thread start-up.
- **paint `--rotate` poses the model before voxelising** (`core/pose.py`), never after:
  the painted 3MF rotated in the slicer would put the colour between layers. Turned about the
  bbox centre, then the same XY centre and min z = 0. The GUI's Pose box is paint-only
  (top_paint's `--rotate` turns its image). Face-down head sliced 2026-10-18: 1670 changes vs
  1678 counted, 585 layers; the template has supports off.
- **paint `--expr` goes through an AST whitelist** (`compile_expr`) before `eval`: no
  attributes, subscripts, lambdas, strings. Add a function by adding it to `_EXPR_NAMES`.
- **Dither gains are simulated.** Blurred dE improves most with shallow stacks, but the slicer
  drops the one-pixel features a dither is made of (see above). `mix_pairs` in `plaque.py`
  averages two gamut states; nudging the target before the query changed nothing (the old
  implementation). Floyd diffusing error in linear light was measured too: not adopted.
- Unverified: PrusaSlicer honouring the per-object `layer_height`/`fill_density` that
  `--flavor prusa` writes (no Prusa profile is carried over, so nothing else sets them).
- **`tools/measure.py` is unverified on hardware.** Its spotread session is copied from calibration-suite (do not
  import it, the repos are independent). All-zero XYZ is refused: a stale ColorMunki dial prints zeros.
- **`--nospos` = patched Argyll with no dial check** (`~/.local/bin/argyll-nospos`, unconditional
  patch; `ARGYLL_NOSPOS=1` is only the wrapper's marker). Always go through the wrapper, never the
  patched binary's path: the wrapper isolates its calibration cache. The white-paper, repeat and
  wedge-reversal checks in measure.py stand in for the check the patch removed; keep them.
