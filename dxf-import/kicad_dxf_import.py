#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["kicad-python>=0.7", "ezdxf>=1.1"]
# ///
"""Import a DXF file into a specific layer of the currently-open KiCad 10 PCB.

The imported geometry is wrapped in a *group* whose name is an *ID* derived from
the DXF filename, so that re-running the import replaces the previous geometry
instead of duplicating it (an idempotent "sync this DXF into the board").

Filename convention::

    <id>_<KiCadLayerName>.dxf

e.g. ``panel_Edge.Cuts.dxf`` -> id="panel", layer="Edge.Cuts".
The split is on the *last* underscore, so the id may contain underscores
(canonical KiCad layer names never do).

Requires a running KiCad 10 with the IPC API server enabled
(Preferences -> Plugins -> Enable IPC API) and the target board open.
"""
from __future__ import annotations

import argparse
import math
import sys
import textwrap
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, List, Sequence, Tuple

Point = Tuple[float, float]


# --------------------------------------------------------------------------- #
# Pure helpers (no KiCad / kipy dependency -- unit-testable offline)
# --------------------------------------------------------------------------- #

def parse_filename(path: Path) -> Tuple[str, str]:
    """Return (id, layer_name) parsed from ``<id>_<Layer>.dxf``.

    Splits on the last underscore: the id may contain underscores, the
    canonical KiCad layer name never does.
    """
    stem = path.stem  # strips the final ".dxf"
    if "_" not in stem:
        raise ValueError(
            f"Filename {path.name!r} does not match '<id>_<Layer>.dxf' "
            f"(no underscore separating id and layer)."
        )
    id_, layer_name = stem.rsplit("_", 1)
    if not id_ or not layer_name:
        raise ValueError(
            f"Filename {path.name!r} does not match '<id>_<Layer>.dxf'."
        )
    return id_, layer_name


@dataclass(frozen=True)
class Transform:
    """Maps DXF coordinates (mm, Y-up) to KiCad coordinates (mm, Y-down).

    KiCad PCB space is Y-down, DXF is Y-up, so Y is negated to preserve the
    drawing's appearance. Scale is applied first, offsets last (in KiCad space).
    """

    scale: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0

    def pt(self, x: float, y: float) -> Point:
        return (x * self.scale + self.offset_x, -(y * self.scale) + self.offset_y)

    def length(self, value: float) -> float:
        return value * self.scale


# Primitives are geometry already expressed in KiCad mm coordinates. Keeping
# them as plain data (not kipy objects) makes the conversion testable without a
# running KiCad.
@dataclass
class Segment:
    start: Point
    end: Point
    kind: str = "segment"


@dataclass
class Arc:
    start: Point
    mid: Point
    end: Point
    kind: str = "arc"


@dataclass
class Circle:
    center: Point
    radius: float
    kind: str = "circle"


Primitive = object  # Segment | Arc | Circle


def _bulge_arc_points(p1: Point, p2: Point, bulge: float) -> Tuple[Point, Point, Point]:
    """3-point (start, mid, end) representation of a polyline bulge arc.

    bulge = tan(theta/4) where theta is the signed included angle (positive = CCW).
    The arc midpoint is offset from the chord midpoint by the sagitta along the
    *right-hand* normal of the chord direction: a positive (CCW) bulge curves to
    that side. (Using the left normal reverses the arc -- the classic mistake.)
    """
    (x1, y1), (x2, y2) = p1, p2
    dx, dy = x2 - x1, y2 - y1
    chord = math.hypot(dx, dy)
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    sagitta = bulge * (chord / 2.0)
    nx, ny = dy / chord, -dx / chord  # right-hand normal of the chord direction
    mid = (mx + nx * sagitta, my + ny * sagitta)
    return p1, mid, p2


def _arc_three_points(
    cx: float, cy: float, r: float, start_deg: float, end_deg: float
) -> Tuple[Point, Point, Point]:
    """3-point representation of a DXF ARC (angles CCW, in degrees)."""
    sweep = (end_deg - start_deg) % 360.0
    if sweep == 0.0:
        sweep = 360.0
    angles = (start_deg, start_deg + sweep / 2.0, end_deg)
    pts = [
        (cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)))
        for a in angles
    ]
    return pts[0], pts[1], pts[2]


def _polyline_spans(
    points: Sequence[Tuple[float, float, float]], closed: bool
) -> Iterator[Tuple[Point, Point, float]]:
    """Yield (p1, p2, bulge) spans for a polyline given (x, y, bulge) vertices."""
    n = len(points)
    last = n if closed else n - 1
    for i in range(last):
        x1, y1, b = points[i]
        x2, y2, _ = points[(i + 1) % n]
        yield (x1, y1), (x2, y2), b


# Linetypes that denote construction / annotation geometry rather than real
# board outline. Solid geometry resolves to "CONTINUOUS" (or "SOLID").
_SOLID_LINETYPES = {"", "CONTINUOUS", "SOLID", "BYBLOCK"}


def _resolve_linetype(e) -> str:
    """Effective linetype name of an entity (resolving BYLAYER), upper-cased."""
    lt = (e.dxf.get("linetype", "BYLAYER") or "BYLAYER").upper()
    if lt == "BYLAYER":
        doc = getattr(e, "doc", None)
        layer = doc.layers.get(e.dxf.layer) if doc and e.dxf.layer in doc.layers else None
        lt = (layer.dxf.linetype or "CONTINUOUS").upper() if layer is not None else "CONTINUOUS"
    return lt


def is_construction(e) -> bool:
    """True if an entity is construction geometry that should not be imported.

    Heuristics (matching how CAD tools mark construction/centerlines):
      * on the AutoCAD non-plotting ``Defpoints`` layer;
      * on a layer that is turned off or frozen;
      * drawn with a non-solid linetype (DASHED, CENTER, HIDDEN, PHANTOM, ...).
    """
    layer_name = e.dxf.layer
    if layer_name.lower() == "defpoints":
        return True
    doc = getattr(e, "doc", None)
    layer = doc.layers.get(layer_name) if doc and layer_name in doc.layers else None
    if layer is not None and (layer.is_off() or layer.is_frozen()):
        return True
    return _resolve_linetype(e) not in _SOLID_LINETYPES


def _round_pt(p: Point) -> Point:
    return (round(p[0], 6), round(p[1], 6))


def _primitive_key(p: Primitive):
    """Geometric identity key for deduplication (direction-independent)."""
    if p.kind == "segment":
        return ("segment", frozenset((_round_pt(p.start), _round_pt(p.end))))
    if p.kind == "arc":
        # an arc and its reverse share the same set of {start, mid, end} points
        return ("arc", frozenset((_round_pt(p.start), _round_pt(p.mid), _round_pt(p.end))))
    return ("circle", _round_pt(p.center), round(p.radius, 6))


def dedupe_primitives(primitives: Iterable[Primitive]) -> List[Primitive]:
    """Drop geometrically coincident primitives, keeping first occurrence.

    Some DXF exports contain overlapping geometry (e.g. a closed outline plus an
    open polyline retracing part of it), which would otherwise import twice.
    """
    seen = set()
    out: List[Primitive] = []
    for p in primitives:
        key = _primitive_key(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def iter_primitives(
    msp,
    tf: Transform,
    flatten_mm: float,
    skipped: Counter,
    include_construction: bool = False,
) -> Iterator[Primitive]:
    """Translate DXF modelspace entities into KiCad-space primitives.

    ``msp`` is an ezdxf modelspace (or any iterable of entities). Construction
    geometry is excluded unless ``include_construction`` is set; unsupported
    entity types are counted in ``skipped`` and otherwise ignored.
    """
    for e in msp:
        dxftype = e.dxftype()

        if not include_construction and is_construction(e):
            skipped[f"{dxftype} (construction)"] += 1
            continue

        if dxftype == "LINE":
            s, en = e.dxf.start, e.dxf.end
            yield Segment(tf.pt(s.x, s.y), tf.pt(en.x, en.y))

        elif dxftype == "CIRCLE":
            c = e.dxf.center
            yield Circle(tf.pt(c[0], c[1]), tf.length(e.dxf.radius))

        elif dxftype == "ARC":
            c = e.dxf.center
            p1, pm, p2 = _arc_three_points(
                c[0], c[1], e.dxf.radius, e.dxf.start_angle, e.dxf.end_angle
            )
            yield Arc(tf.pt(*p1), tf.pt(*pm), tf.pt(*p2))

        elif dxftype == "LWPOLYLINE":
            pts = [(x, y, b) for x, y, _, _, b in e.get_points("xyseb")]
            yield from _spans_to_primitives(_polyline_spans(pts, e.closed), tf)

        elif dxftype == "POLYLINE" and e.get_mode() in (
            "AcDb2dPolyline",
            "AcDb3dPolyline",
        ):
            pts = [
                (v.dxf.location[0], v.dxf.location[1], float(getattr(v.dxf, "bulge", 0.0)))
                for v in e.vertices
            ]
            yield from _spans_to_primitives(_polyline_spans(pts, e.is_closed), tf)

        elif dxftype in ("SPLINE", "ELLIPSE"):
            # No native KiCad equivalent for a general spline/ellipse-arc; flatten
            # to line segments with a bounded deviation.
            verts = [(p[0], p[1]) for p in e.flattening(flatten_mm)]
            for a, b in zip(verts, verts[1:]):
                yield Segment(tf.pt(*a), tf.pt(*b))

        else:
            skipped[dxftype] += 1


def _spans_to_primitives(
    spans: Iterable[Tuple[Point, Point, float]], tf: Transform
) -> Iterator[Primitive]:
    for p1, p2, bulge in spans:
        if abs(bulge) < 1e-12 or math.hypot(p2[0] - p1[0], p2[1] - p1[1]) == 0.0:
            yield Segment(tf.pt(*p1), tf.pt(*p2))
        else:
            s, m, en = _bulge_arc_points(p1, p2, bulge)
            yield Arc(tf.pt(*s), tf.pt(*m), tf.pt(*en))


# --------------------------------------------------------------------------- #
# kipy conversion (needs kicad-python, but not a running KiCad to construct)
# --------------------------------------------------------------------------- #

def primitives_to_shapes(primitives: Iterable[Primitive], layer, width_nm: int):
    """Convert primitives into kipy board shapes on ``layer`` with stroke width."""
    from kipy.board_types import BoardArc, BoardCircle, BoardSegment
    from kipy.geometry import Vector2

    shapes = []
    for p in primitives:
        if p.kind == "segment":
            s = BoardSegment()
            s.start = Vector2.from_xy_mm(*p.start)
            s.end = Vector2.from_xy_mm(*p.end)
        elif p.kind == "arc":
            s = BoardArc()
            s.start = Vector2.from_xy_mm(*p.start)
            s.mid = Vector2.from_xy_mm(*p.mid)
            s.end = Vector2.from_xy_mm(*p.end)
        elif p.kind == "circle":
            s = BoardCircle()
            cx, cy = p.center
            s.center = Vector2.from_xy_mm(cx, cy)
            s.radius_point = Vector2.from_xy_mm(cx + p.radius, cy)
        else:  # pragma: no cover - defensive
            raise ValueError(f"Unknown primitive kind: {p.kind!r}")
        s.layer = layer
        s.attributes.stroke.width = width_nm
        shapes.append(s)
    return shapes


def build_shapes(
    dxf_path: Path,
    layer,
    tf: Transform,
    width_mm: float,
    flatten_mm: float,
    include_construction: bool = False,
    dedupe: bool = True,
):
    """Read a DXF and return (shapes, skipped Counter, duplicate_count)."""
    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    skipped: Counter = Counter()
    primitives = list(
        iter_primitives(doc.modelspace(), tf, flatten_mm, skipped, include_construction)
    )
    duplicates = 0
    if dedupe:
        deduped = dedupe_primitives(primitives)
        duplicates = len(primitives) - len(deduped)
        primitives = deduped
    shapes = primitives_to_shapes(primitives, layer, round(width_mm * 1_000_000))
    return shapes, skipped, duplicates


# --------------------------------------------------------------------------- #
# KiCad IPC import
# --------------------------------------------------------------------------- #

def resolve_layer(board, name: str):
    """Resolve a layer name to a BoardLayer enum against the board's real layers.

    Accepts either the canonical/file name (``F.SilkS``) or the user-visible
    display name (``F.Silkscreen``), case-insensitively, including custom layer
    names. Raises ValueError if no enabled layer matches.
    """
    from kipy.proto.board.board_types_pb2 import BoardLayer
    from kipy.util.board_layer import canonical_name, layer_from_canonical_name

    key = name.strip()
    enabled = board.get_enabled_layers()

    # Cheap path: exact canonical name, no extra IPC calls.
    layer = layer_from_canonical_name(key)
    if layer != BoardLayer.BL_UNKNOWN and layer in enabled:
        return layer

    # Match case-insensitively by canonical or user-visible (display) name.
    target = key.lower()
    available = set()
    for lyr in enabled:
        canon = canonical_name(lyr)
        display = board.get_layer_name(lyr)
        available.add(canon)
        available.add(display)
        if target in (canon.lower(), display.lower()):
            return lyr

    raise ValueError(
        f"unknown layer {name!r}; enable it on the board or use one of: "
        + ", ".join(sorted(available))
    )


def import_dxf(board, group_id: str, shapes, replace: bool):
    """Create ``shapes`` on the board grouped under ``group_id``.

    When ``replace`` is set, an existing group with the same name (and its member
    items) is removed first. Returns ``(removed, created_count)`` where
    ``created_count`` is how many shapes KiCad actually accepted (it can be fewer
    than requested if, say, the target layer is rejected).

    Done in **two** commits on purpose: the shapes must be on the board before the
    group can reference them. ``PCB_GROUP::Deserialize`` resolves its members via
    ``BOARD::ResolveItem``, which only sees items already added to the board -- not
    items still staged in an open commit. Creating the group in the same commit as
    the shapes therefore yields an *empty* group. So we push the shapes first, then
    create the group in a second commit (two undo steps).
    """
    from kipy.board_types import Group

    removed = 0
    # Commit 1: drop any stale group with this ID, then create the shapes.
    commit = board.begin_commit()
    try:
        if replace:
            for g in board.get_groups():
                if g.name == group_id:
                    ids = [item.id for item in g.items]
                    if ids:
                        board.remove_items_by_id(ids)
                    board.remove_items_by_id(g.id)
                    removed += len(ids) + 1

        created = board.create_items(shapes)
        board.push_commit(commit, message=f"Import DXF '{group_id}' (shapes)")
    except Exception:
        board.drop_commit(commit)
        raise

    if not created:
        return removed, 0  # nothing accepted; don't create an empty group

    # Commit 2: group the now-resolvable shapes under the ID.
    commit2 = board.begin_commit()
    try:
        group = Group()
        group._proto.name = group_id  # Group has no name setter; set the proto
        group.items = created
        board.create_items(group)
        board.push_commit(commit2, message=f"Import DXF '{group_id}' (group)")
    except Exception:
        board.drop_commit(commit2)
        # Roll back the orphaned shapes so a re-run starts clean (replace can only
        # find them via the group, which failed to create).
        try:
            board.remove_items(created)
        except Exception:
            pass
        raise

    return removed, len(created)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kicad_dxf_import.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Import a DXF into a layer of the open KiCad 10 PCB as a "
        "tagged, replaceable group. With --watch, keep a directory of DXF files "
        "in sync with the board.",
        epilog=textwrap.dedent(
            """\
            filename convention:
              DXF files are named <id>_<Layer>.dxf, split on the LAST underscore:
                panel_Edge.Cuts.dxf    -> id "panel",      layer "Edge.Cuts"
                frontplate_User.1.dxf  -> id "frontplate", layer "User.1"
                logo_top_F.SilkS.dxf   -> id "logo_top",   layer "F.SilkS"
              The id becomes the group name; re-importing the same id replaces it.

            examples:
              # import one file onto its layer
              uv run kicad_dxf_import.py panel_Edge.Cuts.dxf

              # an inch DXF, shifted, with a wider stroke
              uv run kicad_dxf_import.py logo_F.SilkS.dxf --scale 25.4 --offset-x 10 --line-width 0.15

              # keep a whole directory in sync with the board
              uv run kicad_dxf_import.py --watch ./enclosure

            Requires KiCad 10 running with the IPC API enabled
            (Preferences -> Plugins -> Enable IPC API) and a board open.
            """
        ),
    )
    p.add_argument("path", type=Path, nargs="?", default=None,
                   help="DXF file named <id>_<Layer>.dxf (one-shot), or a "
                        "directory to watch (with --watch; defaults to '.')")
    p.add_argument("--watch", action="store_true",
                   help="Watch a directory and re-sync every <id>_<Layer>.dxf "
                        "file whenever it changes or a new one appears")
    p.add_argument("--interval", type=float, default=1.0,
                   help="Polling interval in seconds for --watch (default 1.0)")
    p.add_argument("--offset-x", type=float, default=0.0,
                   help="X offset in mm added in KiCad space (default 0)")
    p.add_argument("--offset-y", type=float, default=0.0,
                   help="Y offset in mm added in KiCad space (default 0)")
    p.add_argument("--scale", type=float, default=1.0,
                   help="Scale factor for DXF coords; use 25.4 for an inch DXF "
                        "(default 1.0 = DXF units treated as mm)")
    p.add_argument("--line-width", type=float, default=0.1,
                   help="Stroke width in mm for imported graphics (default 0.1)")
    p.add_argument("--flatten", type=float, default=0.02,
                   help="Max deviation in mm when flattening splines/ellipses "
                        "(default 0.02)")
    p.add_argument("--include-construction", action="store_true",
                   help="Import construction geometry too (Defpoints layer, "
                        "off/frozen layers, dashed/center/hidden linetypes). "
                        "By default these are excluded.")
    p.add_argument("--dedupe", action=argparse.BooleanOptionalAction, default=True,
                   help="Drop geometrically coincident duplicate shapes "
                        "(default: on)")
    p.add_argument("--id", dest="id_override", default=None,
                   help="Override the ID parsed from the filename")
    p.add_argument("--layer", dest="layer_override", default=None,
                   help="Override the layer parsed from the filename")
    p.add_argument("--replace", action=argparse.BooleanOptionalAction, default=True,
                   help="Replace an existing group with the same ID (default: on)")
    p.add_argument("--save", action="store_true",
                   help="Save the board after importing")
    return p


def connect_board():
    """Connect to the running KiCad and return its open board.

    Raises kipy ConnectionError (KiCad not running / IPC off) or ApiError
    (no board open).
    """
    from kipy import KiCad

    return KiCad().get_board()


def sync_one(dxf_path: Path, board, args, id_override=None, layer_override=None) -> dict:
    """Import a single DXF into ``board`` and return a result summary.

    Raises ValueError for a bad filename / unknown layer / empty drawing / shapes
    KiCad rejected, ezdxf errors for an unreadable DXF, and kipy errors for board
    problems.
    """
    file_id, file_layer = "", ""
    if id_override is None or layer_override is None:
        file_id, file_layer = parse_filename(dxf_path)  # may raise ValueError
    group_id = id_override or file_id
    layer_name = layer_override or file_layer

    layer = resolve_layer(board, layer_name)  # may raise ValueError

    tf = Transform(scale=args.scale, offset_x=args.offset_x, offset_y=args.offset_y)
    shapes, skipped, duplicates = build_shapes(
        dxf_path, layer, tf, args.line_width, args.flatten,
        include_construction=args.include_construction, dedupe=args.dedupe,
    )
    if not shapes:
        raise ValueError("no importable geometry found")

    removed, created = import_dxf(board, group_id, shapes, args.replace)
    if created == 0:
        raise ValueError(
            f"KiCad rejected all {len(shapes)} shape(s) for layer {layer_name!r} "
            "(is that layer enabled on this board?)"
        )
    if args.save:
        board.save()

    return {
        "group_id": group_id, "layer_name": layer_name,
        "shapes": created, "requested": len(shapes),
        "removed": removed, "skipped": skipped, "duplicates": duplicates,
    }


_CONNECT_HELP = (
    "Is KiCad 10 running with the IPC API enabled "
    "(Preferences -> Plugins -> Enable IPC API) and a board open?"
)


def run_once(args) -> int:
    from kipy.errors import ApiError
    from kipy.errors import ConnectionError as KiCadConnectionError

    if args.path is None:
        print("error: a DXF file is required (or use --watch <dir>)", file=sys.stderr)
        return 2
    if not args.path.exists():
        print(f"error: DXF file not found: {args.path}", file=sys.stderr)
        return 2

    try:
        board = connect_board()
    except (KiCadConnectionError, ApiError) as e:
        print(f"error: could not connect to KiCad ({e}).\n{_CONNECT_HELP}", file=sys.stderr)
        return 3

    try:
        r = sync_one(args.path, args=args, board=board,
                     id_override=args.id_override, layer_override=args.layer_override)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - surface DXF read errors cleanly
        print(f"error: could not read {args.path.name}: {e}", file=sys.stderr)
        return 1

    if sum(r["skipped"].values()):
        summary = ", ".join(f"{k} x{v}" for k, v in r["skipped"].items())
        print(f"note: skipped entities: {summary}", file=sys.stderr)
    if r["duplicates"]:
        print(f"note: dropped {r['duplicates']} duplicate shape(s)", file=sys.stderr)

    notes = []
    if r["removed"]:
        notes.append(f"replaced {r['removed']} stale item(s)")
    if r["shapes"] < r["requested"]:
        notes.append(f"{r['requested'] - r['shapes']} rejected by KiCad")
    note = f" ({'; '.join(notes)})" if notes else ""
    print(
        f"Imported {r['shapes']} shape(s) from {args.path.name} into "
        f"group '{r['group_id']}' on {r['layer_name']}{note}."
    )
    return 0


def _sync_summary(name: str, r: dict) -> str:
    extra = []
    if r["removed"]:
        extra.append(f"replaced {r['removed']}")
    if r["shapes"] < r["requested"]:
        extra.append(f"{r['requested'] - r['shapes']} rejected")
    if r["duplicates"]:
        extra.append(f"{r['duplicates']} dup")
    skipped = sum(r["skipped"].values())
    if skipped:
        extra.append(f"{skipped} skipped")
    suffix = f" ({', '.join(extra)})" if extra else ""
    return (f"synced {name} -> '{r['group_id']}' on {r['layer_name']}: "
            f"{r['shapes']} shape(s){suffix}")


def run_watch(args) -> int:
    from kipy.errors import ApiError
    from kipy.errors import ConnectionError as KiCadConnectionError

    watch_dir = args.path or Path(".")
    if not watch_dir.is_dir():
        print(f"error: --watch needs a directory, got {watch_dir}", file=sys.stderr)
        return 2

    print(f"Watching {watch_dir}/ for DXF changes — Ctrl-C to stop.")
    state: dict = {}        # Path -> (mtime_ns, size) of what we last imported
    waiting = False         # whether we've already printed "waiting for KiCad"

    try:
        while True:
            try:
                board = connect_board()
            except (KiCadConnectionError, ApiError) as e:
                if not waiting:
                    print(f"waiting for KiCad... [{e}]\n  ({_CONNECT_HELP})")
                    waiting = True
                time.sleep(args.interval)
                continue
            if waiting:
                print("connected to KiCad board.")
                waiting = False

            seen = set()
            for f in sorted(watch_dir.glob("*.dxf")):
                try:
                    st = f.stat()
                except OSError:
                    continue
                sig = (st.st_mtime_ns, st.st_size)
                seen.add(f)
                if state.get(f) == sig:
                    continue  # unchanged since last import

                try:
                    r = sync_one(f, args=args, board=board)
                except (KiCadConnectionError, ApiError) as e:
                    print(f"  lost KiCad connection ({e}); will retry")
                    break  # reconnect on the next cycle
                except ValueError as e:
                    print(f"  skip {f.name}: {e}")
                    state[f] = sig  # record so we don't repeat until it changes
                except Exception as e:  # noqa: BLE001 - likely a partial write
                    print(f"  {f.name}: not readable yet, will retry [{e}]")
                    # leave state unset so the next poll retries
                else:
                    print(f"  {_sync_summary(f.name, r)}")
                    state[f] = sig

            for gone in [p for p in state if p not in seen]:
                del state[gone]
                print(f"  {gone.name} removed (its group is left in the board)")

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped watching.")
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Line-buffer stdout so --watch progress shows promptly even when piped/logged.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    return run_watch(args) if args.watch else run_once(args)


if __name__ == "__main__":
    raise SystemExit(main())
