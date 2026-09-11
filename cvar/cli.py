"""Inspect a timeline from the shell.

    python -m cvar /path/to/repo
    python -m cvar . --range main..HEAD
    python -m cvar . --at 4 --track src/auth.py --pane state
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import doctor, glyphs, render, tui
from .tui import PANES
from .gitio import GitError
from .model import Timeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cvar", description="Scrub a commit timeline.")
    parser.add_argument("repo", nargs="?", default=".", type=Path)
    parser.add_argument("--range", dest="rev_range", help="rev range, e.g. main..HEAD")
    parser.add_argument("-n", "--limit", type=int, help="cap the number of commits")
    parser.add_argument("--at", type=int, default=None, help="playhead position (default: last)")
    parser.add_argument("--grid", action="store_true", help="print the grid once and exit")
    parser.add_argument("--editor", help="editor command for the handoff (default: autodetect)")
    parser.add_argument(
        "--nvim-server",
        help="socket of a listening nvim to drive over RPC (default: $NVIM when set)",
    )
    parser.add_argument("--track", help="show a pane for this track instead of the grid")
    parser.add_argument(
        "--pane",
        choices=("diff", "state", "cumulative"),
        default="diff",
        help="which pane to print for --track",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="explain which editor and transport a handoff would use, and why",
    )
    parser.add_argument(
        "--open",
        dest="default_pane",
        choices=PANES,
        default=os.environ.get("CVAR_PANE", "cumulative"),
        help=(
            "what enter opens (default: cumulative, or $CVAR_PANE). "
            "u/d/s still open each pane explicitly."
        ),
    )
    parser.add_argument(
        "--glyphs",
        action="store_true",
        help="print every character the grid uses, to find the ones your font lacks",
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="draw with plain ASCII, for fonts missing the block glyphs",
    )
    trees_group = parser.add_mutually_exclusive_group()
    trees_group.add_argument(
        "--trees",
        dest="trees",
        action="store_true",
        default=None,
        help="start on the worktree overview (default when the repo has several)",
    )
    trees_group.add_argument(
        "--no-trees",
        dest="trees",
        action="store_false",
        help="start on the file grid even when the repo has several worktrees",
    )
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)
    if args.ascii:
        glyphs.use_ascii()

    if args.glyphs:
        print(glyphs.test_card())
        return 0

    try:
        timeline = Timeline.load(args.repo, args.rev_range, args.limit)
    except GitError as exc:
        print(f"cvar: {exc}", file=sys.stderr)
        return 1

    with timeline:
        if args.doctor:
            print(doctor.report(timeline, args.editor, args.nvim_server))
            return 0

        if not len(timeline):
            print("cvar: no commits in range", file=sys.stderr)
            return 1

        playhead = len(timeline) - 1 if args.at is None else args.at
        playhead = max(0, min(playhead, len(timeline) - 1))

        if args.track:
            track_id = _resolve_track(timeline, args.track)
            if track_id is None:
                print(f"cvar: no track matching {args.track!r}", file=sys.stderr)
                return 1
            print(_pane(timeline, track_id, playhead, args.pane))
            return 0

        if args.grid or not sys.stdout.isatty():
            print(render.summary(timeline))
            print()
            print(render.grid(timeline, playhead, color=not args.no_color))
            return 0

        tui.launch(timeline, args.editor, args.nvim_server, args.default_pane, args.trees)
    return 0


def _resolve_track(timeline: Timeline, needle: str) -> str | None:
    if needle in timeline.tracks:
        return needle
    matches = [t.id for t in timeline.track_order() if needle in t.label or needle in t.id]
    return matches[0] if matches else None


def _pane(timeline: Timeline, track_id: str, playhead: int, pane: str) -> str:
    if pane == "state":
        blob = timeline.file_at(track_id, playhead)
        if blob is None:
            return "(file does not exist at this commit)"
        return blob.decode(errors="replace")
    if pane == "cumulative":
        return timeline.cumulative_diff(track_id, playhead) or "(no cumulative change)"
    return timeline.diff_at(track_id, playhead) or "(track unchanged at this commit)"


if __name__ == "__main__":
    raise SystemExit(main())
