#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "easyeda2kicad==1.0.1",
#   "Pillow>=10",
# ]
# ///

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

LIB_RE = re.compile(
    r'\(lib\s+\(name\s+"(?P<name>[^"]+)"\).*?\(uri\s+"(?P<uri>[^"]+)"\)',
    re.DOTALL,
)
PART_RE = re.compile(r"^C[0-9]+$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import EasyEDA/LCSC parts into the current KiCad project library using easyeda2kicad.",
    )
    parser.add_argument("parts", nargs="+", help="EasyEDA/LCSC part numbers, e.g. C146324")
    parser.add_argument(
        "--project",
        type=Path,
        help=(
            "Path to the .kicad_pro project file (or a directory containing one). "
            "Defaults to the single .kicad_pro found in the current or a parent directory; "
            "required to disambiguate when several are present."
        ),
    )
    parser.add_argument(
        "--lib-dir",
        type=Path,
        default=Path("."),
        help="Base output directory for the generated libraries. Defaults to the project root.",
    )
    parser.add_argument(
        "--lib-name",
        help=(
            "Shared library base name. Produces <lib-name>.kicad_sym, <lib-name>.pretty/, "
            "and <lib-name>.3dshapes/. Defaults to 0_<project>."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing symbol/footprint/3D model instead of skipping it.",
    )
    parser.add_argument(
        "--no-standard-footprints",
        action="store_true",
        help="Skip matching imported parts against KiCad's standard footprint libraries.",
    )
    parser.add_argument(
        "--auto-single",
        action="store_true",
        help="When exactly one standard footprint matches, substitute it without opening the chooser.",
    )
    parser.add_argument(
        "--kicad-footprints-dir",
        type=Path,
        help="Override the location of KiCad's standard footprint libraries (dir of *.pretty).",
    )
    parser.add_argument(
        "--print-args",
        action="store_true",
        help="Print the generated easyeda2kicad arguments instead of running them.",
    )
    return parser.parse_args()


def find_project_root(start: Path) -> Path:
    for path in (start, *start.parents):
        if list(path.glob("*.kicad_pro")):
            return path
    raise SystemExit("Could not find a KiCad project root above the current directory.")


def find_project_file(project_root: Path) -> Path:
    project_files = sorted(project_root.glob("*.kicad_pro"))
    if not project_files:
        raise SystemExit(f"No .kicad_pro file found in {project_root}.")
    if len(project_files) > 1:
        names = ", ".join(f.name for f in project_files)
        raise SystemExit(
            f"Multiple .kicad_pro files found in {project_root} ({names}); "
            f"pass --project <file.kicad_pro> to choose one."
        )
    return project_files[0]


def resolve_project(project_arg: Path | None) -> tuple[Path, Path]:
    """Return (project_root, project_file). With --project, use the given file (or
    the single .kicad_pro in the given directory); otherwise search up from cwd."""
    if project_arg is not None:
        p = project_arg.expanduser().resolve()
        if p.is_dir():
            return p, find_project_file(p)
        if not p.exists():
            raise SystemExit(f"KiCad project file not found: {project_arg}")
        if p.suffix != ".kicad_pro":
            raise SystemExit(f"Expected a .kicad_pro file, got: {project_arg}")
        return p.parent, p

    root = find_project_root(Path.cwd().resolve())
    return root, find_project_file(root)


def validate_parts(parts: list[str]) -> None:
    bad_parts = [part for part in parts if not PART_RE.match(part)]
    if bad_parts:
        raise SystemExit(f"Expected EasyEDA/LCSC part numbers like C146324, got: {', '.join(bad_parts)}")


def project_relative_path(project_root: Path, path: Path) -> Path:
    absolute_path = path if path.is_absolute() else project_root / path

    try:
        return absolute_path.resolve().relative_to(project_root.resolve())
    except ValueError:
        raise SystemExit(f"Library path must be inside the KiCad project: {path}") from None


def project_uri(path: Path) -> str:
    return "${KIPRJMOD}/" + path.as_posix()


def table_entry(name: str, uri: str) -> str:
    return f'\t(lib (name "{name}") (type "KiCad") (uri "{uri}") (options "") (descr ""))'


def ensure_project_library_entry(table: Path, table_name: str, name: str, uri: str) -> None:
    if not table.exists():
        table.write_text(f"({table_name}\n\t(version 7)\n{table_entry(name, uri)}\n)\n")
        return

    text = table.read_text()
    for match in LIB_RE.finditer(text):
        if match.group("name") != name:
            continue

        existing_uri = match.group("uri")
        if existing_uri != uri:
            raise SystemExit(
                f'{table.name} already has library "{name}" pointing to {existing_uri}, '
                f"not {uri}."
            )
        return

    stripped = text.rstrip()
    entry = table_entry(name, uri)
    if stripped.endswith(")"):
        text = stripped[:-1].rstrip() + f"\n{entry}\n)\n"
    else:
        text = stripped + f"\n{entry}\n"
    table.write_text(text)


def rename_symbol_lcsc_field(symbol_file: Path) -> None:
    """easyeda2kicad stores the LCSC part number under a symbol property named
    "LCSC Part"; rename it to "LCSC" to match our BOM field conventions. The
    literal string only appears as the property key (values are part numbers like
    C146324), so a plain replacement is safe and idempotent across re-imports."""
    if not symbol_file.exists():
        return
    text = symbol_file.read_text()
    updated = text.replace('"LCSC Part"', '"LCSC"')
    if updated != text:
        symbol_file.write_text(updated)


def ensure_project_library_tables(
    project_root: Path,
    symbol_lib: str,
    symbol_path: Path,
    footprint_lib: str,
    footprint_path: Path,
) -> None:
    ensure_project_library_entry(
        project_root / "sym-lib-table",
        "sym_lib_table",
        symbol_lib,
        project_uri(symbol_path),
    )
    ensure_project_library_entry(
        project_root / "fp-lib-table",
        "fp_lib_table",
        footprint_lib,
        project_uri(footprint_path),
    )


def _ensure_chooser_path() -> None:
    """Put this script's own directory on sys.path so the sibling footprint
    modules import cleanly even when run via the bin/ symlink."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)


def _import_chooser_modules():
    """Make the footprint-chooser package importable and return its modules."""
    _ensure_chooser_path()
    import footprint_chooser_tui as tui
    import footprint_matcher as matcher

    return matcher, tui


def _symbols_by_lcsc(symbol_lib: Path) -> dict[str, dict[str, str]]:
    """Map each symbol's LCSC id -> its property dict (Reference, Footprint, ...)."""
    _ensure_chooser_path()
    from kicad_mod_render import parse_sexpr

    root = parse_sexpr(symbol_lib.read_text())
    result: dict[str, dict[str, str]] = {}
    for node in root:
        if not isinstance(node, list) or not node or node[0] != "symbol":
            continue
        props: dict[str, str] = {}
        for child in node:
            if isinstance(child, list) and child and child[0] == "property" and len(child) >= 3:
                props[str(child[1])] = str(child[2])
        lcsc = props.get("LCSC")
        if lcsc:
            result[lcsc] = props
    return result


def _remove_generated_footprint(gen_mod: Path, project_root: Path) -> list[Path]:
    """Delete a generated footprint and its 3D models once a standard footprint
    has replaced it. The 3D model paths are read from the footprint's `(model ..)`
    entries (resolving ${KIPRJMOD} to the project root); the .wrl/.step sibling is
    removed too since easyeda2kicad writes both."""
    removed: list[Path] = []
    try:
        text = gen_mod.read_text()
    except OSError:
        text = ""

    for match in re.finditer(r'\(model\s+"([^"]+)"', text):
        rel = match.group(1).replace("${KIPRJMOD}/", "").replace("${KIPRJMOD}", "")
        model_path = Path(rel) if Path(rel).is_absolute() else project_root / rel
        for ext in (model_path.suffix, ".wrl", ".step", ".stp"):
            sibling = model_path.with_suffix(ext)
            if sibling.is_file() and sibling not in removed:
                sibling.unlink()
                removed.append(sibling)

    if gen_mod.is_file():
        gen_mod.unlink()
        removed.append(gen_mod)
    return removed


def _rot_note(rotation: int) -> str:
    return f" (FT Rotation Offset {rotation:+d}°)" if rotation else ""


def _set_symbol_property(symbol_lib: Path, lcsc_id: str, key: str, value: str) -> None:
    """Insert a property on the symbol identified by its LCSC id, so the
    Fabrication Toolkit picks it up from the schematic. Anchors on that symbol's
    "LCSC" property (unique per part) and drops the new property right after it."""
    text = symbol_lib.read_text()
    lcsc_block = re.compile(
        r'(?P<indent>[ \t]*)\(property\s+"LCSC"\s+"' + re.escape(lcsc_id) + r'".*?\n(?P=indent)\)',
        re.DOTALL,
    )
    m = lcsc_block.search(text)
    if not m:
        return
    indent = m.group("indent")
    ids = [int(n) for n in re.findall(r"\(id (\d+)\)", text)]
    new_id = (max(ids) + 1) if ids else 100
    block = (
        f'\n{indent}(property "{key}" "{value}" (id {new_id}) (at 0 0 0)'
        f" (effects (font (size 1.27 1.27) ) hide))"
    )
    symbol_lib.write_text(text[: m.end()] + block + text[m.end() :])


def substitute_standard_footprints(
    project_root: Path,
    symbol_path: Path,
    footprint_path: Path,
    lib_name: str,
    parts: list[str],
    args: argparse.Namespace,
) -> None:
    """For each imported part, offer KiCad's standard footprint in place of the
    generated one. Confident single matches can auto-substitute (--auto-single);
    otherwise the interactive chooser decides, with the generated one preselected."""
    try:
        matcher, tui = _import_chooser_modules()
    except Exception as exc:  # pragma: no cover - optional feature
        print(f"[footprints] chooser unavailable ({exc}); keeping generated footprints.")
        return

    root = matcher.find_footprint_root(args.kicad_footprints_dir)
    if not root:
        print("[footprints] KiCad standard footprint libraries not found; keeping generated footprints.")
        return

    symbol_lib = project_root / symbol_path
    footprint_dir = project_root / footprint_path
    index = _symbols_by_lcsc(symbol_lib)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    for part in parts:
        props = index.get(part)
        if not props:
            continue
        fp_ref = props.get("Footprint", "")
        if ":" not in fp_ref:
            continue
        _, fp_name = fp_ref.split(":", 1)
        gen_mod = footprint_dir / f"{fp_name}.kicad_mod"
        if not gen_mod.exists():
            continue

        comp_type = (props.get("Reference") or "")[:1] or None
        matches = matcher.find_candidates(gen_mod, root, comp_type=comp_type)
        if not matches:
            print(f"[footprints] {part}: no standard match, kept {fp_ref}")
            continue

        rotation = 0
        if args.auto_single and len(matches) == 1:
            chosen_ref = matches[0].ref
            rotation = tui.suggest_rotation(tui.load_footprint(gen_mod), tui.load_footprint(matches[0].path))
            print(f"[footprints] {part}: auto-substituted {fp_ref} -> {chosen_ref}{_rot_note(rotation)}")
        elif interactive:
            candidates = [
                tui.Candidate(f"EasyEDA: {fp_name}", gen_mod, fp_ref, "EasyEDA (keep)"),
                *[tui.Candidate(m.ref, m.path, m.ref) for m in matches],
            ]
            try:
                result = tui.choose(candidates, preselect=0, reference_index=0)
            except KeyboardInterrupt:
                # Ctrl-C: stop choosing but keep everything already imported/substituted.
                print(f"[footprints] aborted at {part}; it and remaining parts keep their EasyEDA footprints.")
                break
            if result is None:
                print(f"[footprints] {part}: kept {fp_ref}")
                continue
            chosen, rotation = result
            if chosen.ref == fp_ref:
                print(f"[footprints] {part}: kept {fp_ref}")
                continue
            chosen_ref = chosen.ref
            print(f"[footprints] {part}: substituted {fp_ref} -> {chosen_ref}{_rot_note(rotation)}")
        else:
            names = ", ".join(m.ref for m in matches[:5])
            print(
                f"[footprints] {part}: {len(matches)} standard candidate(s) ({names}); "
                f"kept {fp_ref} (run in a terminal or pass --auto-single to substitute)."
            )
            continue

        text = symbol_lib.read_text()
        symbol_lib.write_text(text.replace(f'"{fp_ref}"', f'"{chosen_ref}"'))
        if rotation:
            _set_symbol_property(symbol_lib, part, "FT Rotation Offset", str(rotation))

        removed = _remove_generated_footprint(gen_mod, project_root)
        if removed:
            print(f"[footprints] {part}: removed EasyEDA {', '.join(p.name for p in removed)}")


def main() -> None:
    args = parse_args()
    validate_parts(args.parts)

    project_root, project_file = resolve_project(args.project)
    default_lib_name = f"0_{project_file.stem}"

    os.chdir(project_root)

    lib_name = args.lib_name or default_lib_name
    relative_lib_dir = project_relative_path(project_root, args.lib_dir)
    symbol_path = relative_lib_dir / f"{lib_name}.kicad_sym"
    footprint_path = relative_lib_dir / f"{lib_name}.pretty"
    # easyeda2kicad's --project-relative does Path(f"{output}.3dshapes").relative_to(cwd),
    # which requires an absolute --output. cwd is the project root (we chdir above),
    # so the absolute base still yields a ${KIPRJMOD}-relative 3D model path.
    output_base = project_root / relative_lib_dir / lib_name

    easyeda_args = [
        "--full",
        "--output",
        str(output_base),
        "--project-relative",
    ]
    if args.overwrite:
        easyeda_args.append("--overwrite")
    # IDs last: easyeda2kicad has no positional for part numbers, so they must be
    # passed via --lcsc_id. Keeping the nargs="+" list at the end matches the
    # upstream README and avoids it swallowing a following flag.
    easyeda_args += ["--lcsc_id", *args.parts]

    if args.print_args:
        print("easyeda2kicad", *easyeda_args)
        return

    from easyeda2kicad.__main__ import main as easyeda_main

    return_code = easyeda_main(easyeda_args)
    if return_code:
        raise SystemExit(return_code)

    rename_symbol_lcsc_field(project_root / symbol_path)
    ensure_project_library_tables(project_root, lib_name, symbol_path, lib_name, footprint_path)

    if not args.no_standard_footprints:
        substitute_standard_footprints(
            project_root, symbol_path, footprint_path, lib_name, args.parts, args
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
