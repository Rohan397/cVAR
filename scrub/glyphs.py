"""Two character sets: one that reads well, one that renders anywhere.

The grid leans on block-drawing characters, and a terminal font that lacks any
of them prints "?" instead — which is worse than plain ASCII, because the row
still looks like data. Rather than guess at font coverage (a terminal cannot be
asked what glyphs it has), the ASCII set is available on request and every
glyph is referenced through here so the two stay in step.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Glyphs:
    ramp: str          # four steps, lightest first
    unchanged: str     # file exists here, untouched
    absent: str        # file does not exist at this commit
    deleted: str
    tick: str          # ruler major
    rule: str          # ruler minor
    caret: str         # playhead marker
    divider: str
    ellipsis: str
    dash: str          # line-range separator, "L1-23"
    left: str
    right: str
    up: str
    down: str
    enter: str


# The ramp is ASCII even in the "unicode" set, and that is deliberate.
#
# It is the one part of the grid that has actually failed in the wild: the
# shade blocks U+2591-2593 were missing from a font, and so were the eighth
# blocks U+2582-2586 -- on a terminal that draws U+2588, U+00B7, U+253C and
# U+25BC without complaint. Coverage of this Unicode range is patchy in a way
# the rest of the grid's characters are not, and a cell that renders as "?"
# still looks like data, which is worse than one that looks plain.
#
# Colour carries the same four steps, so nothing is lost but texture.
UNICODE = Glyphs(
    ramp=".:*#", unchanged="·", absent=" ", deleted="×",
    tick="┼", rule="─", caret="▼", divider="│", ellipsis="…", dash="–",
    # Spelled out rather than U+23CE: the return symbol is the glyph most often
    # missing from a monospace font, and a help bar is the worst place for a "?".
    left="←", right="→", up="↑", down="↓", enter="enter",
)

ASCII = Glyphs(
    ramp=".:*#", unchanged="-", absent=" ", deleted="X",
    tick="+", rule="-", caret="v", divider="|", ellipsis="~", dash="-",
    left="<-", right="->", up="^", down="v", enter="enter",
)


_current = ASCII if os.environ.get("SCRUB_ASCII") else UNICODE


def active() -> Glyphs:
    """The set in force. Call at draw time, never bind at import.

    A module-level `G = active()` is captured before argparse has run, so
    --ascii silently does nothing and only the environment variable works.
    """
    return _current


def test_card() -> str:
    """Every character the grid draws, so a font gap can be seen not guessed.

    A terminal cannot be asked which glyphs its font has, so the only reliable
    check is to print them and look.
    """
    rows = [
        ("churn ramp", UNICODE.ramp, "lightest to heaviest change"),
        ("untouched", UNICODE.unchanged, "file exists here, not touched"),
        ("deleted", UNICODE.deleted, "file removed at this commit"),
        ("ruler", UNICODE.tick + UNICODE.rule, "commit ruler"),
        ("caret", UNICODE.caret, "playhead position"),
        ("divider", UNICODE.divider, "commit row"),
        ("ellipsis", UNICODE.ellipsis, "truncated file names"),
        ("arrows", f"{UNICODE.left}{UNICODE.right}{UNICODE.up}{UNICODE.down}", "help bar"),
    ]
    lines = [
        "Any of these showing as '?' is missing from your terminal font.",
        "Run with --ascii (or export SCRUB_ASCII=1) to draw without them.",
        "",
    ]
    for name, chars in ((n, c) for n, c, _ in rows):
        points = " ".join(f"U+{ord(c):04X}" for c in chars)
        lines.append(f"  {name:<12} {chars:<6}  {points}")
    lines += ["", "  ASCII fallback:", f"    ramp {ASCII.ramp}   untouched {ASCII.unchanged}"
              f"   deleted {ASCII.deleted}   caret {ASCII.caret}"]
    return "\n".join(lines)


def use_ascii() -> None:
    global _current
    _current = ASCII
