# CIE colorimetric tables

Unmodified copies of the CIE's open-access datasets, fetched 2026-10-10 from
https://files.cie.co.at/Publications-datasets/ (dataset pages under https://cie.co.at/data-tables).
`core/spectral.py` resamples them to the meter's 10 nm grid; the files themselves are never edited.

| file | dataset | md5 (matches the CIE page) |
|---|---|---|
| `CIE_xyz_1931_2deg.csv` | CIE 1931 colour-matching functions, 2 degree observer, 1 nm (CIE 018:2019 Table 6, doi:10.25039/CIE.DS.xvudnb9b) | `17cca777db64b17170f06f67ce9d3ab7` |
| `CIE_std_illum_D65.csv` | CIE standard illuminant D65, relative SPD | `03d4eb9b837c60671627c946fb534deb` |
| `CIE_std_illum_D50.csv` | CIE illuminant D50, relative SPD | `e72757c3078b58e78ba63051be4b27b0` |

Illuminant A is not tabulated here: it is Planck's law at 2856 K (CIE 015), computed in code.
