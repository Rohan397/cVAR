"""ASCII rendering of the track grid.

This is deliberately not the real UI — it exists so the density of a branch can
be eyeballed before committing to a TUI framework. If a branch does not read at
a glance here, no amount of widget polish will save it.
"""

from __future__ import annotations

import math

from . import glyphs
from .model import Timeline, Track

# Magnitude ramp for changed cells. Index 0 is the lightest touch.
RAMP_STEPS = 4  # file exists at this commit but was not touched

DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"


def _bucket(weight: int, ceiling: int) -> str:
    """Map a churn weight onto the ramp, log-scaled.

    Linear scaling makes one 800-line vendored file flatten everything else to
    the same shade, which is precisely the branch shape worth seeing.
    """
    if weight <= 0:
        return glyphs.active().ramp[0]
    scaled = math.log1p(weight) / math.log1p(max(ceiling, 1))
    ramp = glyphs.active().ramp
    return ramp[min(len(ramp) - 1, int(scaled * len(ramp)))]


def track_row(track: Track, span: int, ceiling: int) -> str:
    G = glyphs.active()
    cells = []
    for index in range(span):
        clip = track.clips.get(index)
        if clip is not None:
            cells.append(G.deleted if clip.kind == "D" else _bucket(clip.weight, ceiling))
        else:
            state = track.state_at(index)
            cells.append(G.unchanged if state == "live" else G.absent)
    return "".join(cells)


def grid(timeline: Timeline, playhead: int = 0, width: int = 34, color: bool = True) -> str:
    """The full track view: files down the side, commits across."""
    G = glyphs.active()
    span = len(timeline)
    if span == 0:
        return "(no commits in range)"

    tracks = timeline.track_order()
    ceiling = max((c.weight for t in tracks for c in t.clips.values()), default=1)

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if color else text

    lines: list[str] = []

    # Playhead marker sits above the grid, aligned to the commit columns.
    caret = " " * width + " " + " " * playhead + G.caret
    lines.append(paint(caret, BOLD))

    for track in tracks:
        label = track.label
        if len(label) > width:
            label = G.ellipsis + label[-(width - 1) :]
        row = track_row(track, span, ceiling)
        marked = row[:playhead] + paint(row[playhead : playhead + 1], BOLD) + row[playhead + 1 :]
        lines.append(f"{label:<{width}} {marked}")

    # Commit ruler: a tick every five commits so position stays readable.
    ruler = "".join(G.tick if i % 5 == 0 else G.rule for i in range(span))
    lines.append(paint(" " * width + " " + ruler, DIM))

    head = timeline.commits[playhead]
    caption = f"{head.short}  {head.subject}"
    lines.append(paint(f"{'':<{width}} {caption}", DIM))
    return "\n".join(lines)


def summary(timeline: Timeline) -> str:
    """A few lines of orientation before the grid."""
    tracks = timeline.track_order()
    churn = sum(t.weight for t in tracks)
    renamed = sum(1 for t in tracks if len(t.renames) > 1)
    lines = [
        f"{len(timeline)} commits · {len(tracks)} tracks · {churn} lines changed"
        + (f" · {renamed} renamed" if renamed else ""),
    ]
    hottest = sorted(tracks, key=lambda t: -t.weight)[:3]
    if hottest:
        detail = ", ".join(f"{t.label} ({t.weight})" for t in hottest)
        lines.append(f"hottest: {detail}")
    return "\n".join(lines)
