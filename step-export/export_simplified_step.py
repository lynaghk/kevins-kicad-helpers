#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
export_simplified_step.py
=========================

Export a *simplified* STEP file from a KiCad PCB for rendering / interaction
performance.

Strategy (robust, no .kicad_pcb parsing):

  1. Let KiCad (``kicad-cli``) do a normal, full STEP export -- board body
     (plus silkscreen / copper / soldermask, your choice) AND the real 3D
     component models, already placed and *already instanced* (KiCad defines
     each unique model's geometry once and references it for every placement).

  2. Post-process that STEP file: for every *component* solid, replace its
     detailed B-rep with a single rectangular box equal to its 3D bounding box.
     Because the geometry is shared, we only compute/replace it **once per
     unique component**; all instances inherit the simplified box automatically.
     The board is left untouched and fully detailed.

  3. Garbage-collect the now-unreferenced detailed geometry so the file shrinks.

We never parse the board's native format, never resolve 3D-model paths, and
never reimplement KiCad's coordinate / rotation math -- placements, transforms
and instancing all come straight from KiCad and are preserved verbatim.

Usage:
    uv run export_simplified_step.py board.kicad_pcb -o out.step
    uv run export_simplified_step.py already_exported.step -o out.step   # skip export

Run ``--help`` for all options.
"""

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time


# ===========================================================================
#  Progress reporter (in-place on a TTY, periodic lines when piped)
# ===========================================================================
class Progress:
    def __init__(self, quiet=False):
        self.quiet = quiet
        self.tty = sys.stderr.isatty()
        self._last = 0.0
        self._width = 0

    def phase(self, msg):
        if self.quiet:
            return
        self._clear()
        print(f"\n\033[1m==>\033[0m {msg}" if self.tty else f"\n==> {msg}", file=sys.stderr, flush=True)

    def step(self, i, total, label=""):
        if self.quiet:
            return
        now = time.time()
        last = i >= total
        if not last and (now - self._last) < 0.05:
            return
        self._last = now
        msg = f"  [{i}/{total}] {label}"
        if self.tty:
            pad = " " * max(0, self._width - len(msg))
            self._width = len(msg)
            print("\r" + msg + pad, file=sys.stderr, end=("\n" if last else ""), flush=True)
        else:
            if last or total <= 20 or i % max(1, total // 20) == 0:
                print(msg, file=sys.stderr, flush=True)

    def info(self, msg):
        if self.quiet:
            return
        self._clear()
        print(f"    {msg}", file=sys.stderr, flush=True)

    def _clear(self):
        if self.tty and self._width:
            print("\r" + " " * self._width + "\r", file=sys.stderr, end="", flush=True)
            self._width = 0


# ===========================================================================
#  Board export via kicad-cli
# ===========================================================================
def export_full_step(board_path, out_path, opts, prog):
    cmd = ["kicad-cli", "pcb", "export", "step", "--subst-models", "-f", "-o", out_path]
    if opts.silkscreen:
        cmd.append("--include-silkscreen")
    if opts.soldermask:
        cmd.append("--include-soldermask")
    if opts.copper:
        cmd += ["--include-tracks", "--include-pads", "--include-zones"]
    if not opts.include_dnp:
        cmd.append("--no-dnp")
    cmd.append(board_path)
    prog.info("running: " + " ".join(cmd))
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write((proc.stdout or "") + "\n" + (proc.stderr or "") + "\n")
        raise SystemExit(f"kicad-cli failed (exit {proc.returncode})")
    prog.info(f"board+components exported in {time.time() - t0:.1f}s")


# ===========================================================================
#  STEP physical-file model
# ===========================================================================
_ID_RE = re.compile(r"#(\d+)")
_TYPE_RE = re.compile(r"^\s*\(?\s*([A-Z_0-9]+)")


def split_entities(data_section):
    """Split a DATA section into raw `#id = ...` strings, honouring quoted
    strings (which may contain ';' and escaped '')."""
    out = []
    buf = []
    in_str = False
    i, n = 0, len(data_section)
    while i < n:
        c = data_section[i]
        buf.append(c)
        if c == "'":
            if in_str and i + 1 < n and data_section[i + 1] == "'":
                buf.append("'")
                i += 2
                continue
            in_str = not in_str
        elif c == ";" and not in_str:
            out.append("".join(buf))
            buf = []
        i += 1
    return out


class Step:
    """Parsed STEP file: ordered entities keyed by id, plus the verbatim
    header so we can round-trip it."""

    def __init__(self, text):
        m = re.search(r"(.*?\bDATA;)(.*?)(\bENDSEC;.*?END-ISO-10303-21;)", text, re.S)
        if not m:
            raise SystemExit("input does not look like a STEP file")
        self.header = m.group(1)
        self.bodies = {}  # id -> body text (everything after '#id =', no ';')
        self.order = []  # ids in original order
        for raw in split_entities(m.group(2)):
            s = raw.strip()
            if not s:
                continue
            em = re.match(r"#(\d+)\s*=\s*(.*)", s, re.S)
            if not em:
                continue
            i = int(em.group(1))
            body = em.group(2).rstrip()
            if body.endswith(";"):
                body = body[:-1].rstrip()
            self.bodies[i] = " ".join(body.split())
            self.order.append(i)
        self.max_id = max(self.order) if self.order else 0

    def etype(self, i):
        mm = _TYPE_RE.match(self.bodies[i])
        return mm.group(1) if mm else ""

    def refs(self, i):
        return [int(x) for x in _ID_RE.findall(self.bodies[i])]

    def add(self, body):
        self.max_id += 1
        self.bodies[self.max_id] = body
        self.order.append(self.max_id)
        return self.max_id

    def serialize(self):
        parts = [self.header, "\n"]
        for i in self.order:
            if i in self.bodies:
                parts.append(f"#{i} = {self.bodies[i]};\n")
        parts.append("ENDSEC;\nEND-ISO-10303-21;\n")
        return "".join(parts)


# ===========================================================================
#  Float formatting + box B-rep generation
# ===========================================================================
def fmt(v):
    if v == 0:
        return "0."
    s = "%.10g" % v
    if "e" in s or "E" in s:
        return s
    if "." not in s:
        s += "."
    return s


# 6 faces of a box, each as a vertex-index loop wound CCW as seen from outside
# (verified by hand so the right-hand-rule normal points outward).
_BOX_FACES = (
    (0, 3, 2, 1),  # bottom (-Z)
    (4, 5, 6, 7),  # top    (+Z)
    (0, 1, 5, 4),  # front  (-Y)
    (3, 7, 6, 2),  # back   (+Y)
    (0, 4, 7, 3),  # left   (-X)
    (1, 2, 6, 5),  # right  (+X)
)


def emit_box_brep(step, lo, hi):
    """Append a MANIFOLD_SOLID_BREP box spanning lo..hi; return its id."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    V = [
        (x0, y0, z0),
        (x1, y0, z0),
        (x1, y1, z0),
        (x0, y1, z0),
        (x0, y0, z1),
        (x1, y0, z1),
        (x1, y1, z1),
        (x0, y1, z1),
    ]

    vpoint = []
    for v in V:
        cp = step.add(f"CARTESIAN_POINT('',({fmt(v[0])},{fmt(v[1])},{fmt(v[2])}))")
        vpoint.append(step.add(f"VERTEX_POINT('',#{cp})"))

    edge_cache = {}

    def get_edge(a, b):
        key = frozenset((a, b))
        if key in edge_cache:
            return edge_cache[key]
        va, vb = V[a], V[b]
        d = (vb[0] - va[0], vb[1] - va[1], vb[2] - va[2])
        length = math.sqrt(d[0] ** 2 + d[1] ** 2 + d[2] ** 2) or 1.0
        u = (d[0] / length, d[1] / length, d[2] / length)
        dr = step.add(f"DIRECTION('',({fmt(u[0])},{fmt(u[1])},{fmt(u[2])}))")
        vec = step.add(f"VECTOR('',#{dr},{fmt(length)})")
        sp = step.add(f"CARTESIAN_POINT('',({fmt(va[0])},{fmt(va[1])},{fmt(va[2])}))")
        line = step.add(f"LINE('',#{sp},#{vec})")
        ec = step.add(f"EDGE_CURVE('',#{vpoint[a]},#{vpoint[b]},#{line},.T.)")
        edge_cache[key] = (ec, a, b)
        return edge_cache[key]

    face_ids = []
    for loop in _BOX_FACES:
        oriented = []
        for k in range(4):
            a, b = loop[k], loop[(k + 1) % 4]
            ec, ea, eb = get_edge(a, b)
            sense = ".T." if (ea, eb) == (a, b) else ".F."
            oriented.append(step.add(f"ORIENTED_EDGE('',*,*,#{ec},{sense})"))
        loop_id = step.add("EDGE_LOOP('',(" + ",".join(f"#{o}" for o in oriented) + "))")
        bound = step.add(f"FACE_OUTER_BOUND('',#{loop_id},.T.)")
        # outward normal from the loop
        a, b, c = V[loop[0]], V[loop[1]], V[loop[2]]
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        w = (c[0] - b[0], c[1] - b[1], c[2] - b[2])
        nx, ny, nz = (u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0])
        nl = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        ul = math.sqrt(u[0] ** 2 + u[1] ** 2 + u[2] ** 2) or 1.0
        axn = step.add(f"DIRECTION('',({fmt(nx / nl)},{fmt(ny / nl)},{fmt(nz / nl)}))")
        axr = step.add(f"DIRECTION('',({fmt(u[0] / ul)},{fmt(u[1] / ul)},{fmt(u[2] / ul)}))")
        loc = step.add(f"CARTESIAN_POINT('',({fmt(a[0])},{fmt(a[1])},{fmt(a[2])}))")
        plane_axis = step.add(f"AXIS2_PLACEMENT_3D('',#{loc},#{axn},#{axr})")
        plane = step.add(f"PLANE('',#{plane_axis})")
        face_ids.append(step.add(f"ADVANCED_FACE('',(#{bound}),#{plane},.T.)"))

    shell = step.add("CLOSED_SHELL('',(" + ",".join(f"#{f}" for f in face_ids) + "))")
    return step.add(f"MANIFOLD_SOLID_BREP('',#{shell})")


# ===========================================================================
#  Assembly analysis
# ===========================================================================
GEOM_LEAF_TYPES = (
    "MANIFOLD_SOLID_BREP",
    "BREP_WITH_VOIDS",
    "FACETED_BREP",
    "SHELL_BASED_SURFACE_MODEL",
    "GEOMETRIC_SET",
)


def follow(step, start, want_types, max_nodes=10_000_000):
    """BFS over references from `start`; return ids whose type is in want_types."""
    seen = set()
    stack = [start]
    found = []
    while stack:
        i = stack.pop()
        if i in seen or i not in step.bodies:
            continue
        seen.add(i)
        if step.etype(i) in want_types:
            found.append(i)
        if len(seen) > max_nodes:
            break
        stack.extend(step.refs(i))
    return found


def collect_points(step, start):
    """Min/max of every CARTESIAN_POINT reachable from `start`."""
    seen = set()
    stack = [start]
    lo = [math.inf, math.inf, math.inf]
    hi = [-math.inf, -math.inf, -math.inf]
    found = False
    while stack:
        i = stack.pop()
        if i in seen or i not in step.bodies:
            continue
        seen.add(i)
        if step.etype(i) == "CARTESIAN_POINT":
            nums = re.findall(r"-?[0-9.]+(?:[eE][-+]?[0-9]+)?", step.bodies[i].split("(", 1)[1])
            if len(nums) >= 3:
                x, y, z = float(nums[0]), float(nums[1]), float(nums[2])
                lo[0], lo[1], lo[2] = min(lo[0], x), min(lo[1], y), min(lo[2], z)
                hi[0], hi[1], hi[2] = max(hi[0], x), max(hi[1], y), max(hi[2], z)
                found = True
        stack.extend(step.refs(i))
    if not found:
        return None
    return (tuple(lo), tuple(hi))


def map_products(step):
    """Return (rep_to_product, product_name, nauo_children).

    rep_to_product:  shape-representation id -> product id
    product_name:    product id -> name string
    nauo_children:   set of product ids that appear as a NAUO child
    """
    pds_to_pd = {}  # product_definition_shape -> product_definition
    pd_to_pdf = {}
    pdf_to_prod = {}
    prod_name = {}
    sdr = []  # (pds, rep)
    nauo_children = set()
    nauo_parent_of = {}  # child_prod -> parent_prod
    pd_to_prod = {}

    for i in step.order:
        if i not in step.bodies:
            continue
        t = step.etype(i)
        r = step.refs(i)
        if t == "PRODUCT":
            mm = re.match(r"PRODUCT\('((?:[^']|'')*)'", step.bodies[i])
            prod_name[i] = mm.group(1).replace("''", "'") if mm else ""
        elif t == "PRODUCT_DEFINITION_FORMATION":
            if r:
                pdf_to_prod[i] = r[-1]
        elif t == "PRODUCT_DEFINITION":
            if len(r) >= 1:
                pd_to_pdf[i] = r[0]
        elif t == "PRODUCT_DEFINITION_SHAPE":
            if r:
                pds_to_pd[i] = r[-1]
        elif t == "SHAPE_DEFINITION_REPRESENTATION":
            if len(r) >= 2:
                sdr.append((r[0], r[1]))
        elif t == "NEXT_ASSEMBLY_USAGE_OCCURRENCE":
            if len(r) >= 2:
                nauo_children.add(r[-1])  # child product_definition
                nauo_parent_of[r[-1]] = r[-2]

    def pd_product(pd):
        pdf = pd_to_pdf.get(pd)
        return pdf_to_prod.get(pdf) if pdf is not None else None

    rep_to_product = {}
    for pds, rep in sdr:
        pd = pds_to_pd.get(pds)
        prod = pd_product(pd) if pd is not None else None
        if prod is not None:
            rep_to_product[rep] = prod

    # child product_definitions -> product ids
    child_products = {pd_product(pd) for pd in nauo_children}
    child_products.discard(None)

    return rep_to_product, prod_name, child_products


# ===========================================================================
#  Garbage collection
# ===========================================================================
ROOT_TYPES = {
    "SHAPE_DEFINITION_REPRESENTATION",
    "PRODUCT_RELATED_PRODUCT_CATEGORY",
    "CONTEXT_DEPENDENT_SHAPE_REPRESENTATION",
    "MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION",
    "PRESENTATION_LAYER_ASSIGNMENT",
    "APPLICATION_PROTOCOL_DEFINITION",
}


def garbage_collect(step, prog):
    seeds = [i for i in step.order if i in step.bodies and step.etype(i) in ROOT_TYPES]
    seen = set()
    stack = list(seeds)
    while stack:
        i = stack.pop()
        if i in seen or i not in step.bodies:
            continue
        seen.add(i)
        stack.extend(step.refs(i))
    before = len(step.bodies)
    step.order = [i for i in step.order if i in seen]
    step.bodies = {i: step.bodies[i] for i in seen}
    prog.info(f"garbage collected {before - len(step.bodies)} entities ({len(step.bodies)} kept)")


# ===========================================================================
#  Core: replace component solids with bounding boxes
# ===========================================================================
def rewrite_rep_items(step, rep_id, drop_ids, new_brep_id):
    """In a shape-representation, drop item refs in `drop_ids` and add new_brep."""
    body = step.bodies[rep_id]
    # TYPE('label',(items...),#ctx)
    m = re.match(r"([A-Z_0-9]+)\('((?:[^']|'')*)'\s*,\s*\((.*)\)\s*,\s*(#\d+)\s*\)$", body)
    if not m:
        return False
    typ, label, items, ctx = m.groups()
    item_ids = _ID_RE.findall(items)
    kept = [f"#{x}" for x in item_ids if int(x) not in drop_ids]
    kept.append(f"#{new_brep_id}")
    step.bodies[rep_id] = f"{typ}('{label}',({','.join(kept)}),{ctx})"
    return True


# KiCad names board geometry sub-parts with these fixed suffixes (the source
# board name is the prefix), independent of the export filename.  See
# step_pcb_model.cpp pushToAssembly(... "PCB"/"copper"/"pad"/"via"/
# "silkscreen"/"soldermask" ...).
BOARD_SUFFIX_RE = re.compile(
    r"_(PCB|copper|pad|pads|via|vias|silkscreen|soldermask|paste|courtyard)$", re.IGNORECASE
)


def simplify(step, board_prefix, root_name, prog):
    rep_to_product, prod_name, child_products = map_products(step)

    def is_board(prod):
        name = prod_name.get(prod, "")
        if name == root_name or BOARD_SUFFIX_RE.search(name):
            return True
        return bool(board_prefix) and name.startswith(board_prefix + "_")

    # component reps that directly contain solid geometry
    targets = []  # (rep_id, product, brep_ids)
    for rep, prod in rep_to_product.items():
        if is_board(prod):
            continue
        breps = follow(step, rep, GEOM_LEAF_TYPES)
        if breps:
            targets.append((rep, prod, breps))

    prog.phase(f"Simplifying {len(targets)} unique component geometries")
    comp_contexts = set()
    n_solids = 0
    for k, (rep, prod, breps) in enumerate(targets):
        prog.step(k + 1, len(targets), prod_name.get(prod, f"#{prod}"))
        # bbox over all solids in this rep
        lo = [math.inf] * 3
        hi = [-math.inf] * 3
        drop = set()
        for b in breps:
            bb = collect_points(step, b)
            if bb is None:
                continue
            (blo, bhi) = bb
            for a in range(3):
                lo[a] = min(lo[a], blo[a])
                hi[a] = max(hi[a], bhi[a])
            drop.add(b)
            n_solids += 1
        if not drop or math.isinf(lo[0]):
            continue
        # remember this rep's presentation context so we can drop its styles
        ctx_ids = [
            r
            for r in step.refs(rep)
            if step.etype(r).startswith("GEOMETRIC_REPRESENTATION_CONTEXT")
            or "REPRESENTATION_CONTEXT" in step.bodies.get(r, "")
        ]
        comp_contexts.update(ctx_ids)
        box = emit_box_brep(step, tuple(lo), tuple(hi))
        rewrite_rep_items(step, rep, drop, box)

    # drop the per-component style containers (MECHANICAL_DESIGN_...),
    # identified by sharing a representation context with a simplified rep.
    dropped_mdgpr = 0
    if comp_contexts:
        for i in list(step.order):
            if i in step.bodies and step.etype(i) == "MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION":
                ctx = step.refs(i)[-1] if step.refs(i) else None
                if ctx in comp_contexts:
                    del step.bodies[i]
                    dropped_mdgpr += 1
    prog.info(
        f"replaced {n_solids} solids with {len(targets)} boxes; dropped {dropped_mdgpr} component style sets"
    )
    return len(targets), n_solids


def find_root_name(step):
    """Root product = a product whose definition never appears as a NAUO child."""
    _, prod_name, child_products = map_products(step)
    roots = [p for p in prod_name if p not in child_products]
    # prefer a root that looks like '<stem> <n>'
    for p in roots:
        if re.search(r"\s\d+$", prod_name[p]):
            return prod_name[p]
    return prod_name[roots[0]] if roots else ""


# ===========================================================================
#  Driver
# ===========================================================================
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="input .kicad_pcb (exported via kicad-cli) or an already-exported .step file")
    ap.add_argument("-o", "--output", help="output .step (default: <input>.simplified.step)")
    ap.add_argument(
        "--copper", dest="copper", action="store_true", help="include copper (tracks/pads/zones) from the board"
    )
    ap.add_argument("--no-soldermask", dest="soldermask", action="store_false")
    ap.add_argument("--no-silkscreen", dest="silkscreen", action="store_false")
    ap.add_argument("--include-dnp", action="store_true", help="also include Do-Not-Populate components")
    ap.add_argument(
        "--board-name",
        help="override the board product-name prefix used to tell "
        "board geometry from components (default: auto-detected)",
    )
    ap.add_argument("--keep-full", action="store_true", help="keep the intermediate full STEP export")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    prog = Progress(quiet=args.quiet)
    t0 = time.time()

    inp = os.path.abspath(args.input)
    if not os.path.isfile(inp):
        raise SystemExit(f"no such file: {inp}")
    stem = re.sub(r"\.(kicad_pcb|step|stp)$", "", os.path.basename(inp))
    out_path = args.output or os.path.join(os.path.dirname(inp), stem + ".simplified.step")

    is_step = inp.lower().endswith((".step", ".stp"))
    full_step = inp
    tmp_dir = None
    if not is_step:
        # Export into a temp dir under the *input* stem so KiCad's root product
        # name is clean (e.g. 'receiver 1') and matches the board sub-part
        # prefix ('receiver_PCB', ...).
        tmp_dir = tempfile.mkdtemp(prefix="ksimplify_")
        full_step = os.path.join(tmp_dir, stem + ".step")
        prog.phase("Exporting full STEP with kicad-cli (board + real models)")
        export_full_step(inp, full_step, args, prog)
    else:
        prog.phase("Using existing STEP file as input")

    prog.phase("Parsing STEP")
    with open(full_step, "r", errors="replace") as f:
        step = Step(f.read())
    prog.info(f"{len(step.bodies)} entities")

    root_name = args.board_name + " 1" if args.board_name else find_root_name(step)
    board_prefix = args.board_name if args.board_name else re.sub(r"\s+\d+$", "", root_name)
    prog.info(f"root product: '{root_name}'  -> board prefix: '{board_prefix}'")

    n_defs, n_solids = simplify(step, board_prefix, root_name, prog)

    prog.phase("Garbage-collecting detailed geometry")
    garbage_collect(step, prog)

    prog.phase("Writing STEP file")
    with open(out_path, "w") as f:
        f.write(step.serialize())

    size = os.path.getsize(out_path)
    in_size = os.path.getsize(full_step) if os.path.isfile(full_step) else None

    if tmp_dir is not None:
        if args.keep_full:
            kept = os.path.join(os.path.dirname(out_path), stem + ".full.step")
            shutil.move(full_step, kept)
            prog.info(f"kept full export: {kept}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
    prog.phase("Done")
    if not args.quiet:
        print(
            f"""
  output:              {out_path}  ({size / 1e6:.1f} MB)
  unique components:   {n_defs}   (boxes emitted)
  solids replaced:     {n_solids}
  {"full export size:    %.1f MB" % (in_size / 1e6) if in_size else ""}
  elapsed:             {time.time() - t0:.1f}s
""",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
