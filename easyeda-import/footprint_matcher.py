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
    # EasyEDA package families whose KiCad names share no substring.
    "CRYSTAL-SMD": ["Crystal_SMD"],
    "OSC-SMD": ["Oscillator_SMD"],
    "USB-C-SMD": ["USB_C_Receptacle"],
}

# TI-style exposed-pad SOICs: EasyEDA calls them SOPOWERPAD-<n> (or marks the
# name with an -EP flag); KiCad's equivalents are SOIC-<n>-1EP_* and the
# Texas_*-PDSO-G<n>_EP* series. HSOP-<n>-1EP covers the ST/NXP naming.
_POWERPAD = re.compile(r"^SO(?:P(?:OWERPAD)?|IC)?-?(?P<n>\d+)$", re.IGNORECASE)

_DIM_SUFFIX = re.compile(r"^(?P<token>.*?)_(?:\d+P-)?[LWPH]\d", re.IGNORECASE)
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
    """`SOT-23-5_L3.0-W1.6-...` -> `SOT-23-5`, `CRYSTAL-SMD_4P-L3.2-...` ->
    `CRYSTAL-SMD`. Without a dimension suffix, fall back to the first
    `_`-segment (`USB-C-SMD_TYPE-C-16PIN-...` -> `USB-C-SMD`); names with no
    `_` at all pass through intact."""
    m = _DIM_SUFFIX.match(footprint_name)
    if m:
        return m.group("token")
    return footprint_name.split("_", 1)[0]


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
    # System installs first (apt's /usr/share/kicad on Linux, the app bundle on
    # macOS) — the package manager keeps those current; per-user copies last.
    for base in (
        Path("/usr/share/kicad"),
        Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport"),
        Path.home() / ".local/share/kicad",
    ):
        candidates.append(base / "footprints")
        if base.is_dir():
            for sub in sorted(base.glob("*/footprints"), reverse=True):
                candidates.append(sub)
    for c in candidates:
        if c.is_dir() and any(c.glob("*.pretty")):
            return c
    return None


def _search_tokens(token: str, comp_type: str | None, full_name: str = "") -> tuple[list[str], str | None]:
    """Return (name tokens to match, restrict-to-lib or None)."""
    passive = _PASSIVE_CODE.match(token)
    if passive:
        code = passive.group("code")
        t = (comp_type or passive.group("type") or "").upper()[:1]
        lib = _TYPE_LIB.get(t)
        return [code], lib
    tokens = [token, *ALIASES.get(token, [])]
    powerpad = _POWERPAD.match(token)
    if powerpad and ("POWERPAD" in token.upper() or "-EP" in full_name.upper()):
        n = powerpad.group("n")
        tokens += [f"SOIC-{n}-1EP", f"HSOP-{n}-1EP", f"PDSO-G{n}"]
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
    tokens, restrict_lib = _search_tokens(token, comp_type, generated_mod.stem)

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
