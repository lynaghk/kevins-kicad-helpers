# kicad-dxf-import

Import a DXF file into a specific layer of the currently-open **KiCad 10** PCB,
wrapping the imported geometry in a **group** tagged with an **ID**. Re-running
the import replaces the previous geometry instead of duplicating it — an
idempotent "sync this DXF into the board".

Neither `kicad-cli` nor the KiCad IPC API can import DXF, so this tool parses the
DXF itself (via [`ezdxf`](https://ezdxf.mozman.at/)) and creates native KiCad
graphic shapes over the IPC API (via [`kicad-python`](https://gitlab.com/kicad/code/kicad-python) / `kipy`).

## Requirements

- **KiCad 10** running, with the IPC API enabled: *Preferences → Plugins →
  Enable IPC API*, and the target board open.
- [`uv`](https://docs.astral.sh/uv/) — dependencies are declared inline (PEP 723),
  so there is nothing to install: `uv run` fetches `kicad-python` and `ezdxf`
  on first use.

## Usage

```sh
# one-shot: import a single file
uv run kicad_dxf_import.py <id>_<Layer>.dxf [options]

# watch: keep a whole directory of DXFs in sync with the board
uv run kicad_dxf_import.py --watch <dir> [options]
```

The filename encodes both the group **ID** and the target **layer**, split on the
**last** underscore:

| Filename                 | ID         | Layer       |
|--------------------------|------------|-------------|
| `panel_Edge.Cuts.dxf`    | `panel`    | `Edge.Cuts` |
| `frontplate_User.1.dxf`  | `frontplate` | `User.1`  |
| `logo_top_F.SilkS.dxf`   | `logo_top` | `F.SilkS`   |

The layer may be either the canonical/file name (`F.SilkS`, `Dwgs.User`) or the
display name KiCad shows in the GUI (`F.Silkscreen`, `User.Drawings`), matched
case-insensitively against the board's actual layers — including custom names. An
unrecognised or not-enabled layer is an error (it lists the available ones). IDs
may contain underscores; canonical layer names never do.

### Options

| Option                   | Default | Description                                                                |
|--------------------------|---------|----------------------------------------------------------------------------|
| `--watch`                | off     | Watch a directory and re-sync DXFs as they change/appear                   |
| `--interval SECONDS`     | `1.0`   | Polling interval in watch mode                                             |
| `--offset-x MM`          | `0`     | X offset added in KiCad space                                              |
| `--offset-y MM`          | `0`     | Y offset added in KiCad space                                              |
| `--scale FACTOR`         | `1.0`   | Multiply DXF coords (use `25.4` for an inch DXF)                           |
| `--line-width MM`        | `0.1`   | Stroke width for imported graphics                                         |
| `--flatten MM`           | `0.02`  | Max deviation when flattening splines/ellipses                             |
| `--include-construction` | off     | Import construction geometry too (see below)                               |
| `--no-dedupe`            | —       | Keep coincident duplicate shapes (dedupe is on by default)                 |
| `--id ID`                | —       | Override the ID parsed from the filename                                   |
| `--layer NAME`           | —       | Override the layer parsed from the filename                                |
| `--no-replace`           | —       | Add a new group even if one with this ID exists (replace is on by default) |
| `--save`                 | off     | Save the board after importing                                             |

## Watch mode

`--watch <dir>` keeps the board in sync with a folder of DXFs:

- On start it imports every `*.dxf` in the directory, then polls (every
  `--interval` seconds) for changes.
- When a file changes — or a **new** DXF appears — it is re-imported. Because each
  import replaces the group with the matching ID, re-exporting from your CAD tool
  just updates the geometry in place.
- Files that don't match `<id>_<Layer>.dxf` are skipped with a note; a file that's
  mid-write (not yet parseable) is retried on the next poll.
- It connects to KiCad lazily and reconnects on its own, so you can start it before
  KiCad (or leave it running across board reloads). `Ctrl-C` stops it.
- Deleting a DXF stops tracking it but leaves its group in the board (delete the
  group yourself if you want it gone).

```sh
uv run kicad_dxf_import.py --watch ./enclosure --line-width 0.15
```

## Behaviour

- **Group + ID.** Imported shapes are grouped; the group's *name* is the ID. On
  re-import, the existing group with that ID (and its shapes) is removed first,
  then the fresh geometry is laid down. This takes two undo steps (the shapes,
  then the group) because KiCad can only put shapes in a group once they already
  exist on the board. The board is not saved unless you pass `--save`, so you can
  review (or undo) first.
- **Entity mapping.** `LINE → segment`, `CIRCLE → circle`, `ARC → arc`,
  `LWPOLYLINE`/`POLYLINE` → segments (bulge spans become arcs), and
  `SPLINE`/`ELLIPSE` are flattened to segments. `TEXT`/`MTEXT` are skipped.
- **Construction geometry is excluded** by default: anything on the `Defpoints`
  layer, on an off/frozen layer, or drawn with a non-solid linetype
  (dashed/center/hidden/phantom). Pass `--include-construction` to keep it.
- **Duplicates are dropped** by default: some CAD exports contain overlapping
  geometry (e.g. a closed outline plus an open polyline retracing part of it),
  which would otherwise import twice. Pass `--no-dedupe` to keep them.
- **Orientation.** DXF is Y-up, KiCad is Y-down; Y is negated on import so the
  result matches the source drawing.

## Development

```sh
uv run test_kicad_dxf_import.py   # offline tests (no running KiCad needed)
uv run samples/make_samples.py    # regenerate the sample DXF fixtures
```

`samples/` contains fixtures exercising each path (lines+arcs, circles, bulge
polylines, splines). The offline tests also run a regression against a real board
outline if `~/incubator/plate-reader/enclosure/board_Edge.Cuts.dxf` is present.
