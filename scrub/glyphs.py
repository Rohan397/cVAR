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


UNICODE = Glyphs(
    ramp="░▒▓█", unchanged="·", absent=" ", deleted="×",
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


def use_ascii() -> None:
    global _current
    _current = ASCII
