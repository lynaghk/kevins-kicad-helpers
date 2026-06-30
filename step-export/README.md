# kicad-step-export

`export_simplified_step.py` — export a **STEP** file from a KiCad PCB where the
board is kept at full fidelity but every component's 3D model is replaced by its
**bounding box**, with identical components sharing geometry (instancing). For
rendering / interaction performance.

## How it works

1. Runs KiCad's own `kicad-cli pcb export step` to produce a normal full export:
   board body (+ silkscreen / copper / soldermask) **and** the real 3D component
   models, already placed and already *instanced* (KiCad defines each unique
   model once and references it for every placement).
2. Post-processes that STEP file: for each **component** solid it computes the 3D
   bounding box from the solid's own points and swaps the detailed B-rep for a
   single box — **once per unique component**, so all placements inherit the box.
   The board is detected by KiCad's fixed sub-part names (`*_PCB`, `*_copper`,
   `*_pad`, `*_via`, `*_silkscreen`, `*_soldermask`) and left untouched.
3. Garbage-collects the now-unreferenced detailed geometry so the file shrinks.

Because we post-process KiCad's output, we never parse the `.kicad_pcb` format,
never resolve 3D-model paths, and never reimplement KiCad's coordinate / rotation
math. Placements and instancing come straight from KiCad and are preserved
verbatim. The script is pure Python (stdlib only) — no CAD kernel required.

## Usage

```bash
# From a board (runs kicad-cli for you; needs the 3D models installed):
uv run export_simplified_step.py board.kicad_pcb -o board.simplified.step

# From an existing full STEP export (skips kicad-cli entirely):
uv run export_simplified_step.py already_exported.step -o out.step
```

Useful flags: `--no-copper`, `--no-soldermask`, `--no-silkscreen` (lighter board),
`--include-dnp`, `--keep-full` (keep the intermediate full export),
`--board-name PREFIX` (override board-vs-component detection), `--quiet`.

Requirements: `kicad-cli` (KiCad 9/10+) on `PATH`, and [`uv`](https://docs.astral.sh/uv/).
The 3D component models must be available when exporting from a `.kicad_pcb`
(KiCad silently omits components whose models it can't find).

## Notes / limitations

- Bounding boxes are derived from the points in each model's STEP geometry; for
  curved/spline surfaces this can be very slightly larger than the true extent
  (safe for a bounding-box proxy).
- Simplified boxes carry no color/material (they render in the viewer's default).
- The file shrinks by the amount of *component* geometry removed; if you keep
  full copper, the copper geometry dominates the size — use `--no-copper` for a
  much smaller file.
- Validated structurally (well-formed STEP, no dangling refs, closed-manifold
  boxes, instancing preserved). Open the result in KiCad's 3D viewer or a CAD
  tool to confirm visually.
