# Measuring with a ColorMunki (or any ArgyllCMS spectro)

`munki.py` is written from spotread's documented output and a fake-spotread test; **it has not been
run against the instrument yet.** Treat every section below as a checklist for the first run.

## Setup
1. `sudo apt install argyll` (spotread 2.3.1 was used). The ColorMunki needs the udev rule from
   Argyll's `libusb` install, or root.
2. The dial: if your unit's dial position reports stale, `spotread` may print an all-zero XYZ at
   the calibration position instead of complaining. `munki.py` refuses zero readings (`DeadReading`).
   If that happens: power-cycle the meter with the dial at the calibration position, then
   the measure position, and re-run. (The same workaround is documented in the calibration-suite repo.)

## Reflectance (wedges and plaques)
`munki.py measure-wedge --steps 12 -o wedge.json` runs `spotread -s -i D50 -Q 1931_2` (reflective is
the default). It prints the `calibrate.py fit --measured ...` line to run next; do this over white
*and* black bases so td and colour separate.

**Patch size.** The ColorMunki samples a roughly circular area about 8 mm across (measured 7.8 x 7.95 mm
in an Argyll mailing-list test; readings were identical from 20 mm patches down to 7 mm, nearly so at
6 mm, and clearly bad at 5 mm). Argyll's driver also discards samples near patch edges. So an 8 mm
circle needs a patch a bit bigger than 8 mm just for the instrument; I'd allow **at least 3 mm of
margin each side, so steps of 14 mm**, because light scatters sideways inside translucent PLA and a
neighbouring step bleeds into the reading (this last part is my reasoning, not from a source).
`calibrate.py wedge` now defaults to 14 mm steps (it was 10 mm, which leaves only 1 mm of margin
and is at the mercy of hand placement). 12 steps at 14 mm is 168 mm long.

**Light.** The ColorMunki is **UV-cut only** (white-LED illuminant; Argyll's docs say it cannot use
fluorescent-whitener compensation), so PLA whiteners are not excited. A white filament can read
slightly duller or yellower than under daylight. That is a consistent bias, not noise, and it is
absorbed into the fitted colour; note it against the filament and do not compare across meters.

## Transmission with a laptop screen as the backlight
Print `calibrate.py chips --filament <id> -o chips.3mf`, then caliper each chip.
`munki.py transmission --thickness 0.25,0.33,...` shows white/R/G/B patches full screen, reads the
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
connected: `STACKFORGE_MUNKI=1 python3 -m unittest tests.test_munki`.
