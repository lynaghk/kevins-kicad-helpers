# KiCad Parser

Small JVM Clojure tool for exporting KiCad schematic netlists and loading component data into DataScript.

## Setup

    mise trust
    mise install
    scripts/check.sh

## Usage

Print the KiCad S-expression netlist for a schematic:

    clojure -M -m kicad-parser.core path/to/design.kicad_sch

Build a DataScript database from Clojure:

    (require '[kicad-parser.core :as kicad])
    (def db (kicad/schematic->db "path/to/design.kicad_sch"))

On Linux the tool prefers the KiCad Flatpak command.

On macOS it looks for the KiCad app bundle CLI, then falls back to `kicad-cli` on `PATH`.
