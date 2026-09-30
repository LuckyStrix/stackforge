# Review notes

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
- "eval in `--expr` can be escaped": true in principle, documented. It is for trusted local input
  and refuses dunders/imports; not a sandbox.

## Still open
- `stackforge --template` (layer-height inheritance, solid-infill flags) has no test: it needs a
  slicer-exported project 3MF to use as a template, and none is committed (they contain a full
  printer profile). Worth adding a minimal synthetic template.
- `test_smoke` / `test_cli_writes_3mf` only check that a valid 3MF is produced, not its geometry.
- `munki.py` and the screen-transmission method have never been run on the instrument.
