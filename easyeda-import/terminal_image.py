"""Emit images to the terminal at true pixel resolution.

Half-blocks are capped by the character grid (~1x2 px per cell); for crisp pad
numbers we hand the terminal a real PNG. Currently supports the kitty graphics
protocol (kitty, Ghostty, WezTerm). Detection is conservative and everything
degrades to half-blocks when unsupported (see footprint_chooser_tui).

Set KKH_TERMINAL_IMAGE=0 to force the half-block fallback.
"""

from __future__ import annotations

import base64
import io
import os

from PIL import Image

IMAGE_ID = 1  # single reused id; we delete+redraw each frame
_CHUNK = 4096


def protocol() -> str | None:
    """Return 'kitty' if the terminal understands the kitty graphics protocol."""
    if os.environ.get("KKH_TERMINAL_IMAGE") == "0":
        return None
    term = os.environ.get("TERM", "")
    term_program = os.environ.get("TERM_PROGRAM", "")
    if (
        "kitty" in term
        or "ghostty" in term
        or os.environ.get("KITTY_WINDOW_ID")
        or os.environ.get("GHOSTTY_RESOURCES_DIR")
        or term_program in ("ghostty", "WezTerm")
    ):
        return "kitty"
    return None


def clear(image_id: int = IMAGE_ID) -> str:
    """Escape that deletes the image (and its placements) with the given id."""
    return f"\x1b_Ga=d,d=i,i={image_id},q=2\x1b\\"


# Kitty keyboard protocol: push flags 1|2|4|8 = disambiguate | report-event-types |
# report-alternate-keys | report-all-keys-as-escape-codes. The event types
# (press/repeat/release) are what let the chooser peek-while-space-is-held;
# report-all-keys makes even space/enter arrive as CSI-u escapes so their release
# events are reported; alternate keys (flag 4, NOT 16 — 16 is associated-text)
# carry the shifted codepoint as 'base:shifted' in the first CSI group ('?' would
# otherwise arrive only as its base key '/').
def keyboard_enable() -> str:
    return "\x1b[>15u"


def keyboard_disable() -> str:
    return "\x1b[<u"


def kitty_image(img: Image.Image, cols: int, rows: int, image_id: int = IMAGE_ID) -> str:
    """Escape sequence that transmits `img` (PNG) and displays it scaled to fit a
    cols x rows character box at the current cursor position. q=2 suppresses the
    terminal's replies so they don't land in our key-input stream."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    chunks = [data[i : i + _CHUNK] for i in range(0, len(data), _CHUNK)] or [""]

    parts = []
    for idx, chunk in enumerate(chunks):
        more = 0 if idx == len(chunks) - 1 else 1
        if idx == 0:
            ctrl = f"a=T,f=100,i={image_id},c={cols},r={rows},q=2,m={more}"
        else:
            ctrl = f"m={more}"
        parts.append(f"\x1b_G{ctrl};{chunk}\x1b\\")
    return "".join(parts)
