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
part's symbol, footprint, and 3D model into the project-local libraries
(`0_<project>.kicad_sym`, `0_<project>.pretty/`, `0_<project>.3dshapes/`,
registered in `sym-lib-table`/`fp-lib-table`), then offers to swap the imported
footprint for one from KiCad's standard libraries: candidates are matched by
package + pad count, ranked by actual pad-area overlap, and shown in a terminal
chooser (arrow keys flip between origin/scale-aligned previews, `space` peeks at
the EasyEDA original, `r` rotates, `f` shows the back, `?` for help). Substituting
writes the standard `lib:footprint` into the symbol, records any 90° orientation
difference as an `FT Rotation Offset` field (used by the JLCPCB Fabrication
Toolkit), and deletes the now-unused generated files. Useful flags: `--project`
(point at / disambiguate a `.kicad_pro`), `--lib-name`, `--auto-single`,
`--no-standard-footprints`, `--overwrite`.

The code lives in `easyeda-import/` (the `bin/` entry is a symlink); its offline
tests run with `uv run easyeda-import/test_easyeda_import.py`.

## Bootstrapping a project

From the root of your KiCad project repo (a git repo), run `bin/kkh-bootstrap`.
Then:

```
mkdir -p pcbs/<board>        # each board gets its own dir with .kicad_pro/.kicad_sch/.kicad_pcb
bb list                      # list discovered boards
bb check [board]             # omit board to check all
bb build [board] [--force]   # omit board to build all
```

## Developing this repo

`bb format` (from this repo root) formats the `project-tasks/` Clojure sources with cljfmt.
