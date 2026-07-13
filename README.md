# Kevin's KiCad helpers

These are some helpers I've unashamedly vibe-coded to help streamline my KiCad workflows.
See [my newsletter article for more details](https://kevinlynagh.com/newsletter/2026_07_kevins_kicad_helpers/).

These tools work well enough for me, and I'm sharing them more as "these fell off the back of a truck, maybe you'll find them helpful" rather than as "I Want To Run an Impressive, Popular, And Important Open Source Project".

I'm happy to discuss ideas and collaborate on PRs *with humans* in-so-much as our needs overlap.
I'm running KiCad 10 on MacOS and the scripts seem to mostly work for KiCAD 10 on Debian Linux as well (where I have LLM agents use 'em).

In terms of structure:

- `bin/` has symlinks to one-off tools, just put this on your `$PATH`.
- `project-tasks/` are helpers to build KiCad projects.
- everything else is a tool-specific folder, most of which have verbose, LLM-written READMEs (I wrote 100% of *this* README, though, with my lumpy human brain.)


## Install

These scripts rely on:

- [mise-en-place](https://mise.jdx.dev)
- [UV](https://docs.astral.sh/uv/getting-started/installation/)

and within KiCad, the plugins:

- [bennymeg/Fabrication-Toolkit](https://github.com/bennymeg/Fabrication-Toolkit)
- [CDFER/JLCPCB-Kicad-Library](https://github.com/CDFER/JLCPCB-Kicad-Library)

I vendor this repository as a git submodule in my project repositories and add it to the path via the project's root `mise.toml`:

```toml
[env]
_.path = ["{{config_root}}/vendor/kevins-kicad-helpers/bin"]
```

## Available tools/helpers

### kkh build

Run `kkh build` in a folder and it'll create an output directory next to every `*.kicad_pro` it finds in any subfolder.
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

The build script also exposes the version string as a KiCad variable, so add `${KKH_VERSION_DATE}` to your PCB silkscreen to get the version in the output Gerber files.


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
This script downloads CDFER's [daily JLCPCB parts sqlite database](https://github.com/CDFER/jlcpcb-parts-database) and consolidates everything into a single table with a numeric price column and lots of indexes so that searching is fast:

https://github.com/user-attachments/assets/5b1b1c08-0cfe-4aba-8c3a-ec0e257e46ca

I use [DB Browser for SQLite](https://sqlitebrowser.org) but you can of course use whatever interface you like.

It's also extremely useful to point LLM agents at this database:

> My dude, I need an H-bridge that can drive +/- 30 Volts, please query ~/foo/bar/parts.db and give me a table with 5 options for integrated ones showing price / stock / description. Please also make a table showing options for drivers with external transistors. Include links to the datasheets.


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
