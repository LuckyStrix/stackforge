# Review notes

Module names below are from before the 2026-10-05 rename; today they are `tools/measure.py`
(`munki.py`), `tools/paint.py` (`surfacecolor`), `tools/dither_compare.py` (`halftone`),
`tools/make_samples.py` (`make_fixture.py`), `tools/plaque.py` / `stackforge-plaque`
(`stackforge --rank`) and `tests/test_measure.py` (`tests/test_munki.py`).

Findings from read-only Haiku review passes (2026-09-30) over the munki / surfacecolor / halftone
work, triaged by hand. Agent output was treated as leads, not facts: each item was checked against
the code first.

## Fixed
- `munki.py` transmission drift check divided by the first bare reading with no guard (Y=0 with X/Z
  non-zero slips past `DeadReading`). Guarded.
- `td_from_transmittance` had an unguarded denominator. Guarded.
- `SpotreadSession.close()` left the pipe file objects open on the non-pty path. Now closed.
- `tests/test_munki.py` leaked its fake-spotread temp dir. Now cleaned up.
- README tools table omitted `make_fixture.py` (docs-accuracy pass; the only mismatch it found).
- Added tests: `calibrate.build_wedge` / `build_chips` geometry, image-pattern mapping in
  `surfacecolor`, and a `stackforge --rank` end-to-end run (the rank path had no coverage).

## Rejected or already covered
- "calibrate.py entirely untested": `fit_td` already had a round-trip test in `test_munki.py`;
  only wedge/chips geometry was missing (added).
- "PTY blocking read at test_munki.py:247": no such line exists (file is shorter); the reader is a
  daemon thread and every wait has a timeout.
- "eval in `--expr` can be escaped": was true (builtins stripped + dunder check is not a sandbox).
  Fixed 2026-10-02: the expression is parsed and checked against an AST whitelist
  (`surfacecolor.compile_expr`) before `eval`; int literals become floats so `9**9**9` cannot hang.

## Still open
- ~~`--template` has no test~~: `tests/test_template.py` builds a synthetic template, and
  `tests/test_slice.py` slices with the real one when Flash Studio is installed.
- `test_smoke` / `test_cli_writes_3mf` only check that a valid 3MF is produced, not its geometry.
- `munki.py` and the screen-transmission method have never been run on the instrument.
