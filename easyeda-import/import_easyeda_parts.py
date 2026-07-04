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
import shutil
import sys
import tempfile
from dataclasses import dataclass
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
        help="Reimport parts the project library already has, replacing their symbol/footprint/3D model.",
    )
    parser.add_argument(
        "--include-3d-models",
        action="store_true",
        help=(
            "Download the EasyEDA 3D models and keep their footprint references. "
            "By default they are skipped (KiCad has its own 3D libraries) and the "
            "footprint's (model ..) reference is stripped so nothing dangles."
        ),
    )
    parser.add_argument(
        "--import-duplicates",
        action="store_true",
        help=(
            "Import a copy without asking when a part is already provided by an installed "
            "KiCad library (e.g. the JLCPCB-Kicad-Library)."
        ),
    )
    parser.add_argument(
        "--kicad-config-dir",
        type=Path,
        help=(
            "Override the KiCad config dir holding the global sym-lib-table "
            "(default: newest of ~/.config/kicad/{10.0,9.0,8.0})."
        ),
    )
    parser.add_argument(
        "--no-standard-footprints",
        action="store_true",
        help="Skip matching imported parts against KiCad's standard footprint libraries.",
    )
    parser.add_argument(
        "--no-standard-symbols",
        action="store_true",
        help="Skip checking whether KiCad's standard symbol libraries already have the part.",
    )
    parser.add_argument(
        "--kicad-symbols-dir",
        type=Path,
        help="Override the location of KiCad's standard symbol libraries (dir of *.kicad_sym).",
    )
    parser.add_argument(
        "--no-passive-style",
        action="store_true",
        help=(
            "Keep easyeda2kicad's generated symbols for resistors/capacitors instead of "
            "restyling them to match the CDFER JLCPCB-Kicad-Library (hidden pin numbers, "
            "value + voltage rating shown)."
        ),
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
        "--non-interactive",
        action="store_true",
        help=(
            "Never prompt: skip parts an installed library already provides, import the rest "
            "with their generated footprints, and print the standard-footprint candidates. "
            "Combine with --auto-single to still substitute unambiguous matches."
        ),
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
                f'{table.name} already has library "{name}" pointing to {existing_uri}, not {uri}.'
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


@dataclass
class SymbolInfo:
    name: str
    props: dict[str, str]


def _symbols_by_lcsc(symbol_lib: Path) -> dict[str, SymbolInfo]:
    """Map each symbol's LCSC id -> its name and property dict (Reference, Footprint, ...)."""
    _ensure_chooser_path()
    from kicad_mod_render import parse_sexpr

    root = parse_sexpr(symbol_lib.read_text())
    result: dict[str, SymbolInfo] = {}
    for node in root:
        if not isinstance(node, list) or len(node) < 2 or node[0] != "symbol":
            continue
        props: dict[str, str] = {}
        for child in node:
            if isinstance(child, list) and child and child[0] == "property" and len(child) >= 3:
                props[str(child[1])] = str(child[2])
        lcsc = props.get("LCSC")
        if lcsc:
            result[lcsc] = SymbolInfo(str(node[1]), props)
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


def _strip_3d_model_refs(text: str) -> str:
    """Remove every `(model ..)` block from a .kicad_mod, including its leading
    indentation and trailing newline. easyeda2kicad writes the reference even
    when the 3D file itself is not downloaded, so a footprint imported without
    models would otherwise carry a dangling ${KIPRJMOD} path KiCad warns about.
    The scan is quote-aware so parens inside strings don't unbalance it."""
    while True:
        m = re.search(r"[ \t]*\(model\b", text)
        if not m:
            return text
        open_paren = text.index("(", m.start())
        depth = 0
        in_string = escaped = False
        end = None
        for i in range(open_paren, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            return text  # malformed; leave it rather than corrupt the file
        if end < len(text) and text[end] == "\n":
            end += 1
        text = text[: m.start()] + text[end:]


def extract_symbol_block(lib_text: str, symbol_name: str) -> str | None:
    """The balanced `(symbol "<name>" ...)` block of a top-level symbol,
    including its leading indentation. The scan is quote-aware (with backslash
    escapes) so parens inside strings don't unbalance it; sub-unit symbols are
    nested inside their parent, so scanning to balance from the top-level
    header captures them too."""
    match = re.search(r'^[ \t]*\(symbol\s+"' + re.escape(symbol_name) + '"', lib_text, re.MULTILINE)
    if not match:
        return None
    depth = 0
    in_string = escaped = False
    for i in range(match.start(), len(lib_text)):
        ch = lib_text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return lib_text[match.start() : i + 1]
    return None


def merge_symbol_into_lib(project_lib: Path, staged_text: str, symbol_name: str) -> None:
    """Copy one symbol block from a staged library into the project library,
    creating it (with the staged header, so the format version matches the
    symbols) or replacing an existing block of the same name."""
    block = extract_symbol_block(staged_text, symbol_name)
    if block is None:
        raise SystemExit(f'Staged library is missing symbol "{symbol_name}".')

    if not project_lib.exists():
        first_symbol = re.search(r'^[ \t]*\(symbol\s+"', staged_text, re.MULTILINE)
        header = staged_text[: first_symbol.start()]
        project_lib.parent.mkdir(parents=True, exist_ok=True)
        project_lib.write_text(header + block + "\n)\n")
        return

    text = project_lib.read_text()
    existing = extract_symbol_block(text, symbol_name)
    if existing is not None:
        text = text.replace(existing, block, 1)
    else:
        last = text.rfind(")")
        if last == -1:
            raise SystemExit(f"{project_lib} is not a valid symbol library.")
        text = text[:last].rstrip("\n") + "\n" + block + "\n" + text[last:]
    project_lib.write_text(text)


def seed_staged_symbol_lib(staged_lib: Path, project_lib: Path) -> None:
    """Give the staged library the project library's format version:
    easyeda2kicad emits symbols in the largest dialect <= the version it reads
    from the target file, so this keeps new blocks mergeable into the project
    file without mixing dialects."""
    if not project_lib.exists():
        return
    match = re.search(r"\(version\s+(\d+)\)", project_lib.read_text()[:512])
    if not match:
        return
    staged_lib.write_text(f"(kicad_symbol_lib\n  (version {match.group(1)})\n)\n")


def filter_already_imported(parts: list[str], symbol_lib: Path, lib_name: str, *, overwrite: bool) -> list[str]:
    """Drop parts the project library already has (unless --overwrite)."""
    if overwrite or not symbol_lib.exists():
        return parts
    index = _symbols_by_lcsc(symbol_lib)
    kept = []
    for part in parts:
        info = index.get(part)
        if info:
            print(
                f"[duplicates] {part}: already imported as {lib_name}:{info.name}; use --overwrite to reimport."
            )
        else:
            kept.append(part)
    return kept


def confirm_duplicates(
    parts: list[str],
    installed: dict[str, list],
    *,
    import_duplicates: bool,
    interactive: bool,
    ask=input,
) -> list[str]:
    """Handle parts that an installed library (e.g. the JLCPCB-Kicad-Library)
    already provides: those symbols are hand-curated and directly placeable, so
    skipping the import is the default and the existing ref is printed instead."""
    kept = []
    for part in parts:
        hits = installed.get(part)
        if not hits:
            kept.append(part)
            continue
        refs = ", ".join(h.ref for h in hits)
        if import_duplicates:
            print(f"[duplicates] {part}: also in your installed libraries ({refs}); importing a copy anyway.")
            kept.append(part)
        elif interactive:
            print(f"[duplicates] {part} is already in your installed libraries:")
            for h in hits:
                print(f"[duplicates]   {h.ref}")
            if ask(f"[duplicates] import a copy of {part} anyway? [y/N] ").strip().lower().startswith("y"):
                kept.append(part)
            else:
                print(f"[duplicates] {part}: skipped — place {hits[0].ref} directly.")
        else:
            print(
                f"[duplicates] {part}: already installed as {refs}; "
                "skipping (use --import-duplicates to import a copy)."
            )
    return kept


def _move_footprint_files(staged_mod: Path, staging_root: Path, project_root: Path) -> None:
    """Move a staged footprint and the 3D models its (model ..) entries point
    at into the project. Staging mirrors the project layout, so the
    ${KIPRJMOD}-relative model paths inside the file stay correct as-is.

    Sibling parts from the same family can share a generated footprint file; the
    first commit moves it, so a later one finds staging empty. When the target is
    already in the project, there is nothing to move -- reuse it."""
    target_mod = project_root / staged_mod.relative_to(staging_root)
    if not staged_mod.is_file() and target_mod.is_file():
        return
    for match in re.finditer(r'\(model\s+"([^"]+)"', staged_mod.read_text()):
        rel = match.group(1).replace("${KIPRJMOD}/", "").replace("${KIPRJMOD}", "")
        if Path(rel).is_absolute():
            continue
        staged_model = staging_root / rel
        for ext in (staged_model.suffix, ".wrl", ".step", ".stp"):
            sibling = staged_model.with_suffix(ext)
            if sibling.is_file():
                target = project_root / sibling.relative_to(staging_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(sibling), str(target))
    target_mod.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged_mod), str(target_mod))


def commit_part(
    project_root: Path,
    staging_root: Path,
    relative_lib_dir: Path,
    lib_name: str,
    part: str,
    info: SymbolInfo,
    *,
    keep_footprint: bool,
    overwrite: bool,
) -> None:
    """Move one confirmed part from the staging tree into the project: the
    symbol block always; the generated footprint and its 3D models only when
    the EasyEDA footprint was kept (a substituted standard footprint lives in
    KiCad's own libraries). Library tables are registered here, idempotently,
    so a fully skipped run never touches the project."""
    staged_dir = staging_root / relative_lib_dir
    project_dir = project_root / relative_lib_dir
    project_sym = project_dir / f"{lib_name}.kicad_sym"

    if overwrite and project_sym.exists():
        # A previous import of this part may have left a generated footprint
        # behind; drop it before its replacement (or a standard ref) lands.
        old = _symbols_by_lcsc(project_sym).get(part)
        old_ref = old.props.get("Footprint", "") if old else ""
        if old_ref.startswith(f"{lib_name}:"):
            old_mod = project_dir / f"{lib_name}.pretty" / (old_ref.split(":", 1)[1] + ".kicad_mod")
            _remove_generated_footprint(old_mod, project_root)

    if keep_footprint:
        fp_ref = info.props.get("Footprint", "")
        fp_name = fp_ref.split(":", 1)[1] if ":" in fp_ref else fp_ref
        _move_footprint_files(
            staged_dir / f"{lib_name}.pretty" / f"{fp_name}.kicad_mod", staging_root, project_root
        )

    merge_symbol_into_lib(project_sym, (staged_dir / f"{lib_name}.kicad_sym").read_text(), info.name)

    ensure_project_library_entry(
        project_root / "sym-lib-table",
        "sym_lib_table",
        lib_name,
        project_uri(relative_lib_dir / f"{lib_name}.kicad_sym"),
    )
    footprint_path = relative_lib_dir / f"{lib_name}.pretty"
    if (project_root / footprint_path).is_dir():
        ensure_project_library_entry(
            project_root / "fp-lib-table",
            "fp_lib_table",
            lib_name,
            project_uri(footprint_path),
        )


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
    quoted = value.replace("\\", "\\\\").replace('"', '\\"')
    block = (
        f'\n{indent}(property "{key}" "{quoted}" (id {new_id}) (at 0 0 0)'
        f" (effects (font (size 1.27 1.27) ) hide))"
    )
    symbol_lib.write_text(text[: m.end()] + block + text[m.end() :])


def _fetch_jlcpcb_descriptions(parts: list[str]) -> dict[str, str]:
    """Look each part up in the JLCPCB parts catalog (anonymous keyword search)
    and return its spec-style description."""
    from easyeda2kicad.easyeda.easyeda_api import EasyedaApi

    api = EasyedaApi()
    found: dict[str, str] = {}
    for part in parts:
        response = api.search_jlcpcb_components(keyword=part, page_size=5)
        for hit in response.get("results", []):
            if hit.get("lcsc") == part and hit.get("description"):
                found[part] = hit["description"]
                break
    return found


def add_missing_descriptions(symbol_lib: Path, parts: list[str], fetch=_fetch_jlcpcb_descriptions) -> None:
    """easyeda2kicad only emits a description property when the EasyEDA CAD
    data carries one, and for most parts that field is empty — the imported
    symbol then shows nothing in KiCad's symbol chooser. The JLCPCB catalog
    almost always has one, so fill the gap from there. The property key follows
    the library's format version, matching what easyeda2kicad would have
    written itself; failures only cost the description, never the import."""
    if not symbol_lib.exists():
        return
    index = _symbols_by_lcsc(symbol_lib)
    missing = [
        part
        for part in parts
        if part in index
        and not (index[part].props.get("Description") or index[part].props.get("ki_description"))
    ]
    if not missing:
        return

    try:
        descriptions = fetch(missing)
    except Exception as exc:
        print(f"[descriptions] JLCPCB lookup failed ({exc}); symbols imported without descriptions.")
        return

    version_match = re.search(r"\(version\s+(\d+)\)", symbol_lib.read_text()[:512])
    modern = version_match and int(version_match.group(1)) >= 20230620
    key = "Description" if modern else "ki_description"
    for part in missing:
        description = descriptions.get(part)
        if description:
            _set_symbol_property(symbol_lib, part, key, description)
        else:
            print(f"[descriptions] {part}: not found in the JLCPCB catalog; imported without a description.")


_RESISTANCE_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)([kKMmuµ]?)Ω")
_CAPACITANCE_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)([pnuµm]?)F(?![A-Za-z])")
_VOLTAGE_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(k?V)(?![A-Za-z])")
_POLARIZED_RE = re.compile(r"tantalum|electrolytic|polymer|aluminum", re.IGNORECASE)


def _passive_value(kind: str, description: str) -> str | None:
    """The primary rating in a JLCPCB/EasyEDA part description: resistance for
    R, capacitance for C. EasyEDA spells kilo-ohms with an uppercase K; the
    CDFER library whose style we mirror writes kΩ."""
    if kind == "R":
        m = _RESISTANCE_RE.search(description)
        return f"{m.group(1)}{m.group(2).replace('K', 'k')}Ω" if m else None
    m = _CAPACITANCE_RE.search(description)
    return f"{m.group(1)}{m.group(2)}F" if m else None


def _rated_voltage(description: str) -> str | None:
    m = _VOLTAGE_RE.search(description)
    return m.group(1) + m.group(2) if m else None


def _symbol_pin_numbers(block: str) -> list[str]:
    """Every pin number in a symbol block, across all sub-unit symbols."""
    _ensure_chooser_path()
    from kicad_mod_render import parse_sexpr

    numbers: list[str] = []

    def walk(node) -> None:
        if not isinstance(node, list) or not node:
            return
        if node[0] == "pin":
            for child in node:
                if isinstance(child, list) and len(child) >= 2 and child[0] == "number":
                    numbers.append(str(child[1]))
        else:
            for child in node:
                walk(child)

    walk(parse_sexpr(block))
    return numbers


def _q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _styled_property(
    indent: str,
    key: str,
    value: str,
    *,
    at: str = "0 0 0",
    font: str = "1.27 1.27",
    justify_left: bool = False,
    lock: bool = False,
    hide: bool = True,
) -> str:
    effects = f"(font (size {font}) )"
    if justify_left:
        effects += " (justify left)"
    if hide:
        effects += " hide"
    lock_line = f"\n{indent}  (do_not_autoplace)" if lock else ""
    return (
        f"{indent}(property {_q(key)} {_q(value)}\n"
        f"{indent}  (at {at}){lock_line}\n"
        f"{indent}  (effects {effects})\n"
        f"{indent})"
    )


def _styled_pin(indent: str, y: float, angle: int, length: float, number: str) -> str:
    # The name must be "" — under a current-format library header KiCad renders
    # the legacy "~" empty-name sentinel as a literal tilde on the pin.
    return (
        f"{indent}(pin passive line\n"
        f"{indent}  (at 0 {y} {angle})\n"
        f"{indent}  (length {length})\n"
        f'{indent}  (name "" (effects (font (size 1.27 1.27))))\n'
        f'{indent}  (number "{number}" (effects (font (size 1.27 1.27))))\n'
        f"{indent})"
    )


def _styled_passive_block(original: str, name: str, kind: str, value: str, voltage: str | None) -> str:
    """A replacement top-level symbol block styled after the CDFER
    JLCPCB-Kicad-Library passives: vertical body sized like theirs, pin numbers
    hidden, the parsed value as the visible Value (small font), and on
    capacitors the rated voltage shown below it. All other properties are
    carried over hidden; the drawing is emitted in the same KiCad-6-era dialect
    easyeda2kicad itself writes into the staged library."""
    _ensure_chooser_path()
    from kicad_mod_render import parse_sexpr

    indent = original[: len(original) - len(original.lstrip())]
    p1, p2, p3 = indent + "  ", indent + "    ", indent + "      "
    node = parse_sexpr(original)
    properties = [
        (str(child[1]), str(child[2]))
        for child in node
        if isinstance(child, list) and len(child) >= 3 and child[0] == "property"
    ]

    def flag(key: str) -> str:
        m = re.search(r"\(%s\s+(\w+)\)" % key, original)
        return m.group(1) if m else "yes"

    lines = [
        f"{indent}(symbol {_q(name)}",
        f"{p1}(pin_numbers hide)",
        f"{p1}(pin_names (offset 0))",
        f"{p1}(in_bom {flag('in_bom')})",
        f"{p1}(on_board {flag('on_board')})",
    ]
    for key, prop_value in properties:
        if key == "Voltage Rated":
            continue  # re-emitted right after Value, keeping re-runs stable
        if key == "Reference":
            if kind == "R":
                lines.append(
                    _styled_property(p1, key, prop_value, at="1.778 0 0", justify_left=True, hide=False)
                )
            else:
                lines.append(
                    _styled_property(p1, key, prop_value, at="2.032 1.668 0", justify_left=True, hide=False)
                )
        elif key == "Value":
            if kind == "R":
                lines.append(
                    _styled_property(p1, key, value, at="0 0 90", font="0.8 0.8", lock=True, hide=False)
                )
            else:
                lines.append(
                    _styled_property(
                        p1, key, value, at="2.032 -0.3782 0", font="0.8 0.8", justify_left=True, hide=False
                    )
                )
            if kind == "C" and voltage:
                lines.append(
                    _styled_property(
                        p1,
                        "Voltage Rated",
                        voltage,
                        at="2.032 -2.0462 0",
                        font="0.8 0.8",
                        justify_left=True,
                        hide=False,
                    )
                )
        else:
            lines.append(_styled_property(p1, key, prop_value))

    lines.append(f"{p1}(symbol {_q(name + '_0_1')}")
    if kind == "R":
        lines.append(
            f"{p2}(rectangle\n"
            f"{p3}(start -1.016 2.54)\n"
            f"{p3}(end 1.016 -2.54)\n"
            f"{p3}(stroke (width 0.254) (type default))\n"
            f"{p3}(fill (type none))\n"
            f"{p2})"
        )
        pin_length = 1.27
    else:
        for plate_y in (0.635, -0.635):
            lines.append(
                f"{p2}(polyline\n"
                f"{p3}(pts (xy -1.27 {plate_y}) (xy 1.27 {plate_y}))\n"
                f"{p3}(stroke (width 0.254) (type default))\n"
                f"{p3}(fill (type none))\n"
                f"{p2})"
            )
        pin_length = 3.175
    lines.append(_styled_pin(p2, 3.81, 270, pin_length, "1"))
    lines.append(_styled_pin(p2, -3.81, 90, pin_length, "2"))
    lines.append(f"{p1})")
    lines.append(f"{indent})")
    return "\n".join(lines)


def restyle_passive_symbols(symbol_lib: Path, parts: list[str]) -> None:
    """Restyle just-imported two-pin resistors and capacitors to match the CDFER
    JLCPCB-Kicad-Library: easyeda2kicad's generated passives show pin numbers
    and use the MPN as the Value, which reads poorly next to that library's
    hand-curated symbols. Anything that is not a simple two-pin R/C (networks,
    polarized capacitors) or whose description yields no value is left as-is."""
    if not symbol_lib.exists():
        return
    text = symbol_lib.read_text()
    for part, info in _symbols_by_lcsc(symbol_lib).items():
        if part not in parts:
            continue
        kind = info.props.get("Reference", "")
        if kind not in ("R", "C"):
            continue
        description = info.props.get("Description") or info.props.get("ki_description") or ""
        if kind == "C" and _POLARIZED_RE.search(description):
            continue
        value = _passive_value(kind, description)
        if not value:
            continue
        block = extract_symbol_block(text, info.name)
        if block is None or sorted(_symbol_pin_numbers(block)) != ["1", "2"]:
            continue
        voltage = _rated_voltage(description) if kind == "C" else None
        text = text.replace(block, _styled_passive_block(block, info.name, kind, value, voltage), 1)
        shown = f"{value}, {voltage}" if voltage else value
        print(f"[style] {part}: {info.name} restyled to the JLCPCB-library look ({shown}).")
    symbol_lib.write_text(text)


def report_standard_symbols(symbol_lib: Path, parts: list[str], args) -> None:
    """Tell the user when KiCad's standard library already has a part's symbol —
    the import may be unnecessary. Report-only, printed before the choosers so
    the user can still skip the part (using the standard symbol is a
    schematic-level choice)."""
    try:
        _ensure_chooser_path()
        import symbol_matcher
    except Exception as exc:  # pragma: no cover - optional feature
        print(f"[symbols] checker unavailable ({exc}); skipping standard-symbol check.")
        return

    root = symbol_matcher.find_symbol_root(args.kicad_symbols_dir)
    if not root:
        print("[symbols] KiCad standard symbol libraries not found; skipping standard-symbol check.")
        return
    index = symbol_matcher.load_index(root)

    for part, info in _symbols_by_lcsc(symbol_lib).items():
        if part not in parts:
            continue
        mpn = info.props.get("Value", "")
        matches = symbol_matcher.match_mpn(mpn, index)
        if not matches:
            continue
        exact = [m for m in matches if m.exact]
        if exact:
            refs = ", ".join(m.ref for m in exact[:3])
            print(
                f"[symbols] {part} ({mpn}): KiCad's standard library already has this part "
                f"({refs}) — you likely don't need the imported symbol."
            )
        else:
            refs = ", ".join(m.ref for m in matches[:3])
            print(f"[symbols] {part} ({mpn}): close standard-library symbol(s): {refs} — worth checking.")


def _staging_root() -> Path:
    """A fresh staging dir, fully resolved: macOS's TMPDIR sits behind a
    symlink (/var -> /private/var), and easyeda2kicad computes 3D model paths
    with relative_to(Path.cwd()) where cwd is always resolved — an unresolved
    root would make that call crash."""
    return Path(tempfile.mkdtemp(prefix="kkh-easyeda-")).resolve()


def stage_args(output_base: Path, parts: list[str], *, include_3d: bool = False) -> list[str]:
    # IDs last: easyeda2kicad has no positional for part numbers, so they must be
    # passed via --lcsc_id. Keeping the nargs="+" list at the end matches the
    # upstream README and avoids it swallowing a following flag. --overwrite is
    # always safe here: the output is a fresh staging tree. --full pulls the 3D
    # model too; without it we ask only for symbol + footprint.
    mode = ["--full"] if include_3d else ["--symbol", "--footprint"]
    return [*mode, "--output", str(output_base), "--project-relative", "--overwrite", "--lcsc_id", *parts]


def stage_parts(
    parts: list[str],
    staging_root: Path,
    relative_lib_dir: Path,
    lib_name: str,
    project_symbol_lib: Path,
    *,
    include_3d: bool = False,
) -> None:
    """Download and convert into a temp tree that mirrors the project layout.
    easyeda2kicad writes its ${KIPRJMOD}-relative 3D model paths relative to
    the cwd, so running it from the staging root produces files that are
    already correct for their final project location — nothing to rewrite.
    Unless include_3d, the 3D models are neither fetched nor referenced: their
    (model ..) lines are stripped from the staged footprints."""
    out_dir = staging_root / relative_lib_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    seed_staged_symbol_lib(out_dir / f"{lib_name}.kicad_sym", project_symbol_lib)

    from easyeda2kicad.__main__ import main as easyeda_main

    cwd = os.getcwd()
    os.chdir(staging_root)
    try:
        return_code = easyeda_main(stage_args(out_dir / lib_name, parts, include_3d=include_3d))
    finally:
        os.chdir(cwd)
    if return_code:
        print("[import] easyeda2kicad reported errors; continuing with the parts that staged.")

    if not include_3d:
        for mod in (out_dir / f"{lib_name}.pretty").glob("*.kicad_mod"):
            mod.write_text(_strip_3d_model_refs(mod.read_text()))


def confirm_and_commit_parts(
    project_root: Path,
    staging_root: Path,
    relative_lib_dir: Path,
    lib_name: str,
    parts: list[str],
    args: argparse.Namespace,
) -> list[str]:
    """Per-part confirmation: the footprint chooser doubles as the import gate.
    Enter imports the part with the highlighted footprint (the generated EasyEDA
    one, or a standard-library match in its place); q/Esc skips the part without
    writing anything into the project. Returns the parts actually imported."""
    staged_dir = staging_root / relative_lib_dir
    staged_sym = staged_dir / f"{lib_name}.kicad_sym"
    index = _symbols_by_lcsc(staged_sym) if staged_sym.exists() else {}
    interactive = not args.non_interactive and sys.stdin.isatty() and sys.stdout.isatty()

    matcher = tui = fp_root = None
    try:
        matcher, tui = _import_chooser_modules()
    except Exception as exc:  # pragma: no cover - optional feature
        print(f"[import] chooser unavailable ({exc}); importing without the footprint chooser.")
    if matcher and not args.no_standard_footprints:
        fp_root = matcher.find_footprint_root(args.kicad_footprints_dir)
        if not fp_root:
            print("[footprints] KiCad standard footprint libraries not found; no standard-footprint matching.")

    committed: list[str] = []
    for part in parts:
        info = index.get(part)
        if not info:
            print(f"[import] {part}: download failed; nothing imported.")
            continue
        fp_ref = info.props.get("Footprint", "")
        fp_name = fp_ref.split(":", 1)[1] if ":" in fp_ref else ""
        gen_mod = staged_dir / f"{lib_name}.pretty" / f"{fp_name}.kicad_mod"
        # A sibling part from the same family may have already committed this exact
        # footprint, moving it out of staging; then it lives in the project instead.
        project_mod = project_root / relative_lib_dir / f"{lib_name}.pretty" / f"{fp_name}.kicad_mod"
        fp_available = gen_mod.exists() or project_mod.exists()

        matches = []
        if fp_root and gen_mod.exists():
            comp_type = (info.props.get("Reference") or "")[:1] or None
            matches = matcher.find_candidates(gen_mod, fp_root, comp_type=comp_type)

        chosen_ref = None  # None = keep the generated EasyEDA footprint
        rotation = 0
        if args.auto_single and len(matches) == 1:
            chosen_ref = matches[0].ref
            rotation = tui.suggest_rotation(tui.load_footprint(gen_mod), tui.load_footprint(matches[0].path))
        elif interactive and tui and gen_mod.exists() and matches:
            candidates = [
                tui.Candidate(f"{part} EasyEDA: {fp_name}", gen_mod, fp_ref, "EasyEDA (import as-is)"),
                *[tui.Candidate(m.ref, m.path, m.ref) for m in matches],
            ]
            try:
                result = tui.choose(candidates, preselect=0, reference_index=0)
            except KeyboardInterrupt:
                print(f"[import] aborted; {part} and remaining parts were not imported.")
                break
            if result is None:
                print(f"[import] {part}: skipped.")
                continue
            chosen, rotation = result
            if chosen.ref != fp_ref:
                chosen_ref = chosen.ref
        elif interactive and fp_available:
            # Nothing to choose — either no alternatives, or the footprint was
            # already committed by a sibling part sharing it. Import the generated
            # EasyEDA footprint without an empty chooser or a pointless prompt.
            pass
        elif interactive:
            # No footprint to preview (rare: footprint export failed) — still
            # confirm before anything lands in the project.
            if input(f"[import] {part}: import symbol {info.name}? [Y/n] ").strip().lower().startswith("n"):
                print(f"[import] {part}: skipped.")
                continue
        elif matches:
            print(f"[footprints] {part}: {len(matches)} standard candidate(s) for {fp_ref}; keeping generated.")
            for m in matches[:5]:
                print(f"[footprints]   {m.ref}  (pad-area mismatch {m.mismatch}, rot {m.rotation:+d}°)")
            if len(matches) > 5:
                print(f"[footprints]   ... and {len(matches) - 5} more")

        keep = chosen_ref is None
        if not keep:
            text = staged_sym.read_text()
            staged_sym.write_text(text.replace(f'"{fp_ref}"', f'"{chosen_ref}"'))
            if rotation:
                _set_symbol_property(staged_sym, part, "FT Rotation Offset", str(rotation))
        commit_part(
            project_root,
            staging_root,
            relative_lib_dir,
            lib_name,
            part,
            info,
            keep_footprint=keep,
            overwrite=args.overwrite,
        )
        committed.append(part)
        with_note = "" if keep else f", footprint {chosen_ref}{_rot_note(rotation)}"
        print(f"[import] {part}: imported as {lib_name}:{info.name}{with_note}")
    return committed


def main() -> None:
    args = parse_args()
    validate_parts(args.parts)
    parts = list(dict.fromkeys(args.parts))

    project_root, project_file = resolve_project(args.project)
    lib_name = args.lib_name or f"0_{project_file.stem}"
    relative_lib_dir = project_relative_path(project_root, args.lib_dir)
    project_sym = project_root / relative_lib_dir / f"{lib_name}.kicad_sym"

    if args.print_args:
        print(
            "easyeda2kicad",
            *stage_args(project_root / relative_lib_dir / lib_name, parts, include_3d=args.include_3d_models),
        )
        return

    interactive = not args.non_interactive and sys.stdin.isatty() and sys.stdout.isatty()

    parts = filter_already_imported(parts, project_sym, lib_name, overwrite=args.overwrite)
    if parts:
        _ensure_chooser_path()
        import installed_libs

        installed = installed_libs.find_installed(parts, installed_libs.kicad_config_dir(args.kicad_config_dir))
        parts = confirm_duplicates(
            parts, installed, import_duplicates=args.import_duplicates, interactive=interactive
        )
    if not parts:
        print("[import] nothing to import.")
        return

    staging_root = _staging_root()
    try:
        stage_parts(
            parts, staging_root, relative_lib_dir, lib_name, project_sym, include_3d=args.include_3d_models
        )
        staged_sym = staging_root / relative_lib_dir / f"{lib_name}.kicad_sym"
        rename_symbol_lcsc_field(staged_sym)
        add_missing_descriptions(staged_sym, parts)

        if not args.no_standard_symbols:
            report_standard_symbols(staged_sym, parts, args)
        if not args.no_passive_style:
            restyle_passive_symbols(staged_sym, parts)

        committed = confirm_and_commit_parts(
            project_root, staging_root, relative_lib_dir, lib_name, parts, args
        )
        if not committed:
            print("[import] no parts imported; project untouched.")
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
