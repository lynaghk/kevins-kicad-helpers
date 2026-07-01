# Kevin's KiCad helpers.

Intended use case is to add this repo as a git submodule to your KiCad project repo

- `bin/` has one-off tools you can invoke wherever.
- `project-tasks/` is a suite of Babashka helpers to check and build KiCad projects.

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
