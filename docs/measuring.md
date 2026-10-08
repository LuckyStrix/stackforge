# Measuring with a ColorMunki (or any ArgyllCMS spectro)

`stackforge-measure` is written from spotread's documented output and a fake-spotread test; **it has not been
run against the instrument yet.** Treat every section below as a checklist for the first run.

## Setup
1. `sudo apt install argyll` (spotread 2.3.1 was used). The ColorMunki needs the udev rule from
   Argyll's `libusb` install, or root.
2. The dial: if your unit's dial position reports stale, `spotread` may print an all-zero XYZ at
   the calibration position instead of complaining. `stackforge-measure` refuses zero readings (`DeadReading`).
   **Fix: the patched Argyll.** `~/.local/bin/argyll-nospos` runs an ArgyllCMS 2.3.1 build with the
   dial-position check compiled out (same commands and prompts; its own calibration cache in
   `~/.cache/argyll-nospos`). Use `stackforge-measure --nospos ...` (or `argyll-nospos stackforge-measure ...`).
   Nothing then checks the dial, so on that build `stackforge-measure`:
   - says when to turn the dial back to measuring after calibration (spotread no longer insists);
   - asks for one reading of plain white paper and stops unless Y is 70-110 (a calibration taken
     off the calibration tile rescales every later reading);
   - flags a reading within dE 0.5 of the previous one (dial left at calibration, or meter not
     moved) and offers a re-measure;
   - after a wedge, lists steps where L* runs against the wedge's trend (all builds).
   The output JSON records the `spotread` command and `nospos`.
   Fallback on the system build: power-cycle the meter with the dial at the calibration position,
   then the measure position, and re-run. (The same workaround is documented in the calibration-suite repo.)

## Reflectance (wedges and plaques)
`stackforge-measure measure-wedge` runs `spotread -s -i D50 -Q 1931_2` (reflective is the default).

**The wedge sheet round trip** (works across machines; the measuring one needs only the CLI and Argyll):
1. `stackforge-calibrate wedge --base white,black ... -o wedge_teal.3mf` (or *Write wedges 3MF* in the
   GUI) also writes `wedge_teal.sheet.json`: grid, step count, and every strip (one row of steps over
   one base) in print order, with labels and colours since the measuring machine may have no library.
2. `stackforge-measure measure-wedge --sheet wedge_teal.sheet.json` reads, strip by strip, the bare-base
   patch at the left end of each wedge and then steps 1..N, and writes `wedge_teal.readings.json`.
3. `stackforge-calibrate fit --filament teal --readings wedge_teal.readings.json --write` (or *Load
   readings* on the guided page) fits with the **measured** base colours and the wedge's own layer
   height. The bare patch exists because the library's base colour is an estimate, and every step is a
   blend towards it.

Without a sheet, `measure-wedge --steps 12 [--base-patch] -o wedge.json` reads one strip and prints the
`fit ... --measured ...` line (in the GUI, *Copy readings to Calibrate* fills the next empty wedge).

**Patch size.** The ColorMunki samples a roughly circular area about 8 mm across (measured 7.8 x 7.95 mm
in an Argyll mailing-list test; readings were identical from 20 mm patches down to 7 mm, nearly so at
6 mm, and clearly bad at 5 mm). Argyll's driver also discards samples near patch edges. So an 8 mm
circle needs a patch a bit bigger than 8 mm just for the instrument; I'd allow **at least 3 mm of
margin each side, so steps of 14 mm**, because light scatters sideways inside translucent PLA and a
neighbouring step bleeds into the reading (this last part is my reasoning, not from a source).
`stackforge-calibrate wedge` now defaults to 14 mm steps (it was 10 mm, which leaves only 1 mm of margin
and is at the mercy of hand placement). 12 steps at 14 mm is 168 mm long, 182 mm with the bare-base
patch; with gaps it can outgrow the bed, which `wedge` refuses when the template says so.

**Light.** The ColorMunki is **UV-cut only** (white-LED illuminant; Argyll's docs say it cannot use
fluorescent-whitener compensation), so PLA whiteners are not excited. A white filament can read
slightly duller or yellower than under daylight. That is a consistent bias, not noise, and it is
absorbed into the fitted colour; note it against the filament and do not compare across meters.

## Transmission with a laptop screen as the backlight
Print `stackforge-calibrate chips --filament <id> --template <project.3mf> -o chips.3mf`, then caliper each chip.
`stackforge-measure transmission --thickness 0.25,0.33,...` shows white/R/G/B patches full screen, reads the
bare screen, then each chip, then the bare screen again (drift check).
`T = through-chip / bare` per channel, `td = -thickness / ln T`.

Things that will bite: light leaking round the chip (black foam or card, a hole a bit smaller than the
chip), the meter not sitting the same way each time (make a jig), LCD polarisation (PLA is slightly
birefringent), screen warm-up (leave it 20 minutes), and the display's own calibration: the ratio
cancels absolute level but not a channel that is clipped or off-gamut. Chips with T under ~2% are
ignored by the fit because the meter floor and stray light dominate there.
This is a cross-check on the reflectance td, not a replacement: a screen is three narrow bands,
not a spectrum. Whether emissive mode works with the meter's dial position flat on a screen through
a sample is unverified.

## Tests
`python3 -m unittest discover -s tests` runs the fake-spotread and maths tests. With the meter
connected: `STACKFORGE_MUNKI=1 python3 -m unittest tests.test_measure`.

## ColorMunki facts and where they came from
Looked up 2026-09-30. Re-check a source before relying on a number for anything expensive.

| fact | value | source |
|---|---|---|
| sampling area | ~8 mm circle (7.77 x 7.95 mm measured) | [Argyll list: patch sizes](https://argyllcms.freelists.narkive.com/UpodF1z0/limitations-on-colormunki-patch-sizes) |
| patch size that reads the same as 20 mm | down to 7 mm; ~6 mm nearly; 5 mm bad | same thread |
| edge handling | samples over a patch transition plus a margin are discarded; patches with too few samples (~3-4) rejected | same thread (Graeme Gill) |
| UV | UV-cut only, white-LED illuminant; no fluorescent-whitener compensation | [Argyll instruments doc](https://www.argyllcms.com/doc/instruments.html) |
| modes | reflective and emissive spot/strip; transmission not listed for the ColorMunki | same doc |
| native reflective standard | X-Rite XRGA | same doc |
| geometry | 45/0, UV cut (i1Studio, the ColorMunki's successor) | [X-Rite aperture guidance](https://www.xrite.com/service-support/patch__aperture_size_requirements__xrite_exact) |
| repeatability | moving the meter on a smooth uniform patch can shift ~0.2 dE | [Argyll list: drift](https://argyllcms.freelists.narkive.com/38w37snS/colormunki-measurement-drift) |

Also useful:
- [Argyll spotread docs](https://www.argyllcms.com/doc/spotread.html) for flags (`-s` spectrum, `-e` emissive, `-i` illuminant, `-Q` observer)
- The calibration-suite repo (LuckyStrix/calibration-suite): the stale-dial power-cycle procedure, and a transcript of a real ColorMunki Photo's spotread session (`calsuite/display/backends/spotread_session.py`)

Not found in any source I read (so still to measure): the aperture's field of view in emissive mode,
and the meter's footprint on a stepped surface.
