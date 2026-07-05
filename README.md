# Kevin's KiCad helpers.

Intended use case is to add this repo as a git submodule to your KiCad project repo

- `bin/` has one-off tools you can invoke wherever.
- `project-tasks/` is a suite of Babashka helpers to check and build KiCad projects.

## Importing parts from EasyEDA/LCSC

From anywhere inside your KiCad project, run:

```
bin/kkh-import-easyeda-parts C5123975 C3681116   # LCSC part numbers
```

This uses [easyeda2kicad](https://github.com/uPesy/easyeda2kicad.py) to pull each
part's symbol, footprint, and 3D model, then walks you through confirming each
part before anything is written. Nothing touches your project until you press
Enter: downloads are staged in a temp dir, and the footprint chooser doubles as
the import gate — Enter imports the part with the highlighted footprint, `q`/Esc
skips it entirely. Confirmed parts land in the project-local libraries
(`0_<project>.kicad_sym`, `0_<project>.pretty/`, `0_<project>.3dshapes/`,
registered in `sym-lib-table`/`fp-lib-table`).

Before downloading, each part number is checked against libraries you already
have: parts already in the project library are skipped (reimport with
`--overwrite`), and parts provided by an installed KiCad library — e.g.
[JLCPCB-Kicad-Library](https://github.com/CDFER/JLCPCB-Kicad-Library), whose
symbols carry LCSC part numbers — are offered as-is (`skip and place
PCM_JLCPCB-…:… directly`, the default) or imported as a copy anyway. Detection
reads the global `sym-lib-table` plus KiCad's PCM `3rdparty/symbols` tree.

The chooser offers KiCad's standard footprints in place of the generated one:
candidates are matched by package + pad count, ranked by actual pad-area
overlap, and shown as origin/scale-aligned previews (arrow keys compare,
`space` peeks at the EasyEDA original, `r` rotates, `f` shows the back, `?` for
help). Choosing a standard footprint writes its `lib:footprint` into the symbol,
records any 90° orientation difference as an `FT Rotation Offset` field (used by
the JLCPCB Fabrication Toolkit), and never copies the generated files at all. It
also checks KiCad's standard _symbol_ libraries by MPN and tells you when the
part already ships with KiCad (e.g. `Regulator_Linear:AMS1117-3.3`) — meaning
you may not need the import at all. Useful flags: `--project` (point at /
disambiguate a `.kicad_pro`), `--lib-name`, `--auto-single`,
`--non-interactive` (no prompts — skip installed duplicates, import the rest
with their generated footprints, print the candidate/symbol report; good for
scripted or agent-driven analysis), `--import-duplicates`,
`--no-standard-footprints`, `--no-standard-symbols`, `--kicad-footprints-dir`,
`--kicad-symbols-dir`, `--kicad-config-dir`, `--overwrite`.

Imported two-pin resistors and capacitors are restyled to match the look of the
JLCPCB-Kicad-Library above: pin numbers hidden, the value parsed from the part
description (`2kΩ`, `1nF`) shown as the schematic Value, and capacitors showing
their rated voltage below it.
Resistor networks, polarized capacitors, and parts whose description doesn't
parse keep their generated symbols; `--no-passive-style` turns the restyling off
entirely.

The code lives in `easyeda-import/` (the `bin/` entry is a symlink); its offline
tests run with `uv run easyeda-import/test_easyeda_import.py`.

## KiCad tool shims

`bin/` also provides `kicad-cli`, `kicad-python`, and `fabrication-toolkit`
shims that locate the real tools per platform (macOS app bundle, Linux
system install, or the org.kicad.KiCad Flatpak), so the project tasks work
without any machine-specific setup.

- `kicad-cli` — macOS app bundle (with fontconfig setup) → `kicad-cli` on
  PATH → Flatpak. Override with `KICAD_CLI`.
- `kicad-python` — a Python that can import KiCad's `pcbnew`/`wx`: the macOS
  app bundle's framework Python, otherwise the system `python3`.
  Override with `KICAD_PYTHON`.
- `fabrication-toolkit` — runs the CLI of the
  [Fabrication Toolkit](https://github.com/bennymeg/JLC-Plugin-for-KiCad)
  plugin, which must be installed via KiCad's Plugin and Content Manager.
  The shim finds it in the PCM plugin dir for the installed KiCad version on
  any platform. Override the plugin dir with `FABRICATION_TOOLKIT_DIR`; set
  `FABRICATION_TOOLKIT_FLATPAK=1` to run it inside the KiCad Flatpak.

## Bootstrapping a project

From the root of your KiCad project repo (a git repo), run `bin/kkh-bootstrap`.
Then:

    mkdir -p pcbs/<board>        # each board gets its own dir with .kicad_pro/.kicad_sch/.kicad_pcb
    kkh list                     # list discovered boards
    kkh check [board]            # omit board to check all
    kkh build [board] [--force]  # omit board to build all

The `kkh` commands work from any directory inside the project.
The consuming repo needs no bb.edn and no Babashka install: `kkh` brings its own (via `bb` on PATH or the version pinned in this repo's mise.toml, fetched through `mise x`).

## Developing this repo

Each tool lives in its own top-level folder (`easyeda-import/`, `dxf-import/`,
`analyzer/`, `step-export/`, `project-tasks/`) and owns its dev entry points as
executable stubs under `<tool>/bin/`:

- `bin/test` — run the tool's test suite
- `bin/format` — format the tool's sources

From the repo root, `bb test` / `bb format` run the matching stub in every folder
that has one (via `scripts/run-all.bb`) and print a per-tool summary. The stubs
are language agnostic — bash wrapping `uv run`, cljfmt, `clojure -M:test`,
whatever — they just have to be executable. A stub exiting with code 125 is
reported as SKIP without failing the run (e.g. `analyzer/bin/test` when the
`clojure` CLI isn't installed). To add tests or formatting to a tool, drop in a
stub; nothing else needs wiring.

Python is the exception: it is formatted repo-wide rather than per tool.
`scripts/format-python` (also run by `bb format`) ruff-formats every tracked or
untracked-but-not-ignored `*.py` using the config in `ruff.toml`, so a new
helper's Python is picked up automatically — no stub needed.
