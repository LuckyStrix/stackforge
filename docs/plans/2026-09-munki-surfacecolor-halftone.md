# Plan: ColorMunki validation, surfacecolor, blue-noise halftoning, notes

## Context
stackforge (github.com/LuckyStrix/stackforge, public, dir `~/programmingExperiments/shared/3mf_scripts`)
predicts colour from an uncalibrated optical model (single-pass alpha-over, per-filament `td`,
mostly *estimated*). The user has a ColorMunki Photo (spotread/ArgyllCMS 2.3.1 installed) whose
dial position reports stale, so a power-cycle procedure is needed (solved in the separate
`calibration_suite` repo, commit de2ac70). Goal: real measurements to test colour + transmission,
then build ideas 14 and 24, and only *note* 15 and 16. **Idea 23 is dropped** (existing
`--rank`/`--slots` stays as-is). Repos stay independent: copy the spotread code, do not import calsuite.

Order: A (measurement, gives ground truth) -> B (surfacecolor) -> C (blue noise) -> D (notes).
Commit per part; push only when asked (repo is already public, so keep README honest: hardware paths
marked unverified until run).

## A. `munki.py` + hardware-optional tests
Copy/adapt from `calibration_suite/calsuite/display/backends/spotread_session.py`: pty-driven
long-lived `spotread` session, prompt regexes, `NeedsRecalibration`, `DeadReading` (all-zero XYZ
refused), power-cycle guidance. Drop `calsuite.tools`; use `shutil.which`. Different needs, so
different args than the display flow (`-e`):
- **Reflectance** (printed wedges): `spotread -s -i D50 -Q 1931_2` (reflective is the default
  mode), spectrum kept (`-s`) so K/S (idea 16) and metamerism work later have data. Calibrate on the
  instrument's white tile first. Note Munki Photo is UV-included only (no M2) and PLA whiteners can
  fluoresce: record that in the output metadata.
- **Transmission via the calibrated laptop screen** (no backlight): show a full-screen white (and
  optionally R/G/B) patch in a tkinter window, measure emissive (`-e`, `-s`) directly on the screen
  with no sample, then again with the wedge step laid on the screen under the meter.
  T = L_with / L_without per channel/wavelength. Mitigations to document: black foam/card mask
  to stop edge light leaking, fixed meter position jig, LCD polarisation (PLA is slightly
  birefringent, expect small bias), screen warm-up, and a repeat-white drift check. Also gives a
  `td` cross-check independent of reflectance.
- CLI: `munki.py measure-wedge` (n patches -> hex list + spectra JSON ready for
  `calibrate.py fit --measured/--measured2`), `munki.py transmission`, `munki.py verify-plaque`
  (measure patches of a printed stackforge plaque, report dE vs its `--preview` prediction).
- Reuse: `calibrate.py` (`build_wedge`, `fit_td`, `cmd_fit` input via `--measured`),
  `tdcolor.py` (`srgb_to_lab`, `to_hex`), `filamentdb.DB` provenance ("measured").

Tests (`tests/`, stdlib unittest, matching `test_smoke.py`):
1. Offline, always run: fake `spotread` script (transcript from real Munki output as in calsuite)
   exercises session parsing, calibration prompts, DeadReading refusal, hex/spectrum output.
2. Offline: synthetic round trip — generate a wedge with the model at known `td`, feed it through
   `calibrate.fit_td`, assert recovered td (a regression test the repo lacks today).
3. Hardware, opt-in (`STACKFORGE_MUNKI=1`, skipped otherwise): (a) two-base reflectance wedge fit
   -> mean dE < 5 (calibrate's own poor-fit threshold); (b) held-out prediction: print 2-filament
   stacks not used in the fit, compare predicted vs measured dE; (c) transmission agreement with
   reflectance-fit `td`, reported as a ratio not a pass/fail until baselined.
Wedge steps must be larger than the meter's spot aperture (check the Munki's; keep steps >= ~10 mm).

## B. `surfacecolor.py` (idea 14), per `PLAN-surfacecolor.md`
Follow its build order; volumetric partition, slicer does the intersection (no UVs).
1. `surfacecolor.py` with `checker3d` and `expr`: read via `td3mf.read_3mf`, bbox, voxel grid at
   `--resolution`, pattern -> filament index (`tdcolor.quantize`/nearest_lab), per-Z-layer
   `td3mf.greedy_rects` -> `BoxBuilder` -> `td3mf.get_writer(flavor)`. Layer grid must come from
   `--template` (`template_layer_settings`); first layer offsets everything (see CLAUDE.md).
2. `image-spherical`, `image-cylindrical`, `checker-sphere`, `stripes`, `gradient`.
3. Shell masking via distance transform (`scipy.ndimage`), not Z-depth (plan's risk section).
4. Painting window: **out of scope now** (plan says only after 1-3 are trusted).
Reuse `make_fixture.py` (`badge.3mf`) for tests; `expr` is sandboxed to numpy names only (it is an
eval, so restrict globals and document it). Warn on box count as stackforge does. Tests: pattern
functions on a small grid, box emission on the fixture, `tests/test_smoke.py` style end-to-end.

## C. Blue-noise halftoning (idea 24) in stackforge
> Superseded: thresholding the target before the query moved it by less than the gamut's
> spacing and changed nothing. `stackforge.mix_pairs` (two-stack mixing) replaced it.
- `tdcolor.py`: add `dither` mode `blue` next to `none/ordered/floyd` (`bayer()` is the model for
  the threshold matrix): void-and-cluster blue-noise tile generated with numpy, cached under
  `data/` (generate once, commit the small `.npy`).
- `stackforge.solve_image` (uses `Gamut.query`): apply the threshold before the gamut query,
  same path as `ordered`; flag choices/help text updated (help currently says dithering rarely
  helps).
- Compare harness `tools/halftone_compare.py`: none / ordered / floyd / blue, scored with a
  Gaussian-blurred (viewing-distance) Lab dE, because per-pixel dE penalises halftoning unfairly;
  writes a contact sheet into `docs/`. Result decides the README claim, whatever it is.
- Test: blue-noise tile has flat mean and no low-frequency energy peak.

## D. Notes only
Append design notes to `docs/ideas.md`: **16 Kubelka-Munk** (K, S per filament; the two-base wedges
from A supply exactly the reflectance-over-white/black data plus spectra to separate K from S;
would replace the alpha-over step in `tdcolor`/`Gamut`) and **15 backlit stackforge** (transmission
through the whole stack, layer order/gamut/optimizer change; A's screen-transmission rig is its
measurement method). No code. Also remove 23 from the roadmap table (mark dropped).

## E. Process changes (from user feedback)
- **Notes and plans live in the repo**: first step of implementation is copying this plan to
  `docs/plans/2026-09-munki-surfacecolor-halftone.md` (existing `PLAN-surfacecolor.md` moves to
  `docs/plans/surfacecolor.md`, README links updated); ideas 15/16 notes go in `docs/ideas.md`.
  Nothing project-related is kept only under `~/.claude`.
- **Push after the changes** (commit per part with the Co-Authored-By trailer, then
  `git push` to `LuckyStrix/stackforge` main; re-run the leak grep first: no serials, no personal paths,
  no raw instrument logs with device serials, per the style guide).
- **After everything lands, launch cheap subagents (model `haiku`)** in parallel, each read-only with a
  narrow brief, to hunt for flaws: (1) docs/README accuracy vs code (commands actually run),
  (2) test coverage gaps and flaky/unsafe tests, (3) security/robustness of `expr` eval and the
  spotread pty code. Their findings get triaged by me (they can be wrong), real ones fixed and
  recorded in `docs/REVIEW-notes.md`, then pushed.

## Verification
- `python3 -m unittest discover -s tests` (offline tests) after each part.
- A: with the Munki connected, `STACKFORGE_MUNKI=1` tests plus one real wedge measured and fitted.
- B: run on `fabric.3mf`/`badge.3mf` fixtures, confirm output loads in Flash Studio with
  `--template`, check sliced preview (the still-unverified modifier-override question).
- C: run compare harness on a photo and the hue sweep; view the contact sheet.
- Update README (tools table, quick starts, caveats) and CLAUDE.md as each part lands.

## Open items to confirm during work
Whether Munki Photo emissive mode works flat against a laptop screen with a sample in between;
its reflectance aperture size; whether the power-cycle flow is needed on every start.
