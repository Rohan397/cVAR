"""Zooming into a track: which regions of a file each commit touched.

A track row answers "did this commit touch this file". For a file rewritten
across a dozen commits that is not enough to review from — you want to know
*where* in the file, and to be able to step through those places one at a time.

The hard part is that line numbers move. A hunk reported at line 40 in an early
commit may live at line 90 by the tip, so a naive grid would mark the wrong
region. Every commit's hunks are therefore projected forward through the diffs
that follow, into the file's coordinate space at the playhead, and only then
bucketed into rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import gitio

# @@ -old_start,old_count +new_start,new_count @@ ; either count may be omitted,
# which means exactly one line.
HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Hunk:
    """One changed region, in the coordinate space of a single commit."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int

    @property
    def new_end(self) -> int:
        return self.new_start + max(self.new_count, 1)

    @property
    def weight(self) -> int:
        return self.old_count + self.new_count


@dataclass(frozen=True)
class Region:
    """A line range in the playhead's coordinate space, and who touched it."""

    start: int
    end: int
    weight: int


@dataclass
class Chunk:
    """A row in the zoomed view: one region of the file, across all commits."""

    start: int
    end: int
    weight_by_commit: dict[int, int]

    @property
    def label(self) -> str:
        return f"L{self.start}–{self.end}"

    @property
    def weight(self) -> int:
        return sum(self.weight_by_commit.values())


def parse_hunks(diff: str) -> list[Hunk]:
    found = []
    for line in diff.splitlines():
        match = HUNK_HEADER.match(line)
        if match:
            old_start, old_count, new_start, new_count = match.groups()
            found.append(
                Hunk(
                    old_start=int(old_start),
                    old_count=int(old_count) if old_count is not None else 1,
                    new_start=int(new_start),
                    new_count=int(new_count) if new_count is not None else 1,
                )
            )
    return found


def project(line: int, later: list[Hunk]) -> int:
    """Carry a line number forward through one later commit's hunks.

    Lines above every hunk keep their number; lines below shift by the net
    growth of the hunks above them. A line *inside* a rewritten region has no
    single successor, so it collapses to the start of that region — the row it
    lands in is still the right one, which is all the grid needs.
    """
    offset = 0
    for hunk in later:
        if hunk.old_start + hunk.old_count <= line:
            offset += hunk.new_count - hunk.old_count
        elif hunk.old_start <= line:
            return hunk.new_start
    return line + offset


def _merge(regions: list[Region], gap: int) -> list[tuple[int, int]]:
    """Collapse overlapping and near-touching regions into chunk bounds.

    Two edits a line apart are one place to look, not two, so anything closer
    than `gap` is joined rather than given its own row.
    """
    bounds: list[tuple[int, int]] = []
    for region in sorted(regions, key=lambda r: r.start):
        if bounds and region.start - bounds[-1][1] <= gap:
            bounds[-1] = (bounds[-1][0], max(bounds[-1][1], region.end))
        else:
            bounds.append((region.start, region.end))
    return bounds


def build(timeline, track_id: str, playhead: int, gap: int = 3) -> list[Chunk]:
    """Regions of the selected file that changed at or before the playhead.

    Regions never touched get no row: the point of zooming in is to skip the
    parts of the file nothing happened to.
    """
    track = timeline.tracks[track_id]
    touched = sorted(i for i in track.clips if i <= playhead)
    if not touched:
        return []

    # Hunks per commit, in that commit's own coordinates.
    per_commit: dict[int, list[Hunk]] = {}
    for index in touched:
        path = track.path_at(index) or track.label
        diff = gitio.run_text(
            timeline.repo,
            # -U0: no context lines. Context inflates every hunk by six
            # lines, which merges edits that are actually far apart and
            # collapses the whole file into one region.
            "diff-tree", "-p", "-U0", "-M", "--root", "--no-commit-id",
            timeline.sha_at(index), "--", path,
        )
        per_commit[index] = parse_hunks(diff)

    # Project each commit's hunks forward through every later commit that also
    # touched this file, so all of them land in playhead coordinates.
    regions: dict[int, list[Region]] = {}
    for position, index in enumerate(touched):
        later = [per_commit[j] for j in touched[position + 1 :]]
        mapped = []
        for hunk in per_commit[index]:
            start, end = hunk.new_start, hunk.new_end
            for hunks in later:
                start, end = project(start, hunks), project(end, hunks)
            mapped.append(Region(start, max(end, start + 1), hunk.weight))
        regions[index] = mapped

    # A file's creation touches every line, so letting it set boundaries
    # collapses the whole file into one row and defeats the zoom. It still
    # contributes weight to whatever rows the later edits define.
    shaping = [
        region
        for index, group in regions.items()
        if track.clips[index].kind != "A"
        for region in group
    ]
    # Unless creation is all there is — then the whole file is the one row.
    bounds = _merge(shaping or [r for g in regions.values() for r in g], gap)
    chunks = [Chunk(start, end, {}) for start, end in bounds]

    for index, mapped in regions.items():
        for region in mapped:
            for chunk in chunks:
                if region.start < chunk.end and chunk.start < region.end:
                    chunk.weight_by_commit[index] = (
                        chunk.weight_by_commit.get(index, 0) + region.weight
                    )
    return chunks
