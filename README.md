# Kevin's KiCad helpers.

These are some helpers I've unashamedly vibe-coded to help streamline my KiCad workflows.
I haven't been able to write up a proper overview/README yet, so until then please see [my newsletter article for more details](https://kevinlynagh.com/newsletter/2026_07_kevins_kicad_helpers/).

These tools work well enough for me, and I'm sharing them more as a "these fell off the back of a truck, maybe you'll find them helpful" rather than as "I Run an Impressive and Important Open Soruce Project".

I'm happy to discuss ideas and collaborate on PRs *with humans* insomuch as our needs overlap.
I'm running KiCad 10 on MacOS and the scripts seem to mostly work for KiCAD 10 on Debian Linux as well (where I have LLM agents use 'em).

In terms of structure:

- `bin/` has symlinks to one-off tools, just put this on your `$PATH`.
- `project-tasks/` are helpers to build KiCad projects.
- everything else is a tool-specific folder, most of which have verbose, LLM-written READMEs.


## Install

These scripts rely on [mise-en-place](https://mise.jdx.dev) and [UV](https://docs.astral.sh/uv/getting-started/installation/) (TODO: specify latter with former and verify it works with shebangs if UV is not globally available.)

I vendor this repository as a git submodule in my project repositories.
I add to those project's root `mise.toml` files the following:

```toml
[env]
_.path = ["{{config_root}}/vendor/kevins-kicad-helpers/bin"]
```



## ---LLM-generated text follows---


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

## Project tasks

A KiCad project is any directory with a `pcbs/` folder; each board lives in its own subdirectory:

    mkdir -p pcbs/<board>        # each board gets its own dir with .kicad_pro/.kicad_sch/.kicad_pcb
    kkh list                     # list discovered boards
    kkh check [board]            # omit board to check all
    kkh build [board] [--force]  # omit board to build all

The `kkh` commands work from any directory inside the project (they walk up to the nearest ancestor with a `pcbs/` folder).
The project itself needs no configuration — no bb.edn, no tool pins — just `kkh` reachable on PATH, vendored or systemwide.
If you vendor per project with mise, this is the whole setup:

    git submodule add <this repo's url> vendor/kevins-kicad-helpers

    # mise.toml
    [env]
    _.path = ["{{config_root}}/vendor/kevins-kicad-helpers/bin"]

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
