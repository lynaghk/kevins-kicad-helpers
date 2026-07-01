#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "Pillow>=10",
# ]
# ///
"""Find KiCad standard-library footprints that plausibly match a generated one.

The generated footprint (from easyeda2kicad) is named `<PACKAGE>_<dimensions>`,
e.g. `SOT-23-5_L3.0-W1.6-P0.95-LS2.8-BL`. We take the package token (`SOT-23-5`)
and the front-copper pad count, then scan the installed standard libraries for
footprints whose name contains that token and whose pad count agrees. Passive
chip sizes (`0603`) additionally need the component type (R/C/L) to disambiguate
`R_0603*` from `C_0603*`.

Precision is deliberately loose; the interactive chooser is what turns a short
candidate list into a decision. Returns candidates ranked best-first.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from kicad_mod_render import load_footprint, pad_mismatch, suggest_rotation

# Package aliases: easyeda token -> extra tokens to also search for.
ALIASES: dict[str, list[str]] = {
    "SO-8": ["SOIC-8"],
    "SO-14": ["SOIC-14"],
    "SO-16": ["SOIC-16"],
    "SOP-8": ["SOIC-8"],
}

_DIM_SUFFIX = re.compile(r"^(?P<token>.*?)_[LWPH]\d", re.IGNORECASE)
_PASSIVE_CODE = re.compile(r"^(?P<type>[RCLrcl]?)(?P<code>\d{4})$")
_TYPE_LIB = {"R": "Resistor_SMD", "C": "Capacitor_SMD", "L": "Inductor_SMD"}


@dataclass
class Match:
    lib: str
    footprint: str
    path: Path
    pad_count: int
    mismatch: int = 0  # non-overlapping pad area vs the generated footprint (lower = better)
    rotation: int = 0  # suggested rotation (deg CCW) used when computing the overlap

    @property
    def ref(self) -> str:
        return f"{self.lib}:{self.footprint}"


def package_token(footprint_name: str) -> str:
    """`SOT-23-5_L3.0-W1.6-...` -> `SOT-23-5`; leaves names without dims intact."""
    m = _DIM_SUFFIX.match(footprint_name)
    return m.group("token") if m else footprint_name


def find_footprint_root(override: Path | None = None) -> Path | None:
    """Locate the installed standard footprint libraries (dir of *.pretty)."""
    candidates: list[Path] = []
    if override:
        candidates.append(override)
    for var in (
        "KICAD10_FOOTPRINT_DIR",
        "KICAD9_FOOTPRINT_DIR",
        "KICAD8_FOOTPRINT_DIR",
        "KICAD_FOOTPRINT_DIR",
    ):
        v = os.environ.get(var)
        if v:
            candidates.append(Path(v))
    home = Path.home()
    for base in (
        home / ".local/share/kicad",
        Path("/usr/share/kicad"),
        Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport"),
    ):
        candidates.append(base / "footprints")
        if base.is_dir():
            for sub in sorted(base.glob("*/footprints"), reverse=True):
                candidates.append(sub)
    for c in candidates:
        if c.is_dir() and any(c.glob("*.pretty")):
            return c
    return None


def _search_tokens(token: str, comp_type: str | None) -> tuple[list[str], str | None]:
    """Return (name tokens to match, restrict-to-lib or None)."""
    passive = _PASSIVE_CODE.match(token)
    if passive:
        code = passive.group("code")
        t = (comp_type or passive.group("type") or "").upper()[:1]
        lib = _TYPE_LIB.get(t)
        return [code], lib
    tokens = [token, *ALIASES.get(token, [])]
    return tokens, None


def _name_matches(fp_name: str, tokens: list[str]) -> bool:
    low = fp_name.lower()
    return any(t.lower() in low for t in tokens)


def find_candidates(
    generated_mod: Path,
    root: Path,
    *,
    comp_type: str | None = None,
    limit: int = 40,
) -> list[Match]:
    """Standard footprints matching the generated one, ranked best-first by actual
    pad-area overlap: each candidate is rotated by its suggested offset, then its
    non-overlapping pad area vs the generated footprint is measured (see
    kicad_mod_render.pad_mismatch). Name matching + pad count only gate the set."""
    gen = load_footprint(generated_mod)
    token = package_token(generated_mod.stem)
    pad_count = len(gen.pads)
    tokens, restrict_lib = _search_tokens(token, comp_type)

    lib_dirs = sorted(root.glob("*.pretty"))
    if restrict_lib:
        lib_dirs = [d for d in lib_dirs if d.stem == restrict_lib] or lib_dirs

    matches: list[Match] = []
    for lib_dir in lib_dirs:
        lib = lib_dir.stem
        for mod in lib_dir.glob("*.kicad_mod"):
            if not _name_matches(mod.stem, tokens):
                continue
            cand = load_footprint(mod)
            if len(cand.pads) != pad_count:
                continue
            rot = suggest_rotation(gen, cand)
            mismatch = pad_mismatch(gen, cand, rot)
            matches.append(Match(lib, mod.stem, mod, len(cand.pads), mismatch, rot))

    # Best pad-area overlap first; footprint name only breaks ties for determinism.
    matches.sort(key=lambda m: (m.mismatch, m.footprint))
    return matches[:limit]


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="List standard-library candidates for a generated footprint.")
    ap.add_argument("kicad_mod", type=Path)
    ap.add_argument("--type", help="component type hint for passives (R/C/L)")
    ap.add_argument("--root", type=Path, help="override footprint library root")
    args = ap.parse_args()

    root = find_footprint_root(args.root)
    if not root:
        raise SystemExit("Could not locate KiCad standard footprint libraries.")
    token = package_token(args.kicad_mod.stem)
    cands = find_candidates(args.kicad_mod, root, comp_type=args.type)
    print(f"token={token!r}  root={root}")
    print(f"{len(cands)} candidate(s), best pad-overlap first:")
    for m in cands:
        print(f"  {m.ref}  (mismatch {m.mismatch}, rot {m.rotation:+d}°)")


if __name__ == "__main__":
    main()
