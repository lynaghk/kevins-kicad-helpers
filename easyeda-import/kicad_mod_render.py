#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "Pillow>=10",
# ]
# ///
"""Render a KiCad footprint (.kicad_mod) to a raster image and to terminal
half-blocks.

Only the two layers a person needs to eyeball a footprint are drawn: exposed
copper (front pads) and front silkscreen. Everything is drawn in true footprint
coordinates with the footprint origin (0,0) at the centre of the canvas, at a
caller-supplied mm->pixel scale. Keeping origin and scale fixed across a set of
candidates is what lets the chooser flip between them and compare pad geometry in
the same physical place.

Handles both the old easyeda2kicad/kicad-5 s-expression dialect
(`(fp_line (start ..) (end ..) (width ..) (layer ..))`,
`(pad 1 smd rect (at ..) (size ..) (layers ..))`) and the modern KiCad dialect
(`(footprint ..)`, `(stroke (width ..)) (layer "F.SilkS")`), because we compare
generated footprints (old dialect) against KiCad's standard library (modern).
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

# KiCad-ish preview palette.
BG = (24, 24, 27)
COPPER = (200, 52, 52)
SILK = (233, 233, 233)
ORIGIN = (90, 90, 100)
PAD_TEXT = (250, 250, 250)  # pad-number labels

SUPERSAMPLE = 3  # draw big, downscale for cheap anti-aliasing

_FONT_CACHE: dict[int, ImageFont.ImageFont] = {}


def _font(size: int) -> ImageFont.ImageFont:
    size = max(6, int(size))
    if size not in _FONT_CACHE:
        try:
            _FONT_CACHE[size] = ImageFont.load_default(size=size)  # Pillow >= 10: scalable
        except TypeError:  # pragma: no cover - very old Pillow
            _FONT_CACHE[size] = ImageFont.load_default()
    return _FONT_CACHE[size]


# --------------------------------------------------------------------------- #
# s-expression parsing
# --------------------------------------------------------------------------- #
def parse_sexpr(text: str) -> list:
    """Parse s-expression text into nested lists of tokens (str/float)."""
    tokens = _tokenize(text)
    pos = 0

    def parse_list() -> list:
        nonlocal pos
        node: list = []
        while pos < len(tokens):
            tok = tokens[pos]
            if tok == "(":
                pos += 1
                node.append(parse_list())
            elif tok == ")":
                pos += 1
                return node
            else:
                pos += 1
                node.append(tok)
        return node

    # skip to first '('
    while pos < len(tokens) and tokens[pos] != "(":
        pos += 1
    if pos >= len(tokens):
        return []
    pos += 1
    return parse_list()


def _tokenize(text: str) -> list[str]:
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in "()":
            out.append(c)
            i += 1
        elif c.isspace():
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            i += 1  # closing quote
            out.append("".join(buf))
        else:
            buf = []
            while i < n and not text[i].isspace() and text[i] not in "()":
                buf.append(text[i])
                i += 1
            out.append("".join(buf))
    return out


def _num(tok: str) -> float:
    try:
        return float(tok)
    except (TypeError, ValueError):
        return 0.0


def _find(node: list, key: str) -> list | None:
    """First direct child list starting with `key`."""
    for child in node:
        if isinstance(child, list) and child and child[0] == key:
            return child
    return None


def _find_all(node: list, key: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == key]


def _layers_of(node: list) -> list[str]:
    """Layer tokens from either `(layers ..)` (pads) or `(layer "..")`."""
    result: list[str] = []
    for key in ("layers", "layer"):
        found = _find(node, key)
        if found:
            result.extend(str(t) for t in found[1:])
    return result


def _stroke_width(node: list, default: float = 0.12) -> float:
    """Width from modern `(stroke (width w) ..)` or old `(width w)`."""
    stroke = _find(node, "stroke")
    if stroke:
        w = _find(stroke, "width")
        if w:
            return _num(w[1])
    w = _find(node, "width")
    if w:
        return _num(w[1])
    return default


# --------------------------------------------------------------------------- #
# geometry model
# --------------------------------------------------------------------------- #
@dataclass
class Pad:
    x: float
    y: float
    rot: float
    w: float
    h: float
    shape: str
    side: str  # 'F', 'B', or '*' (both, e.g. through-hole)
    number: str = ""  # pad number/name, e.g. "1" (blank for mechanical pads)


@dataclass
class Seg:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    side: str  # 'F' or 'B'


@dataclass
class Circle:
    cx: float
    cy: float
    r: float
    width: float
    side: str  # 'F' or 'B'


@dataclass
class Footprint:
    name: str
    pads: list[Pad]
    silk_segs: list[Seg]
    silk_circles: list[Circle]

    def has_back(self) -> bool:
        """Whether anything is drawn on the back (enables the flip view)."""
        return any(p.side in ("B", "*") for p in self.pads) or any(
            e.side == "B" for e in (*self.silk_segs, *self.silk_circles)
        )


def _copper_side(layers: list[str]) -> str | None:
    front = any(l == "F.Cu" or l.endswith("F.Cu") for l in layers)
    back = any(l == "B.Cu" or l.endswith("B.Cu") for l in layers)
    if any(l == "*.Cu" or l.endswith("*.Cu") for l in layers) or (front and back):
        return "*"
    if front:
        return "F"
    if back:
        return "B"
    return None


def _silk_side(layers: list[str]) -> str | None:
    if any(l == "F.SilkS" or l.endswith("F.SilkS") for l in layers):
        return "F"
    if any(l == "B.SilkS" or l.endswith("B.SilkS") for l in layers):
        return "B"
    return None


def _arc_points(node: list, segments: int = 24) -> list[tuple[float, float]]:
    """Sample an fp_arc into a polyline. Handles the modern 3-point form
    (start/mid/end, all on the arc) and the old form (start = centre, end = a
    point on the arc, angle = sweep in degrees). Falls back to the chord."""
    start, end = _find(node, "start"), _find(node, "end")
    mid, angle = _find(node, "mid"), _find(node, "angle")

    if start and mid and end:  # modern: three points on the arc
        p1 = (_num(start[1]), _num(start[2]))
        pm = (_num(mid[1]), _num(mid[2]))
        p3 = (_num(end[1]), _num(end[2]))
        c = _circumcentre(p1, pm, p3)
        if c is None:
            return [p1, p3]
        cx, cy = c
        r = math.hypot(p1[0] - cx, p1[1] - cy)
        a1 = math.atan2(p1[1] - cy, p1[0] - cx)
        am = math.atan2(pm[1] - cy, pm[0] - cx)
        a3 = math.atan2(p3[1] - cy, p3[0] - cx)
        ccw = (a3 - a1) % (2 * math.pi)
        sweep = ccw if (am - a1) % (2 * math.pi) <= ccw else ccw - 2 * math.pi
        return [
            (cx + r * math.cos(a1 + sweep * i / segments), cy + r * math.sin(a1 + sweep * i / segments))
            for i in range(segments + 1)
        ]

    if start and end and angle:  # old: centre + start point + swept angle
        cx, cy = _num(start[1]), _num(start[2])
        ex, ey = _num(end[1]), _num(end[2])
        r = math.hypot(ex - cx, ey - cy)
        a0 = math.atan2(ey - cy, ex - cx)
        sweep = math.radians(_num(angle[1]))
        return [
            (cx + r * math.cos(a0 + sweep * i / segments), cy + r * math.sin(a0 + sweep * i / segments))
            for i in range(segments + 1)
        ]

    if start and end:  # degenerate: chord
        return [(_num(start[1]), _num(start[2])), (_num(end[1]), _num(end[2]))]
    return []


def _circumcentre(p1, p2, p3) -> tuple[float, float] | None:
    (x1, y1), (x2, y2), (x3, y3) = p1, p2, p3
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:  # collinear
        return None
    s1, s2, s3 = x1 * x1 + y1 * y1, x2 * x2 + y2 * y2, x3 * x3 + y3 * y3
    ux = (s1 * (y2 - y3) + s2 * (y3 - y1) + s3 * (y1 - y2)) / d
    uy = (s1 * (x3 - x2) + s2 * (x1 - x3) + s3 * (x2 - x1)) / d
    return (ux, uy)


def load_footprint(path: Path) -> Footprint:
    root = parse_sexpr(path.read_text())
    name = path.stem
    if len(root) >= 2 and isinstance(root[1], str):
        name = root[1]

    pads: list[Pad] = []
    silk_segs: list[Seg] = []
    silk_circles: list[Circle] = []

    for node in root:
        if not isinstance(node, list) or not node:
            continue
        head = node[0]

        if head == "pad":
            side = _copper_side(_layers_of(node))
            if side is None:
                continue
            shape = node[3] if len(node) > 3 and isinstance(node[3], str) else "rect"
            at = _find(node, "at")
            size = _find(node, "size")
            if not at or not size:
                continue
            x, y = _num(at[1]), _num(at[2])
            rot = _num(at[3]) if len(at) > 3 else 0.0
            w, h = _num(size[1]), _num(size[2])
            number = node[1] if len(node) > 1 and isinstance(node[1], str) else ""
            pads.append(Pad(x, y, rot, w, h, shape, side, number))

        elif head in ("fp_line", "gr_line"):
            side = _silk_side(_layers_of(node))
            if side is None:
                continue
            start, end = _find(node, "start"), _find(node, "end")
            if start and end:
                silk_segs.append(
                    Seg(_num(start[1]), _num(start[2]), _num(end[1]), _num(end[2]), _stroke_width(node), side)
                )

        elif head in ("fp_circle", "gr_circle"):
            side = _silk_side(_layers_of(node))
            if side is None:
                continue
            center, end = _find(node, "center"), _find(node, "end")
            if center and end:
                r = math.hypot(_num(end[1]) - _num(center[1]), _num(end[2]) - _num(center[2]))
                silk_circles.append(Circle(_num(center[1]), _num(center[2]), r, _stroke_width(node), side))

        elif head in ("fp_arc", "gr_arc"):
            side = _silk_side(_layers_of(node))
            if side is None:
                continue
            pts = _arc_points(node)
            w = _stroke_width(node)
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                silk_segs.append(Seg(ax, ay, bx, by, w, side))

    return Footprint(name, pads, silk_segs, silk_circles)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _make_transform(s: float, cx: float, cy: float, mx: int, rotation: int):
    """Return a fn mapping footprint mm coords to canvas pixels: rotate CCW by
    `rotation` degrees, mirror X for the back view, then scale + centre."""
    a = math.radians(rotation)
    ca, sa = math.cos(a), math.sin(a)

    def tf(x: float, y: float) -> tuple[float, float]:
        rx = x * ca - y * sa
        ry = x * sa + y * ca
        return (cx + mx * rx * s, cy + ry * s)

    return tf


def _place(pad: Pad, tf, local_pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Rotate mm points by the pad's own angle, offset to the pad centre, then run
    through `tf` (view rotation/mirror/scale) so everything transforms uniformly."""
    a = math.radians(pad.rot)
    ca, sa = math.cos(a), math.sin(a)
    return [tf(pad.x + x * ca - y * sa, pad.y + x * sa + y * ca) for x, y in local_pts]


def _rect_corners(pad: Pad, tf) -> list[tuple[float, float]]:
    hw, hh = pad.w / 2, pad.h / 2
    return _place(pad, tf, [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)])


def _pill_corners(pad: Pad, tf, cap_segs: int = 10) -> list[tuple[float, float]]:
    """Outline of a KiCad 'oval' pad: a stadium/pill (parallel sides with
    semicircular caps of radius = half the short dimension), NOT an ellipse."""
    w, h = pad.w, pad.h
    r = min(w, h) / 2
    local: list[tuple[float, float]] = []
    if w >= h:  # horizontal pill; caps on the left/right
        cx = w / 2 - r
        for i in range(cap_segs + 1):  # right cap, bottom -> top
            a = math.radians(-90 + 180 * i / cap_segs)
            local.append((cx + r * math.cos(a), r * math.sin(a)))
        for i in range(cap_segs + 1):  # left cap, top -> bottom
            a = math.radians(90 + 180 * i / cap_segs)
            local.append((-cx + r * math.cos(a), r * math.sin(a)))
    else:  # vertical pill; caps on top/bottom
        cy = h / 2 - r
        for i in range(cap_segs + 1):  # bottom cap, right -> left
            a = math.radians(0 + 180 * i / cap_segs)
            local.append((r * math.cos(a), cy + r * math.sin(a)))
        for i in range(cap_segs + 1):  # top cap, left -> right
            a = math.radians(180 + 180 * i / cap_segs)
            local.append((r * math.cos(a), -cy + r * math.sin(a)))
    return _place(pad, tf, local)


def render_footprint(
    fp: Footprint,
    px_per_mm: float,
    width: int,
    height: int,
    side: str = "front",
    rotation: int = 0,
    supersample: int | None = None,
) -> Image.Image:
    """Render one side of `fp` centred on its origin at `px_per_mm`.

    side='front' draws F.Cu + F.SilkS; side='back' draws B.Cu + B.SilkS mirrored
    in X, so it reads as a real bottom-up view. `rotation` (CCW degrees) turns the
    whole footprint, used to preview a standard footprint aligned to the EasyEDA
    one. Through-hole (`*.Cu`) pads show on both sides.

    `supersample` overrides the anti-aliasing factor; a large output canvas (the
    kitty path) needs no supersampling, which avoids building a huge intermediate."""
    ss = SUPERSAMPLE if supersample is None else max(1, supersample)
    W, H = width * ss, height * ss
    s = px_per_mm * ss
    cx, cy = W / 2, H / 2
    want = "B" if side == "back" else "F"
    mx = -1 if side == "back" else 1  # mirror X for the bottom view

    tf = _make_transform(s, cx, cy, mx, rotation)

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # faint origin crosshair
    draw.line([(cx - 6 * ss, cy), (cx + 6 * ss, cy)], fill=ORIGIN, width=max(1, ss))
    draw.line([(cx, cy - 6 * ss), (cx, cy + 6 * ss)], fill=ORIGIN, width=max(1, ss))

    for pad in fp.pads:
        if pad.side not in (want, "*"):
            continue
        c = tf(pad.x, pad.y)
        if pad.shape == "circle":
            rr = pad.w / 2 * s
            draw.ellipse([c[0] - rr, c[1] - rr, c[0] + rr, c[1] + rr], fill=COPPER)
        elif pad.shape == "oval":
            draw.polygon(_pill_corners(pad, tf), fill=COPPER)
        else:
            draw.polygon(_rect_corners(pad, tf), fill=COPPER)
        # pad number, drawn upright and centred (skip when too small to read); a
        # thin dark outline keeps it legible against the copper at small sizes.
        if pad.number:
            fs = int(min(pad.w, pad.h) * s * 0.6)
            if fs >= 7:
                draw.text(
                    c,
                    pad.number,
                    fill=PAD_TEXT,
                    font=_font(fs),
                    anchor="mm",
                    stroke_width=max(1, fs // 10),
                    stroke_fill=(20, 20, 20),
                )

    for seg in fp.silk_segs:
        if seg.side != want:
            continue
        draw.line([tf(seg.x1, seg.y1), tf(seg.x2, seg.y2)], fill=SILK, width=max(1, int(seg.width * s)))

    for circ in fp.silk_circles:
        if circ.side != want:
            continue
        c = tf(circ.cx, circ.cy)
        r = circ.r * s
        draw.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], outline=SILK, width=max(1, int(circ.width * s)))

    return img.resize((width, height), Image.LANCZOS)


def to_half_blocks(img: Image.Image) -> str:
    """Two vertical pixels per character cell via the upper half-block."""
    img = img.convert("RGB")
    w, h = img.size
    if h % 2:
        img = img.crop((0, 0, w, h - 1))
        h -= 1
    px = img.load()
    lines = []
    for y in range(0, h, 2):
        cells = []
        for x in range(w):
            tr, tg, tb = px[x, y]
            br, bg, bb = px[x, y + 1]
            cells.append(f"\x1b[38;2;{tr};{tg};{tb};48;2;{br};{bg};{bb}m▀")
        cells.append("\x1b[0m")
        lines.append("".join(cells))
    return "\n".join(lines)


def _pad_mask(fp: Footprint, rotation: int, s: float, size: int) -> Image.Image:
    """Binary (mode 'L', 0/255) raster of the front-copper pads only, at scale `s`
    (px/mm), origin centred, rotated CCW by `rotation`."""
    img = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(img)
    tf = _make_transform(s, size / 2, size / 2, 1, rotation)
    for pad in fp.pads:
        if pad.side not in ("F", "*"):
            continue
        if pad.shape == "circle":
            c = tf(pad.x, pad.y)
            r = pad.w / 2 * s
            draw.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], fill=255)
        elif pad.shape == "oval":
            draw.polygon(_pill_corners(pad, tf), fill=255)
        else:
            draw.polygon(_rect_corners(pad, tf), fill=255)
    return img


def pad_mismatch(reference: Footprint, candidate: Footprint, rotation: int, size: int = 128) -> int:
    """Non-overlapping pad area (pixel count of the symmetric difference) between
    the reference pads and the candidate's pads rotated by `rotation`. Lower = more
    similar; used to rank standard candidates. Deliberately coarse and cheap."""
    extent = max(bbox_extent(reference), bbox_extent(candidate))
    s = 0.9 * size / (2 * extent)
    a = _pad_mask(reference, 0, s, size)
    b = _pad_mask(candidate, rotation, s, size)
    diff = ImageChops.difference(a, b)  # 255 where exactly one pad covers the pixel
    return sum(diff.histogram()[1:])


def suggest_rotation(reference: Footprint, candidate: Footprint) -> int:
    """Best 90° CCW rotation (0/90/180/270) to align `candidate`'s pad layout to
    `reference`'s. Used to guess the FT Rotation Offset when substituting a
    standard footprint for the EasyEDA one (the EasyEDA one is the JLCPCB-correct
    reference).

    Pads are matched BY PIN NUMBER when both footprints share numbers, so pin 1
    lands on pin 1 — a geometrically symmetric layout (e.g. the two pad rows of a
    SOIC-8) would otherwise tie at 90/270 and could scramble the numbering. Falls
    back to nearest-centre matching when there are no common numbers."""
    ref_by_num: dict[str, list[tuple[float, float]]] = {}
    for p in reference.pads:
        if p.number:
            ref_by_num.setdefault(p.number, []).append((p.x, p.y))
    cand_by_num: dict[str, list[tuple[float, float]]] = {}
    for p in candidate.pads:
        if p.number:
            cand_by_num.setdefault(p.number, []).append((p.x, p.y))
    common = set(ref_by_num) & set(cand_by_num)

    ref_all = [(p.x, p.y) for p in reference.pads]
    cand_all = [(p.x, p.y) for p in candidate.pads]
    if not ref_all or not cand_all:
        return 0

    best_r, best_cost = 0, None
    for r in (0, 90, 180, 270):
        a = math.radians(r)
        ca, sa = math.cos(a), math.sin(a)

        def rot(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
            return [(x * ca - y * sa, x * sa + y * ca) for x, y in pts]

        if common:
            # Same-numbered pads must coincide: cost only pairs pads that share a
            # pin number (min-distance within the group handles duplicate numbers
            # like a split thermal pad).
            cost = 0.0
            for num in common:
                rotated = rot(cand_by_num[num])
                for rx, ry in ref_by_num[num]:
                    cost += min((rx - cx) ** 2 + (ry - cy) ** 2 for cx, cy in rotated)
        else:
            rotated = rot(cand_all)
            cost = sum(min((rx - cx) ** 2 + (ry - cy) ** 2 for cx, cy in rotated) for rx, ry in ref_all)
            cost += sum(min((rx - cx) ** 2 + (ry - cy) ** 2 for rx, ry in ref_all) for cx, cy in rotated)

        if best_cost is None or cost < best_cost:
            best_cost, best_r = cost, r
    return best_r


def bbox_extent(fp: Footprint) -> float:
    """Half-extent (mm) of the footprint from origin, for choosing a fit scale."""
    m = 0.5
    for p in fp.pads:
        m = max(m, abs(p.x) + p.w, abs(p.y) + p.h)
    for sg in fp.silk_segs:
        m = max(m, abs(sg.x1), abs(sg.y1), abs(sg.x2), abs(sg.y2))
    for c in fp.silk_circles:
        m = max(m, abs(c.cx) + c.r, abs(c.cy) + c.r)
    return m


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a .kicad_mod to PNG and/or terminal half-blocks.")
    ap.add_argument("kicad_mod", type=Path)
    ap.add_argument("--png", type=Path, help="also write a PNG here")
    ap.add_argument("--cols", type=int, default=80, help="terminal width in character cells")
    ap.add_argument("--rows", type=int, default=32, help="terminal height in character cells")
    ap.add_argument("--scale", type=float, help="px per mm (default: fit to canvas)")
    ap.add_argument("--side", choices=("front", "back"), default="front", help="which side to draw")
    ap.add_argument("--no-blocks", action="store_true", help="skip half-block terminal output")
    args = ap.parse_args()

    fp = load_footprint(args.kicad_mod)
    width, height = args.cols, args.rows * 2  # 2 px per row
    if args.scale:
        px_per_mm = args.scale
    else:
        px_per_mm = 0.9 * min(width, height) / (2 * bbox_extent(fp))

    img = render_footprint(fp, px_per_mm, width, height, side=args.side)
    if args.png:
        img.save(args.png)
    if not args.no_blocks:
        sys.stdout.write(to_half_blocks(img) + "\n")
    sys.stderr.write(
        f"{fp.name}: {len(fp.pads)} pads, {len(fp.silk_segs)} silk segs, "
        f"{len(fp.silk_circles)} silk circles @ {px_per_mm:.1f}px/mm\n"
    )


if __name__ == "__main__":
    main()
