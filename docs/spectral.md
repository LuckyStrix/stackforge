# Spectral optics: Kubelka-Munk per wavelength

The RGB model (`td`, or `td_rgb`) treats a layer as grey see-through paint that fades towards the
filament's colour. Real filament is a coloured filter. Orange passes red and stops blue, so orange
over blue goes dark olive, not the purple an alpha blend predicts. The owl plaque (2026-10-10)
showed exactly that: its background was predicted purple and printed olive, and its irises were
predicted orange and printed brown. Per-channel td moved the photo fit from dE 21 to 6, but three
channels are still coarse, and a phone photo pins them down poorly.

The ColorMunki reads a full reflectance spectrum for every wedge patch (380-730 nm, 10 nm).
`stackforge-measure measure-wedge --sheet` already saved those spectra; nothing used them until now.

## The model (`core/spectral.py`)

Each filament gets an absorption K and a scattering S (1/mm) per 10 nm band: 36 of each. A layer of
thickness h laid over a background of reflectance Rg reflects

    R = R0 + T² Rg / (1 − R0 Rg)

where R0 and T are the layer's own reflectance and transmittance (Kubelka-Munk two-flux, hyperbolic
form, stable at the pure-absorber and pure-scatterer limits). The rule is exact for KM: two half
layers equal one whole layer. A stack's state is therefore just its reflectance spectrum, and
`plaque`'s breadth-first gamut search works unchanged (`SpectralGamut` in `tools/plaque.py`).

Colour comes last. Each spectrum is integrated against the CIE 1931 2° observer under the chosen
light, then mapped to sRGB through a Bradford adaptation onto D65. With that adaptation, a perfect
white is (1, 1, 1) under every light, and only non-neutral colours move. The CIE tables in
`data/cie/` are the CIE's own files, checksum-verified (`data/cie/SOURCE.md`); illuminant A is
Planck's law at 2856 K.

**No surface term.** K and S are fitted to what the meter reads, so they are effective values that
include surface reflection. They hold for stacks printed the way the wedge was: solid infill, the
same layer grid, the same top finish.

**KM assumes diffuse, homogeneous layers.** Extruded lines with air gaps may not be. The held-out
check below is what tells you whether it holds for your prints.

## Calibrating a filament

You need the filament printed as a wedge over **two contrasting bases**, a white and a black, each
with its bare-base patch measured. Over white a layer's scattering shows; over black its absorption
does. Only the pair separates K from S.

```sh
stackforge-calibrate wedge --filament orange --base white,black --template my.3mf -o wedge.3mf
#   print it; the sheet (wedge.sheet.json) is written beside it
stackforge-measure measure-wedge --sheet wedge.sheet.json              # spectra are saved
stackforge-spectral fit --readings wedge.readings.json --filament orange --preview fit.png
stackforge-spectral fit --readings wedge.readings.json --filament orange --write
```

The fit reports:
- in-sample dE per step
- **held-out dE**: each step predicted by a fit that never saw it. This is the honest accuracy
  number. Above ~3 the model is not describing the print; re-measure, or suspect the print.
- K, S and R∞ at every fifth band

`--write` is refused:
- with one base, or two bases that differ by less than 0.25 in mean reflectance
- with fewer than 4 steps
- when the readings' own XYZ disagrees with the Y of their spectra (see "Unverified" below)

A fit stores a `spectral` block on the filament (`Filament.spectral`: K, S, date, source file,
bases, dE). It also refreshes `color` from R∞, unless the RGB colour was itself measured. It never
touches `td`, `td_rgb` or `provenance`: those belong to the RGB model.

## Using it

- **Plaque:** `stackforge-plaque … --optics spectral [--illuminant D65|D50|A]`. The Designer has the
  same switch under Colour > Optics. Only spectrally calibrated filaments are accepted, base
  included. The CLI lists the missing ones and refuses; the Designer greys them out. Everything
  downstream (ranking, dithering, export, the opaque-base check) is unchanged. The base check uses
  the KM transmittance of the worst band.
- **Spectra tab:** pick calibrated filaments (several overlay) and a base, then drag the layer
  slider. The tab has:
  - three views: reflectance over the base; transmittance through the layers; K/S on a log scale
  - faint ghost curves for every other layer count, a swatch strip, and a hover read-out
  - the stack builder ("2 orange over 3 blue over white", drawn dashed)
  - the viewing-light switch, a metamerism check: "shift under A" is the dE between the stack in
    daylight and under a lamp
  - "Load readings…", which dots the measured wedge steps over the model
  - "Export PNG"
- **`stackforge-spectral show --filaments a,b --base white --layers 8 --preview out.png`:** the same
  plot from the command line.

## Trying it before anything is measured

`Load demo spectra` (Spectra tab), `show --demo`, and `demo-readings` use synthetic filaments
(`demo-white`, `demo-orange`, …). They are watermarked "SYNTHETIC" in the viewer and plots, flagged
`synthetic` if fitted, and never written to your library by the viewer. `demo-readings` writes a
readings file in `measure --sheet` format from known K/S plus meter noise. Fitting it is the
round-trip test of the fit (`tests/test_spectral.py`).

## Unverified

- **The spectrum scale.** Real spotread spectra have never been parsed (`measure.py`, "not verified
  against hardware"). The scale (0..1 vs 0..100) is decided over a whole readings file, never per
  patch: a very dark patch on the 0..100 scale reads below 1. The fit then cross-checks each base
  patch's own XYZ against the Y of its spectrum, and refuses to write on a mismatch. The first real
  wedge set is also the first test of this path.
- **Printed accuracy of `--optics spectral`.** Nothing has been printed from it yet. The demo
  filaments are tuned to look like printed PLA (about 60% of red light still passes 0.24 mm of
  orange, as in the owl photo fit), but they are not measurements.
- **Older builds** drop the `spectral` field with a warning when they load the library, and lose it
  on their next save. Keep using this branch's build on a library that has spectral calibrations.
