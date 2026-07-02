#!/usr/bin/env -S uv run --script
# /// script
# dependencies = []
# ///
"""Detect when KiCad's standard symbol library already has an imported part.

easyeda2kicad always generates a symbol, but for common parts (AMS1117-3.3,
RP2350A, W25Q128JVS, ...) KiCad ships a hand-drawn one that is almost always
nicer — the import was unnecessary. This module scans the installed standard
symbol libraries for names matching a part's MPN so the importer can say so.

Matching is by normalized name (uppercase alphanumerics): an exact match, or a
prefix overlap of >= MIN_OVERLAP characters in either direction. Prefix matches
catch packaging/reel suffixes (W25Q128JVS ~ W25Q128JVSIQTR) and range suffixes
(XGZP6897D ~ XGZP6897D-010KPG) without a per-part mapping table.

The name index over ~200 MB of .kicad_sym files takes a few seconds to build,
so it is cached (keyed by each file's mtime+size) under ~/.cache/kkh-import/.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

MIN_OVERLAP = 6  # normalized chars two names must share before we call it a match

_SYMBOL_NAME = re.compile(r'\(symbol\s+"([^"]+)"')
_UNIT_SUFFIX = re.compile(r"_\d+_\d+$")  # sub-unit bodies like NAME_0_1
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


@dataclass
class SymbolMatch:
    ref: str  # "Lib:Name"
    exact: bool
    overlap: int  # normalized chars shared with the MPN


def find_symbol_root(override: Path | None = None) -> Path | None:
    """Locate the installed standard symbol libraries (dir of *.kicad_sym)."""
    candidates: list[Path] = []
    if override:
        candidates.append(override)
    for var in (
        "KICAD10_SYMBOL_DIR",
        "KICAD9_SYMBOL_DIR",
        "KICAD8_SYMBOL_DIR",
        "KICAD_SYMBOL_DIR",
    ):
        v = os.environ.get(var)
        if v:
            candidates.append(Path(v))
    # System installs first (apt's /usr/share/kicad on Linux, the app bundle on
    # macOS) — the package manager keeps those current; per-user copies last.
    for base in (
        Path("/usr/share/kicad"),
        Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport"),
        Path.home() / ".local/share/kicad",
    ):
        candidates.append(base / "symbols")
        if base.is_dir():
            for sub in sorted(base.glob("*/symbols"), reverse=True):
                candidates.append(sub)
    for c in candidates:
        if c.is_dir() and any(c.glob("*.kicad_sym")):
            return c
    return None


def normalize(name: str) -> str:
    return _NON_ALNUM.sub("", name.upper())


def _cache_path(root: Path) -> Path:
    cache_base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "kkh-import"
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return cache_base / f"symbol-index-{digest}.json"


def _lib_states(root: Path) -> dict[str, list[float]]:
    states = {}
    for lib in sorted(root.glob("*.kicad_sym")):
        st = lib.stat()
        states[lib.name] = [st.st_mtime, st.st_size]
    return states


def _scan_lib(lib: Path) -> list[str]:
    names = _SYMBOL_NAME.findall(lib.read_text(errors="replace"))
    return [n for n in names if not _UNIT_SUFFIX.search(n)]


def load_index(root: Path) -> dict[str, list[str]]:
    """normalized name -> ["Lib:Name", ...] over every standard library."""
    cache_file = _cache_path(root)
    states = _lib_states(root)
    try:
        cached = json.loads(cache_file.read_text())
        if cached.get("files") == states:
            return cached["index"]
    except (OSError, ValueError, KeyError):
        pass

    index: dict[str, list[str]] = {}
    for lib_name in states:
        lib_stem = lib_name.removesuffix(".kicad_sym")
        for symbol in _scan_lib(root / lib_name):
            index.setdefault(normalize(symbol), []).append(f"{lib_stem}:{symbol}")

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps({"files": states, "index": index}))
    tmp.replace(cache_file)
    return index


def match_mpn(mpn: str, index: dict[str, list[str]]) -> list[SymbolMatch]:
    """Standard symbols whose name matches the MPN, best first (exact, then
    longest shared prefix). Prefix matching needs MIN_OVERLAP normalized chars
    so short names like 'S8050' can't claim everything."""
    target = normalize(mpn)
    if not target:
        return []
    matches: list[SymbolMatch] = []
    for norm, refs in index.items():
        if len(norm) < MIN_OVERLAP and norm != target:
            continue
        if norm == target:
            overlap, exact = len(norm), True
        elif target.startswith(norm) or (len(target) >= MIN_OVERLAP and norm.startswith(target)):
            overlap, exact = min(len(norm), len(target)), False
        else:
            continue
        matches.extend(SymbolMatch(ref, exact, overlap) for ref in refs)
    matches.sort(key=lambda m: (not m.exact, -m.overlap, m.ref))
    return matches


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Check MPNs against KiCad's standard symbol libraries.")
    ap.add_argument("mpns", nargs="+", help="manufacturer part numbers, e.g. AMS1117-3.3")
    ap.add_argument("--root", type=Path, help="override symbol library root")
    args = ap.parse_args()

    root = find_symbol_root(args.root)
    if not root:
        raise SystemExit("Could not locate KiCad standard symbol libraries.")
    index = load_index(root)
    for mpn in args.mpns:
        matches = match_mpn(mpn, index)
        if not matches:
            print(f"{mpn}: no standard symbol")
            continue
        for m in matches[:4]:
            kind = "exact" if m.exact else f"prefix/{m.overlap}"
            print(f"{mpn}: {m.ref}  ({kind})")


if __name__ == "__main__":
    main()
