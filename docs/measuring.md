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
the default). Steps must be wider than the spot aperture; `calibrate.py wedge` defaults to 10 mm,
check your meter's aperture. It prints the `calibrate.py fit --measured ...` line to run next; do this
over white *and* black bases so td and colour separate (see the calibrate section of the README).
The Munki Photo is UV-included only (no M2), and whiteners in PLA fluoresce, so readings can differ
from a daylight photo.

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
