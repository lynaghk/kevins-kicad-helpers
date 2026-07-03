#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "Pillow>=10",
# ]
# ///
"""Interactive terminal chooser for KiCad footprints.

Shows one candidate at a time, filling most of the screen, with a short scrolling
list of the alternatives underneath (<=4 rows). Every candidate is rendered at the
same origin (footprint 0,0 at pane centre) and the same mm->pixel scale, so moving
the selection with the arrow keys flips between them in the same physical place --
the fastest way to spot a difference in pad geometry.

Rendering is half-blocks (works in any 24-bit-colour terminal, including tmux).
See kicad_mod_render.py for the drawing itself.

Programmatic entry point:
    choose(candidates) -> Candidate | None   (None if the user cancels)
"""

from __future__ import annotations

import os
import select
import shutil
import sys
import termios
import tty
from dataclasses import dataclass
from pathlib import Path

import terminal_image
from kicad_mod_render import (
    Footprint,
    bbox_extent,
    load_footprint,
    render_footprint,
    suggest_rotation,
    to_half_blocks,
)

LIST_ROWS = 4  # max visible rows in the alternatives list


@dataclass
class Candidate:
    label: str  # what shows in the list, e.g. "Package_TO_SOT_SMD:SOT-23-5"
    path: Path  # .kicad_mod to render
    ref: str  # value returned to the caller when chosen (e.g. "lib:fp" or "")
    note: str = ""  # short right-aligned annotation, e.g. "5 pads" / "generated"


# --------------------------------------------------------------------------- #
# terminal helpers
# --------------------------------------------------------------------------- #
CLEAR = "\x1b[2J\x1b[H"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
HOME = "\x1b[H"
EL = "\x1b[K"  # erase from cursor to end of line
ED = "\x1b[J"  # erase from cursor to end of screen


# codepoint / byte -> action
_ACTIONS = {
    32: "peek",  # space
    13: "enter",
    10: "enter",
    27: "cancel",  # Esc
    3: "abort",  # Ctrl-C (legacy byte)
    ord("q"): "cancel",
    ord("k"): "up",
    ord("j"): "down",
    ord("f"): "flip",
    ord("r"): "rotate",
    ord("h"): "help",
    ord("?"): "help",
}
_EVENTS = {1: "press", 2: "repeat", 3: "release"}
_MOD_CTRL = 0b100  # kitty modifier bitmask: shift=1, alt=2, ctrl=4, ...


def _interpret_csi(params: str, final: str) -> tuple[str, str]:
    """Map a parsed CSI sequence to (action, event). Handles legacy arrows
    (final A/B, no params) and kitty CSI-u events (final 'u', params
    'codepoint:shifted;mods:event'). The shifted codepoint, when present, is the
    key actually typed — that is how '?' arrives, since the base key is '/'.
    Ctrl-C (c + ctrl, or codepoint 3) becomes 'abort'."""
    groups = params.split(";") if params else []
    event, mods = 1, 1
    if len(groups) >= 2:
        mod_part = groups[1].split(":")
        if mod_part[0].isdigit():
            mods = int(mod_part[0])
        if len(mod_part) >= 2 and mod_part[1].isdigit():
            event = int(mod_part[1])
    ev = _EVENTS.get(event, "press")
    ctrl = bool((mods - 1) & _MOD_CTRL)
    if final == "u":
        head = groups[0].split(":") if groups else [""]
        cp = int(head[0]) if head[0].isdigit() else -1
        shifted = int(head[1]) if len(head) > 1 and head[1].isdigit() else -1
        if 3 in (cp, shifted) or (cp in (ord("c"), ord("C")) and ctrl):
            return ("abort", ev)
        return (_ACTIONS.get(shifted if shifted != -1 else cp, "other"), ev)
    if final in ("A", "B", "C", "D"):
        return ({"A": "up", "B": "down"}.get(final, "other"), ev)
    return ("other", ev)


def _read_event(timeout: float | None = None) -> tuple[str, str]:
    """Return (action, event). event is 'press'/'repeat'/'release' (kitty keyboard
    protocol) or 'press' for legacy keys. Reads the raw fd with os.read (unbuffered)
    so a multi-byte escape isn't split by stream buffering."""
    fd = sys.stdin.fileno()
    if timeout is not None and not select.select([fd], [], [], timeout)[0]:
        return ("timeout", "")
    try:
        ch = os.read(fd, 1)
    except OSError:
        return ("cancel", "press")
    if not ch:
        return ("cancel", "press")

    if ch == b"\x1b":
        if not select.select([fd], [], [], 0.05)[0]:
            return ("cancel", "press")  # lone Esc
        b2 = os.read(fd, 1)
        if b2 not in (b"[", b"O"):
            return ("other", "press")
        params, final = b"", b""
        while select.select([fd], [], [], 0.2)[0]:
            c = os.read(fd, 1)
            if b"\x40" <= c <= b"\x7e":  # final byte of the CSI sequence
                final = c
                break
            params += c
        return _interpret_csi(params.decode("ascii", "ignore"), final.decode("ascii", "ignore"))

    return (_ACTIONS.get(ch[0], "other"), "press")


# --------------------------------------------------------------------------- #
# frame construction
# --------------------------------------------------------------------------- #
def _shared_scale(fps: list[Footprint], cols: int, preview_px_h: int) -> float:
    """One mm->px scale that fits the largest candidate, used for all of them."""
    max_extent = max((bbox_extent(fp) for fp in fps), default=1.0)
    return 0.9 * min(cols, preview_px_h) / (2 * max_extent)


def _list_window(selected: int, total: int, rows: int) -> range:
    """Which candidate indices are visible, scrolled to keep selection in view."""
    if total <= rows:
        return range(total)
    start = max(0, min(selected - rows // 2, total - rows))
    return range(start, start + rows)


def _render_list(candidates: list[Candidate], selected: int, cols: int) -> list[str]:
    window = _list_window(selected, len(candidates), LIST_ROWS)
    lines = []
    for i in window:
        c = candidates[i]
        marker = "\x1b[7m>" if i == selected else " "
        reset = "\x1b[0m"
        note = f"  {c.note}" if c.note else ""
        text = f"{marker} {c.label}{note}"
        # pad/truncate to width
        visible_len = len(f"  {c.label}{note}")
        if visible_len > cols:
            text = text[: cols + len(marker) - 1]
        else:
            text = text + " " * (cols - visible_len)
        lines.append(text + reset)
    # pad to a stable LIST_ROWS height
    while len(lines) < min(LIST_ROWS, len(candidates)):
        lines.append(" " * cols)
    return lines


# kitty renders the PNG scaled to fit a cell box; pick a pixel canvas whose aspect
# (~1:2 per cell) matches so it isn't distorted. The canvas is already hi-res, so
# it needs no supersampling (KITTY_SS=1) and is capped so a huge terminal doesn't
# build a needlessly large image every frame.
CELL_W, CELL_H = 8, 16
KITTY_MAX_W = 1200
KITTY_SS = 1


def _build_frame(
    candidates: list[Candidate],
    fps: list[Footprint],
    selected: int,
    shown: int,
    side: str = "front",
    rotation: int = 0,
    reference_index: int = 0,
    proto: str | None = None,
    peek: bool = False,
) -> str:
    """`shown` is the footprint drawn in the preview (may be the peeked reference);
    `selected` is what stays highlighted in the list."""
    size = shutil.get_terminal_size((90, 40))
    cols, rows = size.columns, size.lines

    n_list = min(LIST_ROWS, len(candidates))
    header_rows = 2  # title + the counter/help line
    preview_rows = max(4, rows - n_list - header_rows - 1)

    cur = candidates[shown]
    side_tag = "back" if side == "back" else "front"
    # The reference (EasyEDA) candidate has no rotation offset of its own.
    rot_tag = "" if shown == reference_index else f"   \x1b[2mFT rot: {rotation:+d}°\x1b[0m"
    peek_tag = "   \x1b[7m PEEK: EasyEDA \x1b[0m" if peek else ""
    title = f"\x1b[1m{cur.label}\x1b[0m  \x1b[2m({side_tag})\x1b[0m{rot_tag}{peek_tag}"
    help_line = (
        f"\x1b[2m[{selected + 1}/{len(candidates)}]  "
        f"↑/↓ compare   f flip   r rotate   space peek   Enter import   q skip part   ? help\x1b[0m"
    )
    list_lines = _render_list(candidates, selected, cols)

    if proto == "kitty":
        img_w, img_h = cols * CELL_W, preview_rows * CELL_H
        if img_w > KITTY_MAX_W:  # cap, preserving aspect
            img_w, img_h = KITTY_MAX_W, int(img_h * KITTY_MAX_W / img_w)
        scale = _shared_scale(fps, img_w, img_h)
        img = render_footprint(
            fps[shown], scale, img_w, img_h, side=side, rotation=rotation, supersample=KITTY_SS
        )
        out = [HOME, title, EL, "\r\n", help_line, EL, "\r\n"]
        out.append("\x1b[3;1H")  # top-left of the preview area
        out.append(terminal_image.clear())  # remove the previous frame's image
        out.append(terminal_image.kitty_image(img, cols, preview_rows))
        out.append(f"\x1b[{3 + preview_rows};1H")  # below the image, for the list
        out.extend(ln + EL + "\r\n" for ln in list_lines)
        out.append(ED)
        return "".join(out)

    # Half-block fallback: repaint from the top, erasing each line's old tail (EL)
    # so a shorter title doesn't leave leftovers, and clearing rows below (ED).
    scale = _shared_scale(fps, cols, preview_rows * 2)
    img = render_footprint(fps[shown], scale, cols, preview_rows * 2, side=side, rotation=rotation)
    preview = to_half_blocks(img)
    frame_rows = [title, help_line, *preview.split("\n"), *list_lines]
    return HOME + (EL + "\n").join(frame_rows) + EL + ED


HELP_LINES = [
    "Key commands",
    "",
    "  ↑ / k      previous candidate",
    "  ↓ / j      next candidate",
    "  space      peek the EasyEDA footprint (hold)",
    "  f          flip front / back",
    "  r          rotate 90° (sets FT Rotation Offset)",
    "  Enter      import with the highlighted footprint",
    "  q / Esc    skip this part (import nothing)",
    "  ? / h      this help",
    "",
    "  press any key to close",
]


def _show_help_overlay() -> None:
    """Draw a centred modal listing the key commands; wait for any key press."""
    size = shutil.get_terminal_size((90, 40))
    cols, rows = size.columns, size.lines
    inner = max(len(l) for l in HELP_LINES)
    box_w = inner + 4
    top = max(0, (rows - (len(HELP_LINES) + 2)) // 2)
    left = max(0, (cols - box_w) // 2)
    pad = " " * left

    out = [CLEAR]
    out.append("\n" * top)
    out.append(f"{pad}╭{'─' * (box_w - 2)}╮\n")
    for line in HELP_LINES:
        out.append(f"{pad}│ {line.ljust(inner)} │\n")
    out.append(f"{pad}╰{'─' * (box_w - 2)}╯\n")
    sys.stdout.write("".join(out))
    sys.stdout.flush()
    while True:  # wait for a real key press to dismiss
        action, event = _read_event()
        if event == "release":
            continue
        if action == "abort":
            raise KeyboardInterrupt
        break


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def choose(
    candidates: list[Candidate], *, preselect: int = 0, reference_index: int = 0
) -> tuple[Candidate, int] | None:
    """Interactively pick one candidate. Returns (candidate, rotation_offset_deg)
    or None if the user cancels. Each non-reference candidate starts at the auto-
    suggested rotation that aligns it to the reference (EasyEDA) footprint; the
    user fine-tunes with 'r'. The rotation is meaningful only for a substituted
    footprint (it becomes its FT Rotation Offset)."""
    if not candidates:
        return None
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("footprint chooser needs an interactive terminal")

    fps = [load_footprint(c.path) for c in candidates]
    selected = max(0, min(preselect, len(candidates) - 1))
    side = "front"
    ref = fps[reference_index]
    rotations = [0 if i == reference_index else suggest_rotation(ref, fp) for i, fp in enumerate(fps)]
    proto = terminal_image.protocol()
    kbd = proto == "kitty"  # kitty keyboard protocol gives key-release events
    peek = False  # while true, show the EasyEDA reference instead of the selection

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        if kbd:
            sys.stdout.write(terminal_image.keyboard_enable())
        sys.stdout.write(HIDE_CURSOR + CLEAR)
        while True:
            shown = reference_index if peek else selected
            sys.stdout.write(
                _build_frame(
                    candidates, fps, selected, shown, side, rotations[shown], reference_index, proto, peek
                )
            )
            sys.stdout.flush()
            action, event = _read_event()

            if action == "abort":  # Ctrl-C: exit the whole program
                raise KeyboardInterrupt
            if action == "peek":
                # hold-to-peek where releases are reported (kitty); toggle otherwise
                peek = (event != "release") if kbd else (not peek if event == "press" else peek)
                continue
            if event == "release":
                continue  # ignore key-up for everything else

            if action == "up":
                selected = (selected - 1) % len(candidates)
            elif action == "down":
                selected = (selected + 1) % len(candidates)
            elif action == "flip":
                side = "back" if side == "front" else "front"
            elif action == "rotate" and selected != reference_index:
                rotations[selected] = (rotations[selected] + 90) % 360
            elif action == "help":
                if proto:
                    sys.stdout.write(terminal_image.clear())  # graphics sit above text
                _show_help_overlay()
                sys.stdout.write(CLEAR)  # force a full repaint after the modal
            elif action == "enter":
                return candidates[selected], rotations[selected]
            elif action == "cancel":
                return None
    finally:
        if kbd:
            sys.stdout.write(terminal_image.keyboard_disable())
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if proto:
            sys.stdout.write(terminal_image.clear())
        sys.stdout.write(SHOW_CURSOR + CLEAR)
        sys.stdout.flush()


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Demo: choose among .kicad_mod files.")
    ap.add_argument("kicad_mods", nargs="+", type=Path)
    args = ap.parse_args()

    candidates = []
    for p in args.kicad_mods:
        fp = load_footprint(p)
        candidates.append(Candidate(label=p.stem, path=p, ref=p.stem, note=f"{len(fp.pads)} pads"))

    result = choose(candidates)
    if result is None:
        print("cancelled", file=sys.stderr)
        raise SystemExit(1)
    chosen, rotation = result
    print(f"{chosen.ref}\tFT Rotation Offset={rotation}")


if __name__ == "__main__":
    main()
