# Plan: one GUI for every command-line feature

Status: built through Step 6 on branch `unified-gui` (the side-note at the end is still not done). Goal: a single app, `tdforge-gui`, that exposes every CLI in this
repo (stackforge, topdeco, surfacecolor, calibrate, munki, filamentdb, polymaker, halftone_compare,
make_fixture) in one window, without hand-wiring each flag.

## Decisions (all settled)

- **One installable package** (`src/tdforge/`, `pip install -e .`), standard `src/` layout. Tools,
  core libraries and GUI all live inside it. (`tdforge` is a placeholder name; it matches the
  `td3mf` / `tdcolor` prefix and is a single find-and-replace to change before the first commit.)
- **Widgets are generated from the argparse definitions.** The CLI is the single source of truth; a
  new flag appears in the GUI with no GUI change.
- **No standalone legacy GUIs.** `stackforge_gui.py` and `filamentdb_gui.py` are absorbed into the
  app as tabs and deleted as entry points. One GUI, one launcher.
- **Settings are global; presets are per tool.** No per-print project files.
- **munki shows a terminal in the window.** No wizard: run the CLI, show its output, give it an
  input line (its `input("... Enter")` prompts are the interface).
- **Preview cost is not a constraint.** Jobs run off the UI thread; slow is fine.
- **Headless-slice verification is a side-note** (end of this file), not part of the build.

## Principles

1. The CLI is the product; the GUI builds an argv and runs it. The form shows the equivalent shell
   command, copyable.
2. Colour maths stays pure numpy and 3MF I/O stays in `td3mf.py` (project rule, now `core/`). `gui/`
   imports `tools/` and `core/`; neither imports `gui/`.
3. Generated forms cover the long tail; hand-built widgets only where a generic one would be bad
   (filament picker, image picker, palette), chosen by an override table, not by forking form code.

## Target layout

```
pyproject.toml           deps (numpy, Pillow, scipy), console scripts, tkinter note
README.md  CLAUDE.md  LICENSE  docs/
filaments.json           USER DATA, stays at the repo root, defaults unchanged (see "Data files")
polymaker_catalog.json   likewise
src/tdforge/
  __init__.py
  core/                  pure libraries, no argparse, no tkinter
    tdcolor.py           colour maths (numpy only)
    td3mf.py             all 3MF I/O
    filamentdb.py        DB class + its CLI `main`
  tools/                 one module per CLI; each exposes build_parser() and main()
    stackforge.py  topdeco.py  surfacecolor.py  calibrate.py
    munki.py  polymaker.py  halftone_compare.py  make_fixture.py
  gui/
    app.py               root window, notebook, menu, ProjectBar wiring, main()
    settings.py          global settings (JSON) + presets store
    theme.py             was guikit.py: palette, ttk theme, ImageView, Section, ScrollFrame
    argform/
      spec.py            parser -> list[FieldSpec]; no tkinter, unit-testable
      argv.py            {dest: value} <-> argv list
      overrides.py       per-tool table: widget kind, label, order, hidden, project-bound
      widgets.py         FieldSpec -> tk widget
      form.py            CommandForm: groups, mutual exclusion, required marks, Command line
    run/
      runner.py          subprocess job: stdout queue, cancel, exit status
      terminal.py        TerminalView: scrolling output + input line
    pickers/
      filaments.py  files.py  palette.py
    tabs/
      filaments/         from filamentdb_gui.py, split by class: editor, preview, match, sku_browser
      plaque/            from stackforge_gui.py, split: job/worker, view, controls
      paint.py  calibrate.py  measure.py  tools.py
tests/
  (existing tests, imports changed; sys.path hacks deleted)
  test_argform.py        spec/argv round-trip + drift guard
  test_gui_smoke.py      builds every form; skipped without $DISPLAY
```

Console scripts (from `pyproject.toml`): `stackforge`, `topdeco`, `surfacecolor`, `calibrate`,
`munki`, `polymaker`, `filamentdb`, `halftone-compare`, `make-fixture`, `tdforge-gui`.
`python -m tdforge.tools.stackforge ...` also works. Imports inside the package are absolute
(`from tdforge.core import tdcolor`).

### Data files

`filaments.json` and `polymaker_catalog.json` are user data that diff in git, so they stay at the
repo root and every default stays as it is today (`$FILAMENT_DB` / `./filaments.json`,
`$POLYMAKER_CATALOG` / `./polymaker_catalog.json`, relative to the working directory). The GUI is
the one place a cwd default is awkward, so it stores absolute paths in its settings and passes
`--db` / `--catalog` explicitly. (If the package is ever distributed beyond this checkout, moving
these to a per-user data dir is a follow-up, not part of this work.)

## Step 0 — migrate to the package (mechanical, no behaviour change)

Do this first and alone, in its own commit, so a regression is attributable.

1. `git mv` each file into `src/tdforge/{core,tools,gui}` per the layout (history is kept).
   `guikit.py` -> `gui/theme.py`; the two `*_gui.py` files are moved to their `tabs/` homes in
   Step 4 (parked under `gui/` unchanged until then so they still run via `python -m`).
2. Rewrite imports to absolute `tdforge.*`. Add `pyproject.toml` (setuptools, `src` layout, deps,
   console scripts, `requires-python`), then `pip install -e .`. Replace `requirements.txt` with it.
3. Delete the `sys.path.insert` hacks in `tests/`; fix test imports.
4. Update `README.md` and docs: `python3 stackforge.py ...` becomes `stackforge ...`. Update
   `CLAUDE.md` paths (the rules themselves, "The rule" paragraph, and the Flash Studio notes, do not
   change).
5. Anything referring to a script path by file name (the munki `argyll-nospos` wrapper lookup is by
   PATH, so unaffected; confirm by grep for `__file__` and `.py"` strings) is fixed.

Check: `python -m unittest discover -s tests` green, and the same stackforge command from the README
produces a byte-identical 3MF before and after (keep a reference output from `main` first).

## Step 1 — make the parsers importable

Every tool builds its `ArgumentParser` inside `main()`, so nothing outside can introspect it. For
each tool in `tools/` (and `filamentdb`, whose `main` is in `core/`):

- Move parser construction into `build_parser() -> argparse.ArgumentParser`.
- `main(argv)` becomes `build_parser().parse_args(argv)` then the existing body, unchanged.
  Post-parse validation (`check_args`, `ap.error`) stays in `main`.
- No changes to defaults, help text or behaviour. `make_fixture` has no flags today; it gets a
  small parser (`--out-dir`) so it is uniform.

Check: capture `--help` for every tool and subcommand before the change and diff after (must be
empty); existing tests green.

## Step 2 — generic form engine (`gui/argform`)

**Introspection (`spec.py`).** Walk `parser._actions` (private but stable since 3.2; wrapped in one
function so a change is one fix) and emit `FieldSpec`:

| argparse | FieldSpec / widget |
|---|---|
| `type=float/int`, default | spinbox / entry with the default shown |
| `choices` | combobox |
| `store_true` | checkbox |
| `nargs=N`, `*`, `+` | entry parsed to N values / list editor |
| `action="append"` | repeatable-row list |
| `required=True` or positional | marked required; Run disabled until filled |
| `help` | tooltip + dim caption under the field |
| `add_argument_group` | collapsible `Section` (theme.py), title and description kept |
| `add_mutually_exclusive_group` | radio row (`--rank` / `--no-rank`; `--palette` / `--filaments`) |
| `add_subparsers` | sub-tab per command, each with its own form |

`%%` in help strings is unescaped. Defaults of `None` render empty, with the "(default: ...)" help
text as placeholder.

**Semantic kinds** can't be read from argparse (a `str` could be a path, a hex list or an id list).
`overrides.py` maps `(tool, dest)` to a kind: `file_in`, `file_out`, `image`, `model_3mf`,
`filament_ids`, `filament_id`, `hex_list`, `hide`, or `project:<key>`. A heuristic fallback
(`output`/`preview`/`*_sheet` -> `file_out`; `template`/`model` -> `file_in`; `db` -> project-bound)
means an unlisted new flag still gets a sensible widget.

**Argv builder (`argv.py`).** Flags equal to their default are omitted so the Command line stays
short; required and changed flags are emitted; quoting via `shlex.join`. The same module parses
`{dest: value}` back from a Namespace, which is what presets use.

**Form (`form.py`).** `CommandForm(parser, overrides, project)` renders sections, enforces mutual
exclusion, shows the live Command line, and exposes `argv()`, `values()`, `set_values()` and
`validate()`.

**Drift guard (`tests/test_argform.py`).** For every tool and subcommand:
1. every non-hidden dest produces a widget (a new flag with no override fails loudly, not silently
   unreachable);
2. defaults -> argv -> `parse_args` gives the same Namespace as defaults;
3. every override names a dest that still exists (catches renames).
Spec and argv logic need no tkinter, so these run without a display.

## Step 3 — runner and terminal (`gui/run`)

**Runner.** Run `sys.executable -u -m tdforge.tools.<tool> <argv>` as a subprocess (the CLI exactly
as a user would run it, so GUI and CLI cannot diverge, and `SystemExit` / `ap.error` come back as
text plus an exit code for free). Reader thread -> `queue.Queue` -> `after()` poll on the UI thread.
Cancel = terminate, then kill after 3 s. One job at a time per tab; a status line shows
running / exit code / elapsed.

**Previews.** If the tool has `--preview`, `--gamut-preview` or `--rank-sheet`, the tab fills a temp
path automatically and, on success, loads the PNG into `ImageView` beside the log. The user's chosen
output path is untouched.

**Terminal (`terminal.py`).** The same runner with a bidirectional pipe: output into a read-only
Text widget (ANSI stripped, `\r` handled) and an input line plus an **Enter** button that writes
`line + "\n"` to stdin. munki's own `spotread` pty session is internal to the munki process, so the
outer pipe only has to carry its `input()` prompts. Focus sits on the input line so "press Enter
after placing the chip" is one keystroke; Ctrl+C cancels; "Copy log" is included.

## Step 4 — settings, presets and the project bar (`gui/settings.py`)

**Global settings**, one JSON file at `$XDG_CONFIG_HOME/tdforge/settings.json` (default
`~/.config/tdforge/`): database path, template path, flavor, part type, layer-height override,
window geometry and last tab, recent files. Loaded at start, saved on change; a missing or corrupt
file falls back to defaults and is never fatal.

**Project bar**, a strip across the top, shared by every tab and bound to those settings:
- **Database** (`--db`), **Template** (`--template`), **Flavor** (`--flavor`), **Part type**.
- **Layer height / first layer**: read-only values *derived from the template* via
  `td3mf.template_layer_settings`, with an explicit override checkbox. This makes the project's
  load-bearing rule (layer grid must match the slicer profile) hard to get wrong: no tab has its own
  layer-height box unless unlocked.
- A warning chip if the template's `Application` version looks old (the existing Flash Studio gate).

Fields marked `project:<key>` in the overrides are filled from the bar and shown as "from project"
(an unlock icon allows a one-off per-run value).

**Presets**, per tool and subcommand, one JSON each under `~/.config/tdforge/presets/<tool>[/<cmd>]/<name>.json`,
holding `{dest: value}` from `CommandForm.values()`. Save / Load / Delete in a small combobox on each
form. Project-bound fields are *not* stored in presets (they always come from the bar), so a preset
is portable across templates and the layer grid cannot drift out from under it. A preset naming a dest
that no longer exists loads the rest and notes the skipped keys.

## Step 5 — tabs, in order of value

1. **Host shell + Filaments tab.** Move the content of `filamentdb_gui.App` and
   `stackforge_gui.App` into `ttk.Frame` subclasses (they currently subclass `tk.Tk`), split by
   class into the `tabs/filaments/` and `tabs/plaque/` modules (editor, preview, match, SKU browser;
   job/worker, view, controls). No logic changes in the move. Delete both old `main()` /
   `__main__` entry points. Filaments tab = `FilamentEditor` plus the Polymaker browser;
   filamentdb CLI-only commands come from a generated form.
2. **Paint tab** (largest current gap). Mode switch: *Project from above* (topdeco) / *Pattern or
   wrapped image* (surfacecolor). The pattern dropdown shows only the parameters that pattern uses
   (`--scale` for checker3d, `--period` for stripes, `--lat/--lon` for checker-sphere...); that
   dependency is the one thing argparse does not encode, so it is a small table in `overrides.py`.
   `--expr` is labelled "trusted input only" (it is `eval`).
3. **Plaque tab.** Hosts the existing hand-built stackforge view (live estimate, ranking viewer,
   loadout by eye), since it holds behaviour a CLI form cannot express, plus an "All options"
   sub-tab that is the generated form. Where the two overlap, the hand-built view is the default.
4. **Calibrate tab.** `wedge`, `chips`, `fit` as sub-tabs; `fit`'s `--measured` uses the palette
   widget and `--from-image` the image picker. A finished fit signals the Filaments tab to reload.
5. **Measure tab.** munki subcommands as sub-tabs over one `TerminalView`; `--nospos` and
   `--spotread-arg` in a collapsed "Instrument" section. Banner when `spotread` (or the wrapper) is
   not on PATH. A button copies a finished `measure-wedge` hex list into Calibrate -> fit.
6. **Tools tab.** `halftone_compare` (contact sheet shown in the viewer) and `make_fixture`.

## Step 6 — polish

- Keyboard: Ctrl+Enter runs, Esc cancels.
- README section and a screenshot in `docs/`; update CLAUDE.md's rule line to describe
  `core / tools / gui` and name `argform` as the generation layer.

## Risks and handling

| risk | handling |
|---|---|
| Migration breaks imports or paths | Step 0 is its own commit; byte-identical 3MF before/after; tests green |
| Private argparse attributes change | isolated in `spec.py`; drift tests fail immediately |
| A flag needs a rich widget generic code can't guess | override table; fallback is a plain entry, never a missing field |
| Subprocess start-up cost (numpy import) on every run | acceptable; no persistent worker |
| Two sources for layer height (bar vs a tab) | single `project:layer_height` binding; presets exclude it |
| munki unverified on hardware | terminal passes through exactly what the CLI prints; no GUI logic to be wrong. Measure tab is not "done" until one real wedge run (existing next step) |
| tk not headless-testable | logic lives in the no-tk modules; widget smoke test skipped without `$DISPLAY` |
| Absorbed GUIs regress while being re-rooted and split | move only, no logic edits, then manual pass of each tab against the old behaviour (git history has the old files) |

## Build order and checkpoints

| step | deliverable | check |
|---|---|---|
| 0 | package migration, pyproject, console scripts | tests green; README stackforge command gives a byte-identical 3MF |
| 1 | `build_parser()` in every tool | `--help` diff empty; tests green |
| 2 | `argform` + tests | drift tests green for all tools, no display needed |
| 3 | runner + terminal | run `make_fixture`; cancel `stackforge`; drive a fake `input()` script |
| 4 | settings, presets, project bar | change template -> derived layer heights update; preset round-trip |
| 5.1 | host shell + Filaments | absorbed editor works as before |
| 5.2 | Paint | topdeco and surfacecolor outputs byte-identical to the CLI for the same args |
| 5.3-5.6 | remaining tabs | every subcommand reachable (the drift test is the coverage proof) |
| 6 | polish and docs | manual pass |

"Byte-identical to the CLI" is the success definition for every generated tab: the GUI only builds
argv, so the same inputs must produce the same 3MF.

## Side-note: headless slice check (not scheduled)

CLAUDE.md documents a headless Flash Studio slice (`run.sh --datadir <scratch> --slice 1
--outputdir <dir> file.3mf`). A "Verify slice" button after any 3MF-producing run could call it
through the same runner and report the share of extruded pixels on the designed tool. It needs the
Flash Studio path as a setting, a scratch datadir, and a G-code/pixel checker that exists only as an
ad-hoc procedure today. Worth doing after the tabs; nothing above depends on it.
