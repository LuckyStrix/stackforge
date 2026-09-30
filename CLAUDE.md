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

## Bite

- Unverified: slicer honouring extruder overrides on *modifier* volumes when slicing.
- `filaments.json` tds are mostly estimates; never present them as measured.
- `polymaker.py` fetches from the network; the catalogue is cached in `polymaker_catalog.json`.
