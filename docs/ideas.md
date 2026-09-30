# Colour science and the four-head printer: ideas

Roadmap of possible next projects, roughly by effort (weekend / weeks / semester).
Items 14-25 of a longer list; see the README and `docs/plans/surfacecolor.md` for what exists.

| # | idea | effort |
|---|---|---|
| 14 | `surfacecolor` (built through shell masking; painting window remains) | done / partial |
| 15 | Backlit stackforge: full-colour lithophanes | weeks |
| 16 | Kubelka-Munk upgrade (K and S per filament) | weeks |
| 17 | Spectral stackforge + metamerism explorer | semester |
| 18 | DIY spectro/colorimeter for wedges | weeks |
| 19 | Closed-loop plaque calibration | weeks |
| 20 | Filament dryer + spool scale | weeks |
| 21 | Coloured replicas of artifacts (photogrammetry to surfacecolor) | semester |
| 22 | Tolerance-learning parametric part library | weeks |
| 23 | ~~"Which 4 spools should I buy?" optimizer~~ dropped; `--rank` already covers a chosen pool | - |
| 24 | Blue-noise halftoning (built; helps only shallow stacks, see README) | done |
| 25 | Colourblind palette validator CLI | weekend |

## Noted, not built

### 16. Kubelka-Munk upgrade
Replace stackforge's single-pass alpha-over (`C' = C*T + colour*(1-T)`) with per-filament
absorption K and scattering S, the standard model for layered turbid media. Alpha-over has one
optical parameter (`td`) and ignores light scattered back up through the stack; KM has two and
predicts reflectance of a layer of thickness h over a background analytically, with an explicit
multiple-reflection term. `calibrate.py`'s two-base wedges (over white and over black) are exactly
the pair of measurements needed to separate K from S, and `munki.py measure-wedge` saves spectra
so K and S can be fitted per wavelength band later. Changes needed: a `td`-free filament record
(K, S per channel or band), `Gamut`'s expansion step in `stackforge.py`, and `calibrate.fit_td`.
Risk: KM assumes diffuse, homogeneous layers; extruded lines with air gaps may not be. Test by
predicting held-out stacks (the plaque-verification step in `munki.py`) and comparing dE against the
current model before adopting it.

### 15. Backlit stackforge (full-colour lithophanes)
Light passes *through* the whole stack instead of reflecting off an opaque base, so nearly
everything changes: no opaque base (the backing is the diffuser or nothing), stack order matters
in a different way (the layer nearest the light and the layer nearest the eye contribute
differently), and the model becomes multiplicative, `T = prod exp(-h_i/td_i)` per channel, with
filament colour entering as the absorption spectrum rather than a reflected colour. The gamut
is then the set of transmittance triples reachable by stacks, and the optimizer targets a
transmitted rather than a reflected image. A first version could reuse `Gamut` with the
transmittance product and no base colour. Measurement: `munki.py transmission` (screen as
backlight) measures exactly this quantity, per channel, per chip thickness. Open question: the
colour of the actual backlight (LED white vs screen), which multiplies the result.
