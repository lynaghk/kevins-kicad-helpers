#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["Pillow>=10"]
# ///
"""Offline tests for the EasyEDA importer + footprint chooser (no network, no
easyeda2kicad, no KiCad libraries required — fixtures are inlined).

Run with::  uv run test_easyeda_import.py

Each test pins down behaviour we've already gotten wrong once (arc chords,
ellipse ovals, pin-scrambling rotation, Ctrl-C handling, redraw leftovers, ...)
so regressions show up here instead of in the chooser.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import footprint_chooser_tui as tui
import footprint_matcher as matcher
import import_easyeda_parts as imp
import kicad_mod_render as R
import symbol_matcher as sym

# --------------------------------------------------------------------------- #
# fixtures: one footprint per dialect (trimmed from real easyeda2kicad output
# and KiCad's Package_TO_SOT_SMD; 3D model lines dropped, geometry verbatim)
# --------------------------------------------------------------------------- #

# easyeda2kicad's old/kicad-5 dialect: unquoted tokens, (width ..), (layer F.SilkS).
# EasyEDA/JLCPCB reel orientation: pins 1-3 along the BOTTOM (y=+1.30).
EASYEDA_SOT23_5 = """\
(module easyeda2kicad:SOT-23-5_L3.0-W1.6-P0.95-LS2.8-BL (layer F.Cu) (tedit 5DC5F6A4)
\t(attr smd)
\t(property "LCSC" "C5123975")
\t(fp_line (start -1.55 -0.90) (end -1.55 0.90) (layer F.SilkS) (width 0.25))
\t(fp_line (start -0.40 -0.90) (end 0.40 -0.90) (layer F.SilkS) (width 0.25))
\t(fp_line (start 1.55 -0.90) (end 1.55 0.90) (layer F.SilkS) (width 0.25))
\t(pad 1 smd rect (at -0.95 1.30 -90.00) (size 1.000 0.600) (layers F.Cu F.Paste F.Mask))
\t(pad 2 smd rect (at 0.00 1.30 -90.00) (size 1.000 0.600) (layers F.Cu F.Paste F.Mask))
\t(pad 3 smd rect (at 0.95 1.30 -90.00) (size 1.000 0.600) (layers F.Cu F.Paste F.Mask))
\t(pad 4 smd rect (at 0.95 -1.30 -90.00) (size 1.000 0.600) (layers F.Cu F.Paste F.Mask))
\t(pad 5 smd rect (at -0.95 -1.30 -90.00) (size 1.000 0.600) (layers F.Cu F.Paste F.Mask))
\t(fp_circle (center -1.65 1.52) (end -1.52 1.52) (layer F.SilkS) (width 0.25))
)
"""

# Modern KiCad dialect: (footprint ..), quoted layer names/pad numbers,
# (stroke (width ..)), roundrect pads. IPC orientation: pins 1-3 down the LEFT.
# The fp_arc is a 3-point quarter circle (radius 1, centred on the origin).
KICAD_SOT23_5 = """\
(footprint "SOT-23-5"
\t(version 20240108)
\t(layer "F.Cu")
\t(attr smd)
\t(fp_line
\t\t(start -0.65 -1.61)
\t\t(end 1.75 -1.61)
\t\t(stroke (width 0.12) (type solid))
\t\t(layer "F.SilkS")
\t)
\t(fp_arc
\t\t(start 1 0)
\t\t(mid 0.707107 0.707107)
\t\t(end 0 1)
\t\t(stroke (width 0.12) (type solid))
\t\t(layer "F.SilkS")
\t)
\t(pad "1" smd roundrect
\t\t(at -1.1375 -0.95)
\t\t(size 1.325 0.6)
\t\t(layers "F.Cu" "F.Mask" "F.Paste")
\t\t(roundrect_rratio 0.25)
\t)
\t(pad "2" smd roundrect
\t\t(at -1.1375 0)
\t\t(size 1.325 0.6)
\t\t(layers "F.Cu" "F.Mask" "F.Paste")
\t\t(roundrect_rratio 0.25)
\t)
\t(pad "3" smd roundrect
\t\t(at -1.1375 0.95)
\t\t(size 1.325 0.6)
\t\t(layers "F.Cu" "F.Mask" "F.Paste")
\t\t(roundrect_rratio 0.25)
\t)
\t(pad "4" smd roundrect
\t\t(at 1.1375 0.95)
\t\t(size 1.325 0.6)
\t\t(layers "F.Cu" "F.Mask" "F.Paste")
\t\t(roundrect_rratio 0.25)
\t)
\t(pad "5" smd roundrect
\t\t(at 1.1375 -0.95)
\t\t(size 1.325 0.6)
\t\t(layers "F.Cu" "F.Mask" "F.Paste")
\t\t(roundrect_rratio 0.25)
\t)
)
"""

# Old-dialect arc form: start = centre, end = point on the arc, angle = sweep.
OLD_ARC = """\
(module arc_test (layer F.Cu)
\t(fp_arc (start 0 0) (end 1 0) (angle 90) (layer F.SilkS) (width 0.2))
)
"""

# A through-hole part with an oval pad and back silk, for pill + flip tests.
THT_OVAL = """\
(footprint "tht"
\t(layer "F.Cu")
\t(pad "1" thru_hole rect (at -1.27 0) (size 1.7 1.7) (layers "*.Cu" "*.Mask"))
\t(pad "2" thru_hole oval (at 1.27 0) (size 1.0 2.0) (layers "*.Cu" "*.Mask"))
\t(fp_line (start -1 -2) (end 1 -2) (stroke (width 0.12)) (layer "B.SilkS"))
)
"""

_TMP = Path(tempfile.mkdtemp(prefix="easyeda-test-"))


def _fp(text: str, name: str) -> R.Footprint:
    path = _TMP / f"{name}.kicad_mod"
    path.write_text(text)
    return R.load_footprint(path)


EASYEDA = _fp(EASYEDA_SOT23_5, "easyeda_sot23_5")
KICAD = _fp(KICAD_SOT23_5, "kicad_sot23_5")


def _mk(pads: list[tuple[float, float, str]]) -> R.Footprint:
    """Footprint from bare (x, y, number) pads, for synthetic rotation cases."""
    return R.Footprint(
        "synthetic",
        [R.Pad(x, y, 0.0, 0.6, 0.6, "rect", "F", num) for x, y, num in pads],
        [],
        [],
    )


# --------------------------------------------------------------------------- #
# parsing: both dialects
# --------------------------------------------------------------------------- #
def test_parse_easyeda_dialect():
    assert len(EASYEDA.pads) == 5
    assert [p.number for p in EASYEDA.pads] == ["1", "2", "3", "4", "5"]
    assert all(p.side == "F" for p in EASYEDA.pads)
    assert EASYEDA.pads[0].rot == -90.0
    assert len(EASYEDA.silk_segs) == 3  # (width ..) form parsed
    assert len(EASYEDA.silk_circles) == 1
    assert abs(EASYEDA.silk_circles[0].r - 0.13) < 1e-9


def test_parse_modern_dialect():
    assert [p.number for p in KICAD.pads] == ["1", "2", "3", "4", "5"]
    assert KICAD.pads[0].shape == "roundrect"
    assert KICAD.pads[0].w == 1.325
    # (stroke (width ..)) form parsed, and the line survives with its width
    plain_lines = [s for s in KICAD.silk_segs if s.width == 0.12]
    assert plain_lines, "modern stroke width not parsed"


def test_through_hole_pads_on_both_sides():
    tht = _fp(THT_OVAL, "tht")
    assert all(p.side == "*" for p in tht.pads)
    assert tht.has_back()  # *.Cu pads + B.SilkS enable the flip view


# --------------------------------------------------------------------------- #
# arcs: must be curves, not chords (bug: pin-1 arc rendered as a gap)
# --------------------------------------------------------------------------- #
def test_modern_three_point_arc_is_a_curve():
    # the fixture's quarter circle (radius 1 about the origin) expands to many
    # segments whose points all sit on the circle — a chord would not
    arc_segs = [s for s in KICAD.silk_segs if s.width == 0.12 and abs(s.x1) <= 1.01 and abs(s.y1) <= 1.01]
    on_circle = [s for s in arc_segs if abs((s.x1**2 + s.y1**2) ** 0.5 - 1.0) < 0.01]
    assert len(on_circle) > 5, f"arc not sampled into a curve ({len(on_circle)} segs)"


def test_old_centre_angle_arc_is_a_curve():
    fp = _fp(OLD_ARC, "old_arc")
    assert len(fp.silk_segs) > 5
    for s in fp.silk_segs:
        assert abs((s.x1**2 + s.y1**2) ** 0.5 - 1.0) < 0.01  # radius 1 about (0,0)
    # 90° sweep from (1,0) ends at (0,1)
    assert abs(fp.silk_segs[-1].x2) < 0.01 and abs(fp.silk_segs[-1].y2 - 1.0) < 0.01


# --------------------------------------------------------------------------- #
# oval pads are pills (stadium), not ellipses
# --------------------------------------------------------------------------- #
def test_oval_pad_is_a_pill():
    tht = _fp(THT_OVAL, "tht2")
    oval = next(p for p in tht.pads if p.shape == "oval")
    # vertical pill 1.0 x 2.0: at y = cap-start (h/2 - r = 0.5) the full width is
    # still present (flat parallel sides); an ellipse would already have narrowed
    # to w * sqrt(1 - (0.5/1.0)^2) ≈ 0.87w there.
    s = 40.0  # px/mm
    size = 128
    mask = R._pad_mask(R.Footprint("o", [R.Pad(0, 0, 0, oval.w, oval.h, "oval", "F", "2")], [], []), 0, s, size)
    cx = cy = size / 2
    edge_x = int(cx + (oval.w / 2) * s) - 2  # just inside the side wall
    cap_y = int(cy + (oval.h / 2 - oval.w / 2) * s)  # where the cap begins
    assert mask.getpixel((edge_x, cap_y)) == 255, "pill side wall missing (ellipse regression)"


# --------------------------------------------------------------------------- #
# rotation suggestion: pin numbers must line up (bug: symmetric layouts tied at
# 90/270 and could put pin 1 diagonally opposite)
# --------------------------------------------------------------------------- #
def test_rotation_matches_pin_numbers_not_just_geometry():
    ref = _mk([(0, -1, "1"), (0, 1, "2")])  # vertical, pin 1 up top
    cand = _mk([(-1, 0, "1"), (1, 0, "2")])  # horizontal, pin 1 on the left
    # geometrically 90 and 270 both align the layouts; only 90 maps 1 -> 1
    assert R.suggest_rotation(ref, cand) == 90
    # swap the candidate's labels: now 270 is the number-preserving choice
    swapped = _mk([(-1, 0, "2"), (1, 0, "1")])
    assert R.suggest_rotation(ref, swapped) == 270


def test_rotation_falls_back_to_geometry_without_numbers():
    ref = _mk([(0, -1, ""), (0, 1, "")])
    cand = _mk([(-1, 0, ""), (1, 0, "")])
    assert R.suggest_rotation(ref, cand) in (90, 270)  # tie is fine, must not crash


def test_rotation_easyeda_vs_kicad_sot23_5():
    # EasyEDA reel orientation (pins 1-3 across the bottom) vs KiCad IPC
    # orientation (pins 1-3 down the left): 270° CCW aligns pin numbers.
    assert R.suggest_rotation(EASYEDA, KICAD) == 270


# --------------------------------------------------------------------------- #
# pad-overlap ranking
# --------------------------------------------------------------------------- #
def test_pad_mismatch_identity_is_zero():
    assert R.pad_mismatch(EASYEDA, EASYEDA, 0) == 0


def test_pad_mismatch_ranks_closer_geometry_first():
    grown = R.Footprint(
        "grown",
        [R.Pad(p.x, p.y, p.rot, p.w * 1.5, p.h * 1.5, p.shape, p.side, p.number) for p in EASYEDA.pads],
        [],
        [],
    )
    doubled = R.Footprint(
        "doubled",
        [R.Pad(p.x, p.y, p.rot, p.w * 2.5, p.h * 2.5, p.shape, p.side, p.number) for p in EASYEDA.pads],
        [],
        [],
    )
    a = R.pad_mismatch(EASYEDA, grown, 0)
    b = R.pad_mismatch(EASYEDA, doubled, 0)
    assert 0 < a < b, f"expected 0 < {a} < {b}"


# --------------------------------------------------------------------------- #
# key handling (bugs: arrows exited the program; Ctrl-C must abort, plain c not)
# --------------------------------------------------------------------------- #
def test_interpret_csi_key_events():
    cases = [
        (("", "A"), ("up", "press")),  # legacy arrow burst
        (("1;1:3", "B"), ("down", "release")),  # kitty arrow release
        (("32;1:1", "u"), ("peek", "press")),  # space press
        (("32;1:3", "u"), ("peek", "release")),  # space release (peek ends)
        (("113", "u"), ("cancel", "press")),  # q
        (("113:81;2", "u"), ("other", "press")),  # Shift-Q: uppercase is deliberately unmapped
        (("47:63;2", "u"), ("help", "press")),  # '?' = Shift-/ (shifted codepoint is the typed key)
        (("47;2", "u"), ("other", "press")),  # Shift-/ without alternate: base '/' unmapped
        (("113;;113", "u"), ("cancel", "press")),  # Ghostty with text flag: empty mods group
        (("99;5", "u"), ("abort", "press")),  # Ctrl-C in kitty mode
        (("3", "u"), ("abort", "press")),  # Ctrl-C as codepoint 3
        (("99", "u"), ("other", "press")),  # plain c is NOT abort
    ]
    for (params, final), expected in cases:
        got = tui._interpret_csi(params, final)
        assert got == expected, f"CSI {params!r},{final!r}: {got} != {expected}"


# --------------------------------------------------------------------------- #
# frame drawing (bug: shorter titles left leftovers from longer ones)
# --------------------------------------------------------------------------- #
def test_frames_erase_stale_text_and_peek_shows_reference():
    import os

    orig = shutil.get_terminal_size
    shutil.get_terminal_size = lambda fallback=(90, 40): os.terminal_size((80, 30))
    try:
        cands = [
            tui.Candidate("EasyEDA: SOT-23-5_L3.0", _TMP / "easyeda_sot23_5.kicad_mod", "e", "EasyEDA (keep)"),
            tui.Candidate("Package_TO_SOT_SMD:SOT-23-5", _TMP / "kicad_sot23_5.kicad_mod", "k"),
        ]
        fps = [tui.load_footprint(c.path) for c in cands]
        frame = tui._build_frame(cands, fps, selected=1, shown=1, rotation=270)
        assert frame.startswith(tui.HOME)
        assert tui.EL + "\n" in frame and frame.endswith(tui.EL + tui.ED)
        assert "FT rot: +270°" in frame
        peek = tui._build_frame(cands, fps, selected=1, shown=0, peek=True)
        assert "PEEK: EasyEDA" in peek and "EasyEDA: SOT-23-5" in peek
    finally:
        shutil.get_terminal_size = orig


# --------------------------------------------------------------------------- #
# importer post-processing
# --------------------------------------------------------------------------- #
SYMBOL_LIB = """\
(kicad_symbol_lib
  (version 20211014)
  (symbol "LGS6302"
    (property
      "Footprint"
      "0_test:SOT-23-5_L3.0-W1.6-P0.95-LS2.8-BL"
      (id 2)
      (at 0 0 0)
      (effects (font (size 1.27 1.27) ) hide)
    )
    (property
      "LCSC Part"
      "C5123975"
      (id 6)
      (at 0 0 0)
      (effects (font (size 1.27 1.27) ) hide)
    )
  )
)
"""


def test_rename_lcsc_field():
    lib = _TMP / "rename.kicad_sym"
    lib.write_text(SYMBOL_LIB)
    imp.rename_symbol_lcsc_field(lib)
    text = lib.read_text()
    assert '"LCSC"' in text and '"LCSC Part"' not in text
    imp.rename_symbol_lcsc_field(lib)  # idempotent
    assert lib.read_text() == text


def test_set_symbol_property_inserts_parseable_ft_rotation():
    lib = _TMP / "ftrot.kicad_sym"
    lib.write_text(SYMBOL_LIB.replace('"LCSC Part"', '"LCSC"'))
    imp._set_symbol_property(lib, "C5123975", "FT Rotation Offset", "270")
    text = lib.read_text()
    assert '(property "FT Rotation Offset" "270"' in text
    root = R.parse_sexpr(text)  # still a well-formed s-expression
    assert root and root[0] == "kicad_symbol_lib"


def test_symbols_by_lcsc_index():
    lib = _TMP / "index.kicad_sym"
    lib.write_text(SYMBOL_LIB.replace('"LCSC Part"', '"LCSC"'))
    index = imp._symbols_by_lcsc(lib)
    assert index["C5123975"]["Footprint"].startswith("0_test:")


def test_remove_generated_footprint_removes_models_keeps_others():
    proj = _TMP / "rmproj"
    (proj / "lib.pretty").mkdir(parents=True, exist_ok=True)
    (proj / "lib.3dshapes").mkdir(exist_ok=True)
    mod = proj / "lib.pretty/FP.kicad_mod"
    mod.write_text('(module x (layer F.Cu)\n  (model "${KIPRJMOD}/lib.3dshapes/FP.wrl")\n)')
    for name in ("FP.wrl", "FP.step", "OTHER.wrl"):
        (proj / "lib.3dshapes" / name).write_text("x")
    removed = imp._remove_generated_footprint(mod, proj)
    assert {p.name for p in removed} == {"FP.kicad_mod", "FP.wrl", "FP.step"}
    assert (proj / "lib.3dshapes/OTHER.wrl").exists()


# --------------------------------------------------------------------------- #
# project resolution (bug/feature: --project + multi-project disambiguation)
# --------------------------------------------------------------------------- #
def test_resolve_project_requires_disambiguation():
    proj = _TMP / "multi"
    proj.mkdir(exist_ok=True)
    (proj / "alpha.kicad_pro").write_text("{}")
    (proj / "beta.kicad_pro").write_text("{}")
    try:
        imp.resolve_project(proj)
        raise AssertionError("expected SystemExit for multiple projects")
    except SystemExit as e:
        assert "--project" in str(e)
    root, pro = imp.resolve_project(proj / "beta.kicad_pro")
    assert root == proj.resolve() and pro.name == "beta.kicad_pro"


def test_package_token():
    assert matcher.package_token("SOT-23-5_L3.0-W1.6-P0.95-LS2.8-BL") == "SOT-23-5"
    assert matcher.package_token("SO-8_L4.9-W3.9-P1.27-LS6.0-BL-EP") == "SO-8"
    assert matcher.package_token("SOT-23-5") == "SOT-23-5"
    # a `<n>P-` pad-count segment before the dims must not defeat extraction
    assert matcher.package_token("CRYSTAL-SMD_4P-L3.2-W2.5-BL") == "CRYSTAL-SMD"
    assert matcher.package_token("SENSOR-TH_6P-L8.5-W8.5-P2.54-LS10.6-TL") == "SENSOR-TH"
    # no dims at all: fall back to the first _-segment
    assert matcher.package_token("USB-C-SMD_TYPE-C-16PIN-2MD-073") == "USB-C-SMD"


def test_search_tokens_powerpad_and_aliases():
    # TI PowerPAD spelling and plain SO-8 + -EP flag both expand to the KiCad
    # exposed-pad names; a plain SO-8 (no EP) must NOT.
    tokens, _ = matcher._search_tokens("SOPOWERPAD-8", None, "SOPOWERPAD-8_L4.9-W3.9-P1.27-LS6.0-TL-EP")
    assert "SOIC-8-1EP" in tokens and "PDSO-G8" in tokens
    tokens, _ = matcher._search_tokens("SO-8", None, "SO-8_L4.9-W3.9-P1.27-LS6.0-BL-EP")
    assert "SOIC-8-1EP" in tokens and "SOIC-8" in tokens
    tokens, _ = matcher._search_tokens("SO-8", None, "SO-8_L4.9-W3.9-P1.27-LS6.0-BL")
    assert "SOIC-8-1EP" not in tokens
    # cross-naming-scheme aliases
    tokens, _ = matcher._search_tokens("CRYSTAL-SMD", None, "CRYSTAL-SMD_4P-L3.2-W2.5-BL")
    assert "Crystal_SMD" in tokens
    tokens, _ = matcher._search_tokens("USB-C-SMD", None, "USB-C-SMD_TYPE-C-16PIN-2MD-073")
    assert "USB_C_Receptacle" in tokens


# --------------------------------------------------------------------------- #
# standard-symbol detection (feature: tell the user the import was unnecessary)
# --------------------------------------------------------------------------- #
STD_SYMBOL_LIB = """\
(kicad_symbol_lib
  (version 20241209)
  (symbol "AMS1117-3.3"
    (symbol "AMS1117-3.3_0_1" (rectangle (start -5 5) (end 5 -5)))
  )
  (symbol "W25Q128JVS"
    (symbol "W25Q128JVS_1_1" (rectangle (start -5 5) (end 5 -5)))
  )
  (symbol "RP2350A")
)
"""


def _std_symbol_index() -> dict[str, list[str]]:
    import os

    root = _TMP / "symroot"
    root.mkdir(exist_ok=True)
    (root / "Test_Lib.kicad_sym").write_text(STD_SYMBOL_LIB)
    orig = os.environ.get("XDG_CACHE_HOME")
    os.environ["XDG_CACHE_HOME"] = str(_TMP / "cache")
    try:
        return sym.load_index(root)
    finally:
        if orig is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = orig


def test_symbol_index_skips_subunits():
    index = _std_symbol_index()
    refs = [r for refs in index.values() for r in refs]
    assert "Test_Lib:AMS1117-3.3" in refs and "Test_Lib:RP2350A" in refs
    assert not any("_0_1" in r or "_1_1" in r for r in refs)


def test_symbol_match_exact_and_prefix():
    index = _std_symbol_index()
    exact = sym.match_mpn("AMS1117-3.3", index)
    assert exact and exact[0].exact and exact[0].ref == "Test_Lib:AMS1117-3.3"
    # packaging suffix: the standard name is a prefix of the MPN
    pref = sym.match_mpn("W25Q128JVSIQTR", index)
    assert pref and not pref[0].exact and pref[0].ref == "Test_Lib:W25Q128JVS"
    # unrelated and too-short MPNs match nothing
    assert sym.match_mpn("DRV8251ADDAR", index) == []
    assert sym.match_mpn("RP2", index) == []


def test_symbol_index_cache_reused_and_invalidated():
    import json
    import os

    root = _TMP / "symroot2"
    root.mkdir(exist_ok=True)
    lib = root / "Only.kicad_sym"
    lib.write_text('(kicad_symbol_lib (symbol "PARTONE"))')
    os.environ["XDG_CACHE_HOME"] = str(_TMP / "cache2")
    try:
        first = sym.load_index(root)
        assert sym.normalize("PARTONE") in first
        cache_files = list((_TMP / "cache2/kkh-import").glob("symbol-index-*.json"))
        assert len(cache_files) == 1
        # poison the cached index: an unchanged library must be served from it
        data = json.loads(cache_files[0].read_text())
        data["index"]["CANARY"] = ["Only:CANARY"]
        cache_files[0].write_text(json.dumps(data))
        assert "CANARY" in sym.load_index(root)
        # touching the library invalidates the cache and drops the canary
        lib.write_text('(kicad_symbol_lib (symbol "PARTONE") (symbol "PARTTWO"))')
        os.utime(lib, (1, 1))
        rebuilt = sym.load_index(root)
        assert "CANARY" not in rebuilt and sym.normalize("PARTTWO") in rebuilt
    finally:
        os.environ.pop("XDG_CACHE_HOME", None)


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
