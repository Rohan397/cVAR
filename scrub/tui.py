"""The scrubber.

Arrow keys move a playhead along the commit timeline and a cursor down the
track list. Enter hands the current frame to the editor. Nothing opens until
you ask it to, so scanning the grid costs nothing.

The timeline stretches to fill the terminal: with few commits each one becomes
a wide clip, the way a short sequence spreads across an editor's timeline panel
rather than huddling in the corner. Only when commits outnumber columns does it
fall back to one column each and scroll.
"""

from __future__ import annotations

import contextlib
import curses
import locale
import math

from . import chunks as chunkmod
from . import glyphs, watch
from .bridge import EditorBridge
from .model import Timeline, Track

RAMP_STEPS = 4  # the ramp is always four buckets, whichever set is in force

PANES = ("unified", "diff", "state", "cumulative")
ORDERS = ("recent", "first", "churn")

# Style ids. Resolved to curses attributes once colours are initialised, so the
# cell logic stays testable without a terminal.
ST_RAMP = 0  # ...through ST_RAMP + 3
ST_DELETED = 4
ST_QUIET = 5
ST_ABSENT = 6
ST_ACCENT = 7
ST_LABEL = 8
ST_SELECTED = 9

# Curses colour pair ids.
CP_COOL = 1
CP_WARM = 2
CP_ALERT = 3
CP_PLAIN = 4

STYLE_ATTR: dict[int, int] = {}


class ScrubApp:
    def __init__(
        self,
        timeline: Timeline,
        bridge: EditorBridge,
        default_pane: str = "unified",
    ) -> None:
        self.timeline = timeline
        self.bridge = bridge
        # What enter opens. Every pane keeps its own key regardless, so changing
        # this rebinds the default without taking any view away.
        self.default_pane = default_pane if default_pane in PANES else "unified"
        # Zoom: rows become regions of one file instead of one row per file.
        self.zoom: str | None = None
        self.chunks: list[chunkmod.Chunk] = []
        # Live view. Follow behaves like tail -f: on while parked at the
        # tip, off the moment you step back to look at something.
        self.follow = True
        self.order = "recent"
        self.tip = watch.tip(timeline.repo)
        self.playhead = len(timeline) - 1
        self.cursor = 0
        self.solo: str | None = None
        self.status = ""
        self.commit_offset = 0
        self.track_offset = 0
        self.ceiling = max(
            (clip.weight for track in timeline.tracks.values() for clip in track.clips.values()),
            default=1,
        )

    # -- state -----------------------------------------------------------

    @property
    def tracks(self) -> list[Track]:
        if self.solo is not None:
            return [self.timeline.tracks[self.solo]]
        return self.timeline.track_order(self.order)

    @property
    def selected(self) -> Track:
        """The track in play — the zoomed file, or the highlighted row."""
        if self.zoom is not None:
            return self.timeline.tracks[self.zoom]
        return self.tracks[min(self.cursor, len(self.tracks) - 1)]

    @property
    def rows(self) -> list:
        return self.chunks if self.zoom is not None else self.tracks

    @property
    def selected_chunk(self) -> chunkmod.Chunk | None:
        if self.zoom is None or not self.chunks:
            return None
        return self.chunks[min(self.cursor, len(self.chunks) - 1)]

    def refresh(self, announce: bool = True) -> None:
        """Rebuild from disk, keeping your place as far as it still exists."""
        was_track = self.selected.label if self.timeline.tracks else None
        was_zoom = self.zoom is not None
        previous = len(self.timeline)

        old = self.timeline
        try:
            self.timeline = old.reload()
        except Exception as exc:  # a rebase mid-read, a repo that vanished
            self.status = f"reload failed: {exc}"
            return
        old.close()

        self.tip = watch.tip(self.timeline.repo)
        arrived = len(self.timeline) - previous

        # Re-find the row by name; indices shift as commits land.
        self.zoom, self.chunks = None, []
        order = self.timeline.track_order(self.order)
        self.cursor = next(
            (i for i, track in enumerate(order) if track.label == was_track), 0
        )
        if self.follow:
            self.playhead = len(self.timeline) - 1
        else:
            self.playhead = min(self.playhead, len(self.timeline) - 1)
        if was_zoom and order:
            self.toggle_zoom()

        if announce and arrived > 0:
            plural = "s" if arrived != 1 else ""
            self.status = f"{arrived} new commit{plural}"
        elif announce:
            self.status = "reloaded"

    def toggle_zoom(self) -> None:
        """Zoom into the selected file, or back out to the file list."""
        if self.zoom is not None:
            restored = self.zoom
            self.zoom = None
            self.chunks = []
            self.cursor = next(
                (i for i, t in enumerate(self.tracks) if t.id == restored), 0
            )
            self.status = ""
            return
        track = self.selected
        found = chunkmod.build(self.timeline, track.id, self.playhead)
        if not found:
            self.status = f"{track.label} has no changes at or before this commit"
            return
        self.zoom, self.chunks, self.cursor = track.id, found, 0
        self.status = f"{track.label} — {len(found)} changed regions"

    def move_playhead(self, delta: int) -> None:
        self.playhead = max(0, min(self.playhead + delta, len(self.timeline) - 1))
        # Stepping away from the tip means you are reading, not watching.
        self.follow = self.playhead == len(self.timeline) - 1

    def move_cursor(self, delta: int) -> None:
        self.cursor = max(0, min(self.cursor + delta, len(self.rows) - 1))

    def jump_to_clip(self, direction: int) -> None:
        """Hop to the next commit that touched the selected track.

        The equivalent of stepping between keyframes: most commits leave any
        given file alone, so stepping one at a time is mostly dead air.
        """
        candidates = sorted(self.selected.clips)
        upcoming = [i for i in candidates if (i > self.playhead if direction > 0 else i < self.playhead)]
        if not upcoming:
            self.status = "no further changes to this track"
            return
        self.playhead = upcoming[0] if direction > 0 else upcoming[-1]
        self.status = ""

    def cycle_order(self) -> None:
        """Step through the row orderings, keeping the selected file selected."""
        was = self.selected.label if self.timeline.tracks else None
        self.order = ORDERS[(ORDERS.index(self.order) + 1) % len(ORDERS)]
        rows = self.tracks
        self.cursor = next((i for i, t in enumerate(rows) if t.label == was), 0)
        self.status = {
            "recent": "ordered by most recently changed",
            "first": "ordered by when each file first appeared",
            "churn": "ordered by lines changed",
        }[self.order]

    def toggle_solo(self) -> None:
        if self.solo is not None:
            restored = self.solo
            self.solo = None
            self.cursor = next((i for i, t in enumerate(self.tracks) if t.id == restored), 0)
            self.status = ""
        else:
            self.solo = self.selected.id
            self.cursor = 0
            self.status = f"soloed {self.selected.label}"

    def chunk_cell(self, chunk: chunkmod.Chunk, index: int) -> tuple[str, int]:
        weight = chunk.weight_by_commit.get(index)
        if weight is not None:
            step = _bucket(weight, self.ceiling)
            return glyphs.active().ramp[step], ST_RAMP + step
        if self.selected.state_at(index) == "live":
            return glyphs.active().unchanged, ST_QUIET
        return glyphs.active().absent, ST_ABSENT

    def cell(self, track: Track, index: int) -> tuple[str, int]:
        clip = track.clips.get(index)
        if clip is not None:
            if clip.kind == "D":
                return glyphs.active().deleted, ST_DELETED
            step = _bucket(clip.weight, self.ceiling)
            return glyphs.active().ramp[step], ST_RAMP + step
        if track.state_at(index) == "live":
            return glyphs.active().unchanged, ST_QUIET
        return glyphs.active().absent, ST_ABSENT

    # -- layout ----------------------------------------------------------

    def spans(self, grid_w: int) -> dict[int, tuple[int, int]]:
        """Map each visible commit to the column range it occupies.

        Integer boundaries are computed from the index rather than by repeating
        a fixed width, so rounding is spread across the row and the timeline
        ends flush with the right edge.
        """
        total = len(self.timeline)
        if total <= grid_w:
            return {i: (i * grid_w // total, (i + 1) * grid_w // total) for i in range(total)}
        last = min(total, self.commit_offset + grid_w)
        return {i: (i - self.commit_offset, i - self.commit_offset + 1) for i in range(self.commit_offset, last)}

    def _scroll(self, track_count: int, body_h: int, grid_w: int) -> None:
        """Keep the playhead and the selected track inside the viewport."""
        if len(self.timeline) <= grid_w:
            self.commit_offset = 0
        else:
            if self.playhead < self.commit_offset:
                self.commit_offset = self.playhead
            elif self.playhead >= self.commit_offset + grid_w:
                self.commit_offset = self.playhead - grid_w + 1
            self.commit_offset = max(0, min(self.commit_offset, len(self.timeline) - grid_w))

        self.cursor = min(self.cursor, max(0, track_count - 1))
        if self.cursor < self.track_offset:
            self.track_offset = self.cursor
        elif self.cursor >= self.track_offset + body_h:
            self.track_offset = self.cursor - body_h + 1

    # -- rendering -------------------------------------------------------

    def draw(self, stdscr: "curses._CursesWindow") -> None:
        stdscr.erase()
        height, cols = stdscr.getmaxyx()
        rows = self.rows
        zoomed = self.zoom is not None
        self.ceiling = max(
            (
                max(c.weight_by_commit.values(), default=1)
                for c in self.chunks
            )
            if zoomed
            else (
                clip.weight
                for track in self.timeline.tracks.values()
                for clip in track.clips.values()
            ),
            default=1,
        )

        label_w = max(12, min(26, max((len(r.label) for r in rows), default=10) + 1))
        grid_w = max(8, cols - label_w - 1)
        body_h = max(1, height - 8)

        self._scroll(len(rows), body_h, grid_w)
        spans = self.spans(grid_w)
        visible = rows[self.track_offset : self.track_offset + body_h]

        _put(stdscr, 0, 0, self._header(), cols, STYLE_ATTR.get(ST_ACCENT, 0))

        for offset, item in enumerate(visible):
            row = offset + 2
            selected = (self.track_offset + offset) == self.cursor if zoomed \
                else item.id == self.selected.id
            _put(stdscr, row, 0, _fit(item.label, label_w), label_w,
                 STYLE_ATTR.get(ST_SELECTED if selected else ST_LABEL, 0))
            for index, (x0, x1) in spans.items():
                glyph, style = (
                    self.chunk_cell(item, index) if zoomed else self.cell(item, index)
                )
                attr = STYLE_ATTR.get(style, 0)
                if index == self.playhead:
                    attr |= curses.A_REVERSE if selected else curses.A_BOLD
                _put(stdscr, row, label_w + 1 + x0, glyph * (x1 - x0), x1 - x0, attr)

        ruler_row = 2 + len(visible)
        self._draw_ruler(stdscr, ruler_row, label_w, spans)
        self._draw_commit_row(stdscr, ruler_row + 1, label_w, cols)

        # The clip detail belongs beside the commit it describes, not pinned to
        # the floor; only the status and help bar hold the bottom.
        _put(stdscr, ruler_row + 2, label_w + 1, self._clip_line(), cols, STYLE_ATTR.get(ST_LABEL, 0))
        _put(stdscr, height - 2, 0, self.status, cols, STYLE_ATTR.get(ST_ACCENT, 0))
        _put(stdscr, height - 1, 0, self._help(), cols, STYLE_ATTR.get(ST_LABEL, 0))
        stdscr.noutrefresh()
        curses.doupdate()

    def _draw_ruler(self, stdscr, row: int, label_w: int, spans: dict[int, tuple[int, int]]) -> None:
        G = glyphs.active()
        attr = STYLE_ATTR.get(ST_ABSENT, 0)
        for index, (x0, x1) in spans.items():
            width = x1 - x0
            mark = G.tick if width > 1 or index % 5 == 0 else G.rule
            _put(stdscr, row, label_w + 1 + x0, mark + G.rule * (width - 1), width, attr)

        # The caret is the only thing marking horizontal position once the
        # commit row stops being a per-commit strip.
        if self.playhead in spans:
            x0, x1 = spans[self.playhead]
            caret_x = label_w + 1 + x0 + (x1 - x0 - 1) // 2
            _put(stdscr, row, caret_x, G.caret, 1, STYLE_ATTR.get(ST_ACCENT, 0))

    def _draw_commit_row(self, stdscr, row: int, label_w: int, cols: int) -> None:
        G = glyphs.active()
        """The bottom track: the message of the commit under the playhead.

        Subjects were unreadable sliced into per-commit cells, so this row gives
        the whole width to one commit — the one you are parked on.
        """
        commit = self.timeline.commits[self.playhead]
        _put(stdscr, row, 0, _fit("commit", label_w), label_w, STYLE_ATTR.get(ST_LABEL, 0))

        width = max(0, cols - label_w - 1)
        subject = f"{commit.short}  {commit.subject}"
        meta = f"{commit.author} · {commit.when[:10]}"
        # Attribution only earns its place if the subject still fits whole.
        if len(subject) + len(meta) + 3 <= width:
            subject = subject.ljust(width - len(meta)) + meta
        _put(stdscr, row, label_w + 1, subject, width, STYLE_ATTR.get(ST_ACCENT, 0))

    def _help(self) -> str:
        G = glyphs.active()
        """The help bar names the current default, or the rebind is invisible."""
        keys = "  ".join(
            f"{key} {name}"
            for key, name in (
                ("u", "unified"), ("d", "split"), ("s", "state"), ("c", "cumul")
            )
        )
        return (
            f"{G.left}{G.right} commit  {G.up}{G.down} track  [ ] next change  "
            f"{G.enter} {self.default_pane}  {keys}  o order  r reload  "
            f"{'z files' if self.zoom else 'z chunks'}  f solo  q quit"
        )

    def _header(self) -> str:
        timeline = self.timeline
        churn = sum(t.weight for t in timeline.tracks.values())
        live = "  ●live" if self.follow else ""
        if self.zoom is not None:
            return (
                f"scrub  {self.playhead + 1}/{len(timeline)} commits · "
                f"{self.selected.label} · {len(self.chunks)} changed regions{live}"
            )
        solo = f"  solo:{self.solo}" if self.solo else ""
        return (
            f"scrub  {self.playhead + 1}/{len(timeline)} commits · "
            f"{len(timeline.tracks)} tracks · {churn} lines · by {self.order}{solo}{live}"
        )

    def _clip_line(self) -> str:
        """The selected track at the playhead. The sha lives on the commit row."""
        track = self.selected
        clip = track.clips.get(self.playhead)
        if clip is None:
            return f"{track.label}  ({track.state_at(self.playhead)}, unchanged here)"
        rename = f"  ← {clip.old_path}" if clip.old_path else ""
        return f"{clip.kind}  {clip.path}  +{clip.added} −{clip.deleted}{rename}"

    # -- loop ------------------------------------------------------------

    def run(self, stdscr: "curses._CursesWindow") -> None:
        curses.curs_set(0)
        stdscr.keypad(True)
        # Wake twice a second so new commits appear without a keypress. The
        # process sleeps in the kernel between ticks; it does not spin.
        stdscr.timeout(500)
        # Assemble split escape sequences rather than surfacing a bare ESC.
        try:
            curses.set_escdelay(25)
        except (AttributeError, curses.error):
            pass  # older curses builds

        _init_colors()
        # Terminal editors need the actual terminal, not a curses window.
        self.bridge.suspend = lambda: _suspended(stdscr)

        while True:
            self.draw(stdscr)
            try:
                key = stdscr.get_wch()
            except curses.error:
                # Timed out with no key: the only moment worth checking disk.
                current = watch.tip(self.timeline.repo)
                if current is not None and current != self.tip:
                    self.refresh()
                continue

            # ESC is deliberately not a quit key. With a read timeout an
            # arrow key's escape sequence can be delivered split, and a bare
            # ESC arriving first would quit the moment you pressed left.
            if key in ("q", "Q"):
                return
            if key == curses.KEY_RESIZE:
                continue
            if key in (curses.KEY_LEFT, "h"):
                self.move_playhead(-1)
            elif key in (curses.KEY_RIGHT, "l"):
                self.move_playhead(1)
            elif key in (curses.KEY_UP, "k"):
                self.move_cursor(-1)
            elif key in (curses.KEY_DOWN, "j"):
                self.move_cursor(1)
            elif key == "[":
                self.jump_to_clip(-1)
            elif key == "]":
                self.jump_to_clip(1)
            elif key == "g":
                self.playhead = 0
                self.follow = False
            elif key == "G":
                self.playhead = len(self.timeline) - 1
                self.follow = True
            elif key == "r":
                self.refresh()
            elif key == "F":
                self.follow = not self.follow
                if self.follow:
                    self.playhead = len(self.timeline) - 1
                self.status = "following" if self.follow else "not following"
            elif key == "o":
                self.cycle_order()
            elif key == "f":
                self.toggle_solo()
            elif key == "z":
                self.toggle_zoom()
            elif key in ("\n", "\r", curses.KEY_ENTER):
                self._handoff(self.default_pane)
            elif key == "u":
                self._handoff("unified")
            elif key == "d":
                self._handoff("diff")
            elif key == "s":
                self._handoff("state")
            elif key == "c":
                self._handoff("cumulative")

    def _handoff(self, pane: str) -> None:
        if not self.bridge.available:
            self.status = "no editor found (try --editor)"
            return
        opener = {
            "unified": self.bridge.open_unified,
            "diff": self.bridge.open_diff,
            "state": self.bridge.open_state,
            "cumulative": self.bridge.open_cumulative,
        }[pane]
        # Zoomed in, the handoff lands on the region you selected rather
        # than the top of the file — that is the point of zooming.
        chunk = self.selected_chunk
        self.status = opener(
            self.selected.id, self.playhead, chunk.start if chunk else None
        )


@contextlib.contextmanager
def _suspended(stdscr: "curses._CursesWindow"):
    """Drop out of curses so a terminal editor can own the screen.

    On the way back the window is redrawn from scratch: the editor will have
    scribbled over every cell, and curses still believes its own cache.
    """
    curses.endwin()
    try:
        yield
    finally:
        stdscr.clearok(True)
        stdscr.refresh()
        curses.doupdate()


def _bucket(weight: int, ceiling: int) -> int:
    if weight <= 0:
        return 0
    scaled = math.log1p(weight) / math.log1p(max(ceiling, 1))
    return min(RAMP_STEPS - 1, int(scaled * RAMP_STEPS))


def _fit(text: str, width: int) -> str:
    G = glyphs.active()
    if len(text) > width - 1:
        text = G.ellipsis + text[-(width - 2) :]
    return f"{text:<{width}}"


def _init_colors() -> None:
    """A colour-blind-safe scale.

    Churn rides the blue-yellow axis, which survives both deuteranopia and
    protanopia, and rises in luminance as well as hue so the ramp still reads
    in greyscale. Green is avoided entirely: it would sit next to the red of a
    deletion and collapse into it for the most common forms of colour blindness.
    Deletion gets magenta, the one hue distinct from both ends of that axis.

    The glyphs ░▒▓█ already encode magnitude, so colour is reinforcement rather
    than the only channel carrying the signal.
    """
    STYLE_ATTR.clear()
    if not curses.has_colors():
        STYLE_ATTR.update({
            ST_RAMP: 0, ST_RAMP + 1: 0, ST_RAMP + 2: curses.A_BOLD, ST_RAMP + 3: curses.A_BOLD,
            ST_DELETED: curses.A_BOLD, ST_QUIET: curses.A_DIM, ST_ABSENT: curses.A_DIM,
            ST_ACCENT: curses.A_BOLD, ST_LABEL: curses.A_DIM, ST_SELECTED: curses.A_BOLD,
        })
        return

    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(CP_COOL, curses.COLOR_CYAN, -1)
    curses.init_pair(CP_WARM, curses.COLOR_YELLOW, -1)
    curses.init_pair(CP_ALERT, curses.COLOR_MAGENTA, -1)
    curses.init_pair(CP_PLAIN, curses.COLOR_WHITE, -1)

    cool, warm = curses.color_pair(CP_COOL), curses.color_pair(CP_WARM)
    alert, plain = curses.color_pair(CP_ALERT), curses.color_pair(CP_PLAIN)
    STYLE_ATTR.update({
        ST_RAMP + 0: cool | curses.A_DIM,
        ST_RAMP + 1: cool,
        ST_RAMP + 2: warm,
        ST_RAMP + 3: warm | curses.A_BOLD,
        ST_DELETED: alert | curses.A_BOLD,
        ST_QUIET: plain | curses.A_DIM,
        ST_ABSENT: plain | curses.A_DIM,
        ST_ACCENT: plain | curses.A_BOLD,
        ST_LABEL: plain | curses.A_DIM,
        ST_SELECTED: plain | curses.A_BOLD,
    })


def _put(win: "curses._CursesWindow", y: int, x: int, text: str, width: int, attr: int) -> None:
    """addstr that tolerates the bottom-right corner and narrow terminals."""
    rows, cols = win.getmaxyx()
    if y < 0 or y >= rows or x >= cols or x < 0:
        return
    clipped = text[: max(0, min(width, cols - x))]
    if not clipped:
        return
    try:
        win.addstr(y, x, clipped, attr)
    except curses.error:
        pass  # writing the final cell always raises; the glyph still lands


def launch(
    timeline: Timeline,
    editor: str | None = None,
    server: str | None = None,
    default_pane: str = "unified",
) -> None:
    # curses encodes output through the C locale, so the ramp glyphs and box
    # drawing come out as garbage unless this is set first.
    locale.setlocale(locale.LC_ALL, "")

    bridge = EditorBridge(timeline, editor, server)
    try:
        curses.wrapper(ScrubApp(timeline, bridge, default_pane).run)
    finally:
        bridge.close()
