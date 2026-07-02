#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["kicad-python>=0.7", "ezdxf>=1.1"]
# ///
"""Offline tests for kicad_dxf_import.py (no running KiCad required).

Run with::  uv run test_kicad_dxf_import.py
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

import ezdxf

import kicad_dxf_import as kdi

SAMPLES = Path(__file__).resolve().parent / "samples"


def _kinds(primitives):
    return [p.kind for p in primitives]


def _primitives(name, tf=None, flatten=0.02):
    tf = tf or kdi.Transform()
    doc = ezdxf.readfile(str(SAMPLES / name))
    skipped: Counter = Counter()
    prims = list(kdi.iter_primitives(doc.modelspace(), tf, flatten, skipped))
    return prims, skipped


def test_parse_filename():
    assert kdi.parse_filename(Path("panel_Edge.Cuts.dxf")) == ("panel", "Edge.Cuts")
    assert kdi.parse_filename(Path("frontplate_User.1.dxf")) == ("frontplate", "User.1")
    assert kdi.parse_filename(Path("logo_top_F.SilkS.dxf")) == ("logo_top", "F.SilkS")
    assert kdi.parse_filename(Path("/a/b/holes_Dwgs.User.dxf")) == ("holes", "Dwgs.User")
    for bad in ("noseparator.dxf", "_Edge.Cuts.dxf", "panel_.dxf"):
        try:
            kdi.parse_filename(Path(bad))
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


def test_transform():
    tf = kdi.Transform(scale=2.0, offset_x=50.0, offset_y=25.0)
    # Y is negated (DXF Y-up -> KiCad Y-down); offsets applied in KiCad space.
    assert tf.pt(0, 0) == (50.0, 25.0)
    assert tf.pt(10, 0) == (70.0, 25.0)
    assert tf.pt(0, 10) == (50.0, 5.0)
    assert tf.length(3.0) == 6.0


def test_bulge_arc_points_semicircle():
    # bulge=1, chord (0,0)->(2,0): CCW semicircle, center (1,0), passing through
    # the *lower* point (1,-1). (A left-normal bug would wrongly give (1,1).)
    s, m, e = kdi._bulge_arc_points((0.0, 0.0), (2.0, 0.0), 1.0)
    assert s == (0.0, 0.0) and e == (2.0, 0.0)
    assert math.isclose(m[0], 1.0) and math.isclose(m[1], -1.0)
    # negative bulge mirrors to the other side
    _, m2, _ = kdi._bulge_arc_points((0.0, 0.0), (2.0, 0.0), -1.0)
    assert math.isclose(m2[1], 1.0)


def test_arc_three_points_quarter():
    s, m, e = kdi._arc_three_points(0.0, 0.0, 1.0, 0.0, 90.0)
    assert math.isclose(s[0], 1.0, abs_tol=1e-9) and math.isclose(s[1], 0.0, abs_tol=1e-9)
    assert math.isclose(e[0], 0.0, abs_tol=1e-9) and math.isclose(e[1], 1.0, abs_tol=1e-9)
    # midpoint at 45 degrees
    assert math.isclose(m[0], math.sqrt(0.5)) and math.isclose(m[1], math.sqrt(0.5))


def test_board_edge_cuts():
    prims, skipped = _primitives("board_Edge.Cuts.dxf")
    assert not skipped
    assert _kinds(prims) == ["segment"] * 4 + ["arc"] * 2


def test_holes():
    prims, skipped = _primitives("holes_User.1.dxf")
    assert not skipped
    assert _kinds(prims) == ["circle"] * 5
    radii = sorted(round(p.radius, 3) for p in prims)
    assert radii == [1.0, 1.5, 2.0, 2.5, 3.0]


def test_logo():
    prims, skipped = _primitives("logo_F.SilkS.dxf")
    assert not skipped
    kinds = _kinds(prims)
    # closed polyline: 1 bulge arc + 3 straight segments
    assert kinds[:4] == ["arc", "segment", "segment", "segment"]
    # spline flattened into several segments
    assert kinds[4:].count("segment") >= 3
    assert kinds.count("arc") == 1


def test_mixed_with_offset_scale():
    tf = kdi.Transform(scale=2.0, offset_x=50.0, offset_y=25.0)
    prims, skipped = _primitives("mixed_Dwgs.User.dxf", tf=tf)
    assert not skipped
    # order: LINE, ARC, CIRCLE, then open LWPOLYLINE (2 segments)
    assert _kinds(prims) == ["segment", "arc", "circle", "segment", "segment"]
    line = prims[0]
    assert line.start == (50.0, 25.0)
    assert line.end == (70.0, 25.0)
    circle = prims[2]
    # DXF circle center (0,20) r4 -> center (50, -15), radius scaled to 8
    assert circle.center == (50.0, -15.0)
    assert math.isclose(circle.radius, 8.0)


def test_construction_excluded():
    doc = ezdxf.new("R2010", setup=True)
    if "Defpoints" not in doc.layers:
        doc.layers.add("Defpoints")
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0))  # real
    msp.add_line((0, 0), (10, 10), dxfattribs={"linetype": "DASHED"})  # construction
    msp.add_line((1, 1), (2, 2), dxfattribs={"layer": "Defpoints"})  # construction
    skipped = Counter()
    prims = list(kdi.iter_primitives(msp, kdi.Transform(), 0.02, skipped))
    assert [p.kind for p in prims] == ["segment"]
    assert sum(skipped.values()) == 2
    # opt-in keeps everything
    prims_all = list(kdi.iter_primitives(msp, kdi.Transform(), 0.02, Counter(), include_construction=True))
    assert len(prims_all) == 3


def test_dedupe_primitives():
    prims = [
        kdi.Segment((0, 0), (10, 0)),
        kdi.Segment((10, 0), (0, 0)),  # reversed duplicate
        kdi.Circle((1, 1), 2.0),
        kdi.Arc((0, 0), (1, 1), (2, 0)),
        kdi.Arc((2, 0), (1, 1), (0, 0)),  # reversed duplicate
    ]
    out = kdi.dedupe_primitives(prims)
    assert [p.kind for p in out] == ["segment", "circle", "arc"]


def test_real_plate_reader_dxf():
    """Regression for the user's real board: dashed construction lines excluded,
    overlapping polyline deduped -> clean outline (4 seg + 1 arc) + 1 hole."""
    f = Path.home() / "incubator/plate-reader/enclosure/board_Edge.Cuts.dxf"
    if not f.exists():
        print("  (skipped: sample not present)")
        return
    doc = ezdxf.readfile(str(f))
    skipped = Counter()
    prims = kdi.dedupe_primitives(list(kdi.iter_primitives(doc.modelspace(), kdi.Transform(), 0.02, skipped)))
    kinds = Counter(p.kind for p in prims)
    assert kinds == Counter({"segment": 4, "arc": 1, "circle": 1}), kinds
    assert sum(skipped.values()) == 2, skipped  # two dashed construction lines


def test_sync_one_rejects_bad_filename():
    # sync_one validates the filename before touching the board, so watch mode can
    # skip non-conforming files without a connection.
    import argparse

    try:
        kdi.sync_one(Path("badname.dxf"), board=None, args=argparse.Namespace())
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a non-conforming filename")


def test_primitives_to_shapes():
    from kipy.board_types import BoardArc, BoardCircle, BoardSegment
    from kipy.proto.board.board_types_pb2 import BoardLayer

    prims, _ = _primitives("mixed_Dwgs.User.dxf")
    shapes = kdi.primitives_to_shapes(prims, BoardLayer.BL_Dwgs_User, 100_000)
    assert isinstance(shapes[0], BoardSegment)
    assert isinstance(shapes[1], BoardArc)
    assert isinstance(shapes[2], BoardCircle)
    for s in shapes:
        assert s.layer == BoardLayer.BL_Dwgs_User
        assert s.attributes.stroke.width == 100_000
    # first DXF entity is LINE (0,0)->(10,0); identity transform, mm->nm
    seg = shapes[0]
    assert seg.start.x == 0 and seg.start.y == 0
    assert seg.end.x == 10_000_000 and seg.end.y == 0


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
