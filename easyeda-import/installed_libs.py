#!/usr/bin/env -S uv run --script
# /// script
# dependencies = []
# ///
"""Find LCSC parts the user already has in an installed KiCad symbol library.

Third-party libraries like CDFER's JLCPCB-Kicad-Library carry the LCSC part
number as a symbol property (`(property "LCSC" "C112574")`), so importing such
a part again with easyeda2kicad would just create a worse duplicate. Before
importing, the importer asks this module whether any requested C-number is
already available, and where.

Libraries come from the global sym-lib-table (nicknames the user can actually
place) plus a scan of KiCad's PCM 3rdparty/symbols tree for library files that
were installed but never registered. Candidate files are narrowed with ripgrep
(the JLCPCB library alone is megabytes of s-expressions); only files that
mention a requested C-number get parsed in Python.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_VAR = re.compile(r"\$\{([^}]+)\}")
_LIB_ENTRY = re.compile(
    r'\(lib\s+\(name\s+"(?P<name>[^"]+)"\).*?\(uri\s+"(?P<uri>[^"]+)"\)',
    re.DOTALL,
)
# One pass over a .kicad_sym: track the enclosing symbol, catch LCSC properties.
_SYMBOL_OR_LCSC = re.compile(
    r'\(symbol\s+"(?P<symbol>(?:[^"\\]|\\.)*)"'
    r'|\(property\s+"LCSC(?: Part)?"\s+"(?P<lcsc>C\d+)"'
)
_UNIT_SUFFIX = re.compile(r"_\d+_\d+$")  # sub-unit bodies like NAME_0_1


@dataclass
class InstalledSymbol:
    ref: str  # "PCM_JLCPCB-Crystals:Crystal, 11MHz, 20pF"
    lcsc: str  # "C112574"


def kicad_config_dir(override: Path | None = None) -> Path | None:
    """The versioned KiCad config dir holding the global lib tables. Newest
    version wins across the per-platform locations (Linux XDG, macOS
    Preferences)."""
    if override:
        return override
    bases = [
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kicad",
        Path.home() / "Library/Preferences/kicad",  # macOS
    ]
    for version in ("10.0", "9.0", "8.0"):
        for base in bases:
            if (base / version).is_dir():
                return base / version
    return None


def default_third_party_dir(config_dir: Path) -> Path:
    """Where KiCad's PCM puts downloaded content for this config's version:
    the env var, the path configured in KiCad's own settings, or the first
    per-platform default that exists (Linux share dir, macOS Documents)."""
    major = config_dir.name.split(".")[0]
    env = os.environ.get(f"KICAD{major}_3RD_PARTY")
    if env:
        return Path(env)
    try:
        common = json.loads((config_dir / "kicad_common.json").read_text())
        configured = (common.get("environment", {}).get("vars") or {}).get(f"KICAD{major}_3RD_PARTY")
        if configured:
            return Path(configured)
    except (OSError, ValueError):
        pass
    candidates = [
        Path.home() / ".local/share/kicad" / config_dir.name / "3rdparty",
        Path.home() / "Documents/KiCad" / config_dir.name / "3rdparty",  # macOS
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def expand_kicad_vars(uri: str, vars: dict[str, str]) -> Path | None:
    """Resolve ${KICADn_...} substitutions; None when a variable is unknown
    (that library simply can't be checked)."""
    unresolved = False

    def sub(match: re.Match) -> str:
        nonlocal unresolved
        value = vars.get(match.group(1))
        if value is None:
            unresolved = True
            return ""
        return value

    expanded = _VAR.sub(sub, uri)
    return None if unresolved else Path(expanded)


def discover_symbol_libs(
    config_dir: Path | None,
    third_party: Path | None = None,
) -> list[tuple[str, Path]]:
    """(nickname, path) for every installed symbol library worth checking:
    global sym-lib-table entries first, then unregistered PCM 3rdparty files
    under a synthesized PCM_<stem> nickname."""
    libs: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    if config_dir and third_party is None:
        third_party = default_third_party_dir(config_dir)

    vars: dict[str, str] = {}
    if third_party:
        major = config_dir.name.split(".")[0] if config_dir else "10"
        vars[f"KICAD{major}_3RD_PARTY"] = str(third_party)

    table = config_dir / "sym-lib-table" if config_dir else None
    if table and table.is_file():
        for match in _LIB_ENTRY.finditer(table.read_text()):
            path = expand_kicad_vars(match.group("uri"), vars)
            if path and path.is_file():
                libs.append((match.group("name"), path))
                seen.add(path.resolve())

    if third_party:
        for path in sorted((third_party / "symbols").rglob("*.kicad_sym")):
            if path.resolve() not in seen:
                libs.append((f"PCM_{path.stem}", path))

    return libs


def _files_mentioning(parts: list[str], files: list[Path]) -> list[Path]:
    """Narrow to files containing any requested C-number, via ripgrep when
    available (these libraries are big; most contain no requested part)."""
    pattern = '"(?:' + "|".join(parts) + ')"'
    try:
        result = subprocess.run(
            ["rg", "-l", "--no-messages", "-e", pattern, "--", *map(str, files)],
            capture_output=True,
            text=True,
        )
        return [Path(line) for line in result.stdout.splitlines()]
    except OSError:
        needles = [f'"{part}"' for part in parts]
        return [f for f in files if any(n in f.read_text(errors="replace") for n in needles)]


def _scan_lib(nickname: str, path: Path, wanted: set[str]) -> dict[str, list[InstalledSymbol]]:
    found: dict[str, list[InstalledSymbol]] = {}
    current = ""
    for match in _SYMBOL_OR_LCSC.finditer(path.read_text(errors="replace")):
        symbol = match.group("symbol")
        if symbol is not None:
            if _UNIT_SUFFIX.search(symbol):
                continue
            current = symbol
            if symbol in wanted:
                found.setdefault(symbol, []).append(InstalledSymbol(f"{nickname}:{symbol}", symbol))
        elif (lcsc := match.group("lcsc")) in wanted and current:
            found.setdefault(lcsc, []).append(InstalledSymbol(f"{nickname}:{current}", lcsc))
    return found


def find_installed(
    parts: list[str],
    config_dir: Path | None,
    third_party: Path | None = None,
) -> dict[str, list[InstalledSymbol]]:
    """Map each requested C-number to the installed symbols that already
    provide it (empty dict when nothing is installed)."""
    libs = discover_symbol_libs(config_dir, third_party)
    if not libs or not parts:
        return {}

    by_path = {path: nickname for nickname, path in reversed(libs)}  # first nickname wins
    found: dict[str, list[InstalledSymbol]] = {}
    wanted = set(parts)
    for path in _files_mentioning(parts, list(by_path)):
        for lcsc, symbols in _scan_lib(by_path[path], path, wanted).items():
            found.setdefault(lcsc, []).extend(symbols)
    return found
