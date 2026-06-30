#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["ezdxf>=1.1"]
# ///
"""Generate the DXF sample fixtures used to exercise kicad_dxf_import.py.

Each file is named ``<id>_<Layer>.dxf`` so it round-trips through the importer's
filename convention. Run with::

    uv run samples/make_samples.py

The expected primitive counts are documented next to each builder and asserted
by the offline tests in ../test_kicad_dxf_import.py.
"""
from __future__ import annotations

from pathlib import Path

import ezdxf

OUT_DIR = Path(__file__).resolve().parent


def _new():
    doc = ezdxf.new("R2010")
    return doc, doc.modelspace()


def board_edge_cuts() -> "ezdxf.document.Drawing":
    """board_Edge.Cuts.dxf -> 4 LINEs + 2 ARCs (= 4 segments + 2 arcs)."""
    doc, msp = _new()
    # 60 x 40 rectangle outline
    corners = [(0, 0), (60, 0), (60, 40), (0, 40)]
    for a, b in zip(corners, corners[1:] + corners[:1]):
        msp.add_line(a, b)
    # two semicircular bumps to exercise ARC handling
    msp.add_arc(center=(30, 40), radius=5, start_angle=0, end_angle=180)
    msp.add_arc(center=(30, 0), radius=5, start_angle=180, end_angle=360)
    return doc


def holes_user1() -> "ezdxf.document.Drawing":
    """holes_User.1.dxf -> 5 CIRCLEs."""
    doc, msp = _new()
    for (cx, cy, r) in [(10, 10, 1.5), (25, 10, 2.0), (40, 10, 2.5),
                        (17, 25, 3.0), (33, 25, 1.0)]:
        msp.add_circle(center=(cx, cy), radius=r)
    return doc


def logo_silks() -> "ezdxf.document.Drawing":
    """logo_F.SilkS.dxf -> closed LWPOLYLINE (1 arc + 3 segments) + SPLINE (flattened)."""
    doc, msp = _new()
    # closed polyline: first span has a bulge (-> arc), the rest are straight
    msp.add_lwpolyline(
        [(10, 10, 0.5), (30, 10, 0.0), (30, 30, 0.0), (10, 30, 0.0)],
        format="xyb",
        close=True,
    )
    # a smooth spline (flattened to segments on import)
    msp.add_spline([(12, 32), (18, 38), (24, 34), (30, 38)])
    return doc


def mixed_dwgs() -> "ezdxf.document.Drawing":
    """mixed_Dwgs.User.dxf -> 1 LINE + 1 ARC + 1 CIRCLE + open LWPOLYLINE (2 segments).

    Coordinates are kept simple so the offset/scale transform is easy to assert.
    """
    doc, msp = _new()
    msp.add_line((0, 0), (10, 0))                                  # segment
    msp.add_arc(center=(20, 0), radius=5, start_angle=0, end_angle=90)
    msp.add_circle(center=(0, 20), radius=4)
    msp.add_lwpolyline([(0, 0), (5, 5), (10, 0)], format="xy", close=False)  # 2 segments
    return doc


BUILDERS = {
    "board_Edge.Cuts.dxf": board_edge_cuts,
    "holes_User.1.dxf": holes_user1,
    "logo_F.SilkS.dxf": logo_silks,
    "mixed_Dwgs.User.dxf": mixed_dwgs,
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, builder in BUILDERS.items():
        path = OUT_DIR / name
        builder().saveas(str(path))
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
