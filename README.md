# Kevin's KiCad helpers

These are some helpers I've unashamedly vibe-coded to help streamline my KiCad workflows.
See [my newsletter article for more details](https://kevinlynagh.com/newsletter/2026_07_kevins_kicad_helpers/).

These tools work for me, and I'm sharing them in case they're helpful to you too.
I'm running KiCad 10 on MacOS and have LLM agents running them on KiCAD 10 in a Debian Linux sandbox too.

I'm happy to discuss ideas and collaborate on PRs *with humans*.

In terms of structure:

- `bin/` has symlinks/binstubs to one-off tools, just put this on your `$PATH`.
- `project-tasks/` are helpers to build KiCad projects.
- everything else is a tool-specific folder, most of which have verbose, LLM-written READMEs (I wrote 100% of *this* README, though, with my lumpy human brain.)


## Install

These scripts rely on [mise-en-place](https://mise.jdx.dev) and, within KiCad, the plugins:

- [bennymeg/Fabrication-Toolkit](https://github.com/bennymeg/Fabrication-Toolkit)
- [CDFER/JLCPCB-Kicad-Library](https://github.com/CDFER/JLCPCB-Kicad-Library)

I vendor this repository as a git submodule in my project repositories and add it to the path via the project's root `mise.toml`:

```toml
[env]
_.path = ["{{config_root}}/vendor/kevins-kicad-helpers/bin"]
```

## Available tools/helpers

### kkh build

Run `kkh build` anywhere in a git repository and it'll create an output directory next to every `*.kicad_pro` it finds under that repository.
The outputs are named with the date, git revision, and also indicate whether there are unstaged changes in the repository working tree:

```
2026-07-07-73c5c1
├── bom.csv
├── designators.csv
├── netlist.ipc
├── positions.csv
├── receiver-gerbers-2026-07-07-73c5c1.zip
├── receiver.full.step
├── receiver.simplified.step
└── schematics
    ├── receiver-pcb-back.pdf
    ├── receiver-pcb-front.pdf
    └── receiver-sch.pdf
```

This one command runs schematic DRC, PCB ERC, and custom analysis checks, then builds all of the output files required to place a JLCPCB assembly order.
(Run `kkh check` if you want everything but the build.)
Run `kkh list` to see the repo-relative board names you can pass to `kkh build <board>` or `kkh check <board>`.

To retire a board without deleting it (e.g. an obsolete revision you want to keep browsable), create an empty `.kkh-skip` file in its directory.
Argless `kkh build` and `kkh check` then leave that board alone, `kkh list` shows it as `(skipped)`, and naming it explicitly still builds it.

The build script also exposes the version string as a KiCad variable, so add `${KKH_VERSION_DATE}` to your PCB silkscreen to get the version in the output Gerber files.


### kkh macos-opener

MacOS makes it hard to open multiple instances of the same application, which makes it difficult to compare multiple KiCad projects and copy schematics/layout between them.
Run `kkh macos-opener install` to create `/Applications/KiCad New Instance.app` and set it as the default application for `.kicad_pro` files, so you can easily open as many KiCad project instances as you want.
Run `kkh macos-opener uninstall` to set the default program back to `KiCad.app`.


### kkh-dxf-import

I specify all of my mechanical stuff --- board outlines, mounting hole positions, etc. --- in Autodesk Inventor since it has a constraint solver and allows me to directly reference complex geometry driven by other objects.

KiCad's GUI has a DXF import tool, but it doesn't have a mechanism to *replace* already imported geometry, which makes it tedious to iterate.

This script:

- imports geometry from a DXF file into a board layer specified by the filename (`panel_Edge.Cuts.dxf` imports a group ID'd "panel" to the Edge Cuts layer)
- deletes any geometry previously imported with that ID
- polls the file for changes, so you get "live reload"

I combine this with a similar poll + export-to-file script in my mechanical CAD tool (shown above) to get live syncing to KiCad (shown below):

https://github.com/user-attachments/assets/40c2ba55-c334-4d1a-9740-b980dbfd4e7f

DXFs map into KiCad's origin at top-left of the page outline and the DXF positive Y direction points up, so most of my PCBs end up drawn outside of the page `¯\_(ツ)_/¯`.


### kkh-import-easyeda-parts

A wrapper around uPesy's [easyeda2kicad.py](https://github.com/uPesy/easyeda2kicad.py) that tries to match a part with an existing symbol/footprint that's in the KiCad standard library.
Potential matches are shown via terminal UI so you can interactive select the best one:

https://github.com/user-attachments/assets/69aff150-4cd5-4d72-a978-02896a938bb6

Schematic and footprint libraries are created as `0_<project-name>` with the `0_` prefix so that the library shows up first in all of the lists.


### kkh-download-jlcpcb-parts-database

I design all of my boards to be assembled by JLCPCB, so it's *extremely* helpful to have a low-latency way to query their parts.
This script downloads the [jlcparts](https://github.com/yaqwsx/jlcparts) daily JLCPCB parts sqlite database and consolidates everything into a `components` table and lots of indexes so that searching is fast.
It also parses resistor, capacitor, and inductor values into tables with SI-base-unit REAL columns, so parametric queries like `farads = 100e-9 AND voltage_v >= 16` work with plain SQL.

https://github.com/user-attachments/assets/5b1b1c08-0cfe-4aba-8c3a-ec0e257e46ca

The database is written to `jlcpcb_parts.db`, or to the file you give as the first argument:

    kkh-download-jlcpcb-parts-database ~/foo/bar/parts.db

I use [DB Browser for SQLite](https://sqlitebrowser.org) but you can of course use whatever interface you like.

It's also extremely useful to point LLM agents at this database:

> My dude, I need an H-bridge that can drive +/- 30 Volts, please query ~/foo/bar/jlcpcb_parts.db and give me a table with 5 options for integrated drivers showing price / stock / description. Please also make a table showing options for drivers with external transistors. Include links to the datasheets.


### kkh-analyze-schematic

This tool runs KiCad to generate a netlist, then imports it into a graph database and runs various checks.

When I get some time, I might design a little language that can live in schematic text boxes to specify invariants to check.

For now, though, it's hardcoded with the following:

- print all i2c addresses (specified via the `i2c` property on schematic instances) and throw an error if any address maps to distinct instances.
- print total current (specified via the `max_mA` property on schematic instances)
- check that total capacitance on a node named `VCC` or `VBUS` is less than 10uF

See the [newsletter article](https://kevinlynagh.com/newsletter/2026_07_kevins_kicad_helpers/#schematic-analysis) for a bit more about how this works internally.
(Or, you know, read the code --- Clojure's a pretty cool language, you might enjoy it!)


### kkh-step-export

This script simplifies a KiCad-exported STEP file by replacing all components with their bounding boxes.

![](https://kevinlynagh.com/newsletter/2026_07_kevins_kicad_helpers/step_simplification.jpg)

This a huge relief to my M1 Macbook Air running Windows CAD software in a virtual machine, which does *not* do well when every pin of every chip is a separate solid body.


## TODO

- specify UV via Mise and verify it works with shebangs if UV is not globally available.
