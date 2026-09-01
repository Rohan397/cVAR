"""Navigation and handoff tests.

The curses drawing is exercised by a pty smoke test at the bottom; everything
above it drives ScrubApp's state directly, which is where the logic lives.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from make_fixture import build  # noqa: E402
from scrub.bridge import (  # noqa: E402
    VSCODE_FAMILY,
    EditorBridge,
    _relatedness,
    detect_editor,
    discover_nvim_server,
)
import subprocess  # noqa: E402

from scrub import bridge as bridge_mod  # noqa: E402
from scrub import chunks, doctor, glyphs, watch  # noqa: E402
from scrub.model import Timeline  # noqa: E402
from scrub.tui import ST_DELETED, ST_RAMP, ScrubApp  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


class FakeEditor:
    """A stand-in editor that records the argv it was launched with.

    The basename decides which transport the bridge picks, so tests name it
    "cursor" or "nvim" to select one.
    """

    def __init__(self, directory: Path, name: str = "cursor") -> None:
        self.log = directory / "launches.txt"
        self.path = directory / name
        self.path.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + str(self.log) + "\n"
        )
        self.path.chmod(0o755)

    def launches(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text().splitlines() if line]


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = build(Path(cls._tmp.name) / "repo")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.timeline = Timeline.load(self.repo)
        self.addCleanup(self.timeline.close)
        # A per-test directory, so one test's launches never leak into another's.
        editor_dir = tempfile.TemporaryDirectory()
        self.addCleanup(editor_dir.cleanup)
        self.editor = FakeEditor(Path(editor_dir.name))
        self.bridge = EditorBridge(self.timeline, str(self.editor.path))
        self.addCleanup(self.bridge.close)
        self.track = next(
            t for t in self.timeline.track_order() if "authentication" in t.label
        )

    def wait_for_launch(self, count: int = 1) -> list[str]:
        for _ in range(100):  # the editor is launched detached
            launches = self.editor.launches()
            if len(launches) >= count:
                return launches
            time.sleep(0.02)
        return self.editor.launches()

    def test_fake_editor_is_recognised_as_vscode_family(self):
        self.assertEqual(self.bridge.mode, "gui")
        self.assertIn(self.bridge.name, VSCODE_FAMILY)

    def test_diff_launches_editor_with_two_files(self):
        message = self.bridge.open_diff(self.track.id, 5)
        launch = self.wait_for_launch()[0]
        self.assertIn("--diff", launch)
        self.assertIn("--reuse-window", launch)
        self.assertIn("↔", message)

    def test_diff_filenames_carry_the_short_shas(self):
        self.bridge.open_diff(self.track.id, 5)
        launch = self.wait_for_launch()[0]
        before = self.timeline.commits[4].short
        after = self.timeline.commits[5].short
        self.assertIn(f"@{before}.py", launch)
        self.assertIn(f"@{after}.py", launch)

    def test_staged_files_keep_their_suffix_for_highlighting(self):
        self.bridge.open_state(self.track.id, 7)
        launch = self.wait_for_launch()[0]
        self.assertIn("--goto", launch)
        self.assertTrue(launch.strip().endswith(".py"))

    def test_first_commit_diffs_against_the_base_revision(self):
        # src/app.py predates the branch, so index 0 has a left-hand side.
        app = next(t for t in self.timeline.track_order() if "app.py" in t.label)
        message = self.bridge.open_diff(app.id, 0)
        self.assertIn("base", message)

    def test_state_reports_a_file_that_does_not_exist_yet(self):
        util = next(t for t in self.timeline.track_order() if "util.py" in t.label)
        self.assertIn("does not exist", self.bridge.open_state(util.id, 7))

    def test_cumulative_diffs_base_against_playhead(self):
        message = self.bridge.open_cumulative(self.track.id, 7)
        self.assertIn("base", message)
        self.assertIn(self.timeline.commits[7].short, message)

    def test_nothing_launches_without_an_explicit_call(self):
        self.assertEqual(self.editor.launches(), [])


class NavigationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = build(Path(cls._tmp.name) / "repo")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.timeline = Timeline.load(self.repo)
        self.addCleanup(self.timeline.close)
        self.app = ScrubApp(self.timeline, EditorBridge(self.timeline, "/nonexistent"))

    def test_playhead_starts_at_the_tip(self):
        self.assertEqual(self.app.playhead, len(self.timeline) - 1)

    def test_playhead_clamps_at_both_ends(self):
        self.app.move_playhead(-100)
        self.assertEqual(self.app.playhead, 0)
        self.app.move_playhead(100)
        self.assertEqual(self.app.playhead, len(self.timeline) - 1)

    def test_cursor_clamps_to_the_track_list(self):
        self.app.move_cursor(100)
        self.assertEqual(self.app.cursor, len(self.app.tracks) - 1)
        self.app.move_cursor(-100)
        self.assertEqual(self.app.cursor, 0)

    def test_bracket_jumps_between_clips_not_commits(self):
        self.app.cursor = next(
            i for i, t in enumerate(self.app.tracks) if "app.py" in t.label
        )
        self.app.playhead = 0
        self.app.jump_to_clip(1)
        # src/app.py is only touched once, at commit 5.
        self.assertEqual(self.app.playhead, 5)

    def test_bracket_reports_when_there_is_nowhere_to_jump(self):
        self.app.cursor = next(
            i for i, t in enumerate(self.app.tracks) if "app.py" in t.label
        )
        self.app.playhead = 7
        self.app.jump_to_clip(1)
        self.assertEqual(self.app.playhead, 7)
        self.assertIn("no further changes", self.app.status)

    def test_solo_narrows_to_one_track_and_restores_the_cursor(self):
        self.app.cursor = 1
        soloed = self.app.selected.id
        self.app.toggle_solo()
        self.assertEqual([t.id for t in self.app.tracks], [soloed])
        self.app.toggle_solo()
        self.assertIsNone(self.app.solo)
        self.assertEqual(self.app.selected.id, soloed)

    def test_handoff_reports_a_missing_editor_instead_of_raising(self):
        self.app._handoff("diff")
        self.assertIn("no editor", self.app.status)

    def test_cells_distinguish_absent_unchanged_changed_and_deleted(self):
        util = next(t for t in self.timeline.tracks.values() if "util.py" in t.label)
        test = next(t for t in self.timeline.tracks.values() if "test_auth" in t.label)
        self.assertEqual(self.app.cell(util, 5)[0], "·")  # live, untouched
        self.assertEqual(self.app.cell(util, 6)[0], "×")  # deleted here
        self.assertEqual(self.app.cell(util, 7)[0], " ")  # gone
        self.assertEqual(self.app.cell(test, 0)[0], " ")  # not added yet
        self.assertIn(self.app.cell(test, 2)[0], glyphs.UNICODE.ramp)  # added here


class NvimBridgeTest(unittest.TestCase):
    """nvim is a terminal app, so it gets different transports to VS Code."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = build(Path(cls._tmp.name) / "repo")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.timeline = Timeline.load(self.repo)
        self.addCleanup(self.timeline.close)
        editor_dir = tempfile.TemporaryDirectory()
        self.addCleanup(editor_dir.cleanup)
        self.editor = FakeEditor(Path(editor_dir.name), name="nvim")
        self.track = next(
            t for t in self.timeline.track_order() if "authentication" in t.label
        )
        # Point socket discovery at an empty directory. Without this these
        # tests pick up whatever nvim the developer happens to have open and
        # silently switch transport out from under the assertion.
        sockets = tempfile.TemporaryDirectory()
        self.addCleanup(sockets.cleanup)
        for name in ("TMPDIR", "NVIM"):
            self.addCleanup(_restore_env, name, os.environ.get(name))
        os.environ["TMPDIR"] = sockets.name + os.sep
        os.environ.pop("NVIM", None)

    def bridge(self, **kwargs) -> EditorBridge:
        made = EditorBridge(self.timeline, str(self.editor.path), **kwargs)
        self.addCleanup(made.close)
        return made

    def wait_for_launch(self) -> str:
        for _ in range(100):
            launches = self.editor.launches()
            if launches:
                return launches[0]
            time.sleep(0.02)
        self.fail("editor was never launched")

    def launch_argv(self) -> list[str]:
        """Launch arguments as tokens.

        Substring checks against the joined command line are unsound: a
        generated temp path like `scrub-dyomifnp` contains "-d", so asserting
        a flag's absence fails at random.
        """
        return self.wait_for_launch().split()

    def test_nvim_without_a_server_suspends_the_tui(self):
        bridge = self.bridge()
        self.assertEqual(bridge.mode, "suspend")

    def test_nvim_with_a_server_uses_rpc(self):
        self.assertEqual(self.bridge(server="/tmp/nvim.sock").mode, "remote")

    def test_running_inside_nvims_terminal_is_detected(self):
        os.environ["NVIM"] = "/tmp/parent.sock"
        self.addCleanup(os.environ.pop, "NVIM", None)
        bridge = self.bridge()
        self.assertEqual(bridge.mode, "remote")
        self.assertEqual(bridge.server, "/tmp/parent.sock")

    def test_suspend_hands_over_the_terminal_and_takes_it_back(self):
        events = []

        @contextlib.contextmanager
        def suspend():
            events.append("out")
            yield
            events.append("back")

        bridge = self.bridge(suspend=suspend)
        bridge.open_diff(self.track.id, 5)
        # The editor runs to completion between the two, not detached.
        self.assertEqual(events, ["out", "back"])
        self.assertIn("-d", self.editor.launches()[0].split())

    def test_suspend_mode_passes_vim_diff_flag_with_both_revisions(self):
        bridge = self.bridge()
        bridge.open_diff(self.track.id, 5)
        argv = self.launch_argv()
        launch = " ".join(argv)
        before = self.timeline.commits[4].short
        after = self.timeline.commits[5].short
        self.assertIn("-d", argv)
        self.assertIn(f"@{before}.py", launch)
        self.assertIn(f"@{after}.py", launch)

    def test_remote_mode_drives_nvim_over_rpc(self):
        bridge = self.bridge(server="/tmp/nvim.sock")
        bridge.open_diff(self.track.id, 5)
        launch = self.wait_for_launch()
        self.assertIn("--server /tmp/nvim.sock", launch)
        self.assertIn("--remote-expr", launch)
        self.assertIn("dofile", launch)

    def test_remote_script_carries_both_revisions(self):
        bridge = self.bridge(server="/tmp/nvim.sock")
        bridge.open_diff(self.track.id, 5)
        self.wait_for_launch()
        script = (bridge._dir / "open.lua").read_text()
        before = self.timeline.commits[4].short
        after = self.timeline.commits[5].short
        self.assertIn(f"@{before}.py", script)
        self.assertIn(f"@{after}.py", script)

    def test_remote_split_is_rightbelow_so_new_lands_on_the_right(self):
        # Without this the user's 'splitright' setting decides which revision
        # appears where, which would silently invert the diff.
        bridge = self.bridge(server="/tmp/nvim.sock")
        bridge.open_diff(self.track.id, 5)
        self.wait_for_launch()
        self.assertIn(
            "vertical rightbelow diffsplit", (bridge._dir / "open.lua").read_text()
        )

    def test_remote_reuses_one_tab_instead_of_stacking(self):
        # Thirty handoffs should leave one diff tab, not thirty.
        bridge = self.bridge(server="/tmp/nvim.sock")
        bridge.open_diff(self.track.id, 5)
        self.wait_for_launch()
        script = (bridge._dir / "open.lua").read_text()
        self.assertIn("vim.g.scrub_tab", script)
        self.assertIn("nvim_tabpage_is_valid", script)

    def test_remote_state_pane_has_no_right_hand_side(self):
        # Lua has no null; the absent revision must land as nil so the script's
        # `if request.right` test skips the diffsplit.
        bridge = self.bridge(server="/tmp/nvim.sock")
        bridge.open_state(self.track.id, 7)
        self.wait_for_launch()
        self.assertIn("right = nil", (bridge._dir / "open.lua").read_text())

    def test_remote_restores_focus_only_for_a_terminal_caller(self):
        # Inside nvim's own :terminal the RPC steals the cursor from the
        # scrubber; in a sibling terminal pane there is nothing to restore.
        bridge = self.bridge(server="/tmp/nvim.sock")
        bridge.open_diff(self.track.id, 5)
        self.wait_for_launch()
        script = (bridge._dir / "open.lua").read_text()
        self.assertIn('== "terminal"', script)
        self.assertIn("startinsert", script)

    def test_state_opens_a_single_buffer_without_the_diff_flag(self):
        bridge = self.bridge()
        bridge.open_state(self.track.id, 7)
        argv = self.launch_argv()
        self.assertNotIn("-d", argv)
        self.assertTrue(argv[-1].endswith(".py"))

    def test_snapshots_open_read_only_so_they_cannot_be_edited(self):
        # The temp dir is deleted on exit; a save would silently vanish.
        bridge = self.bridge()
        bridge.open_diff(self.track.id, 5)
        self.assertIn("-R", self.launch_argv())

    def test_remote_mode_also_locks_the_buffers(self):
        bridge = self.bridge(server="/tmp/nvim.sock")
        bridge.open_diff(self.track.id, 5)
        self.wait_for_launch()
        script = (bridge._dir / "open.lua").read_text()
        self.assertIn("readonly nomodifiable", script)

    def test_configured_editor_outranks_an_installed_gui(self):
        # Having Cursor on disk must not override someone's $EDITOR.
        for name in ("SCRUB_EDITOR", "VISUAL", "NVIM"):
            os.environ.pop(name, None)
            self.addCleanup(os.environ.pop, name, None)
        os.environ["EDITOR"] = str(self.editor.path)
        self.addCleanup(os.environ.pop, "EDITOR", None)
        self.assertEqual(Path(detect_editor() or "").name, "nvim")

    def test_scrub_editor_beats_everything_else(self):
        os.environ["EDITOR"] = "/bin/cat"
        os.environ["SCRUB_EDITOR"] = str(self.editor.path)
        self.addCleanup(os.environ.pop, "EDITOR", None)
        self.addCleanup(os.environ.pop, "SCRUB_EDITOR", None)
        self.assertEqual(Path(detect_editor() or "").name, "nvim")

    def _clear_editor_env(self):
        for name in ("EDITOR", "VISUAL", "SCRUB_EDITOR"):
            self.addCleanup(_restore_env, name, os.environ.get(name))
            os.environ.pop(name, None)

    def _fake_discovery(self, socket):
        import scrub.bridge as bridge_mod

        real = bridge_mod.discover_nvim_server
        self.addCleanup(setattr, bridge_mod, "discover_nvim_server", real)
        bridge_mod.discover_nvim_server = lambda repo, editor: socket

    @unittest.skipUnless(shutil.which("nvim"), "nvim not installed")
    def test_live_nvim_on_this_repo_outranks_an_installed_gui(self):
        # Having Cursor on the machine is not evidence about this repo; an nvim
        # already open on it is. Otherwise the two-pane workflow silently sends
        # every handoff to the wrong editor.
        self._clear_editor_env()
        self._fake_discovery("/tmp/fake-nvim.sock")
        bridge = EditorBridge(self.timeline, None)
        self.addCleanup(bridge.close)
        self.assertEqual(Path(bridge.editor or "").name, "nvim")
        self.assertEqual(bridge.mode, "remote")

    def test_a_stated_editor_is_never_second_guessed(self):
        # A configured $EDITOR outranks evidence — discovery must not override it.
        self._clear_editor_env()
        self._fake_discovery("/tmp/fake-nvim.sock")
        os.environ["EDITOR"] = str(self.editor.path.parent / "cursor")
        FakeEditor(self.editor.path.parent, name="cursor")
        bridge = EditorBridge(self.timeline, None)
        self.addCleanup(bridge.close)
        self.assertEqual(bridge.mode, "gui")

    def test_a_bogus_named_editor_is_reported_not_substituted(self):
        # --editor names one editor. Quietly running a different one would turn
        # a typo into handoffs landing somewhere the user never chose.
        self._clear_editor_env()
        self._fake_discovery("/tmp/fake-nvim.sock")
        bridge = EditorBridge(self.timeline, "/nonexistent")
        self.addCleanup(bridge.close)
        self.assertIsNone(bridge.editor)
        self.assertEqual(bridge.mode, "none")
        self.assertFalse(bridge.available)

    def test_no_running_nvim_falls_back_to_the_installed_editor(self):
        self._clear_editor_env()
        self._fake_discovery(None)
        bridge = EditorBridge(self.timeline, None)
        self.addCleanup(bridge.close)
        self.assertIsNotNone(bridge.editor)
        self.assertNotEqual(bridge.mode, "remote")

    def test_doctor_names_the_transport_and_why_it_was_chosen(self):
        self._clear_editor_env()
        self._fake_discovery(None)
        os.environ["EDITOR"] = str(self.editor.path)  # the fake nvim
        text = doctor.report(self.timeline, None, None)
        self.assertIn("transport   suspend", text)
        self.assertIn("from $EDITOR", text)
        # The whole point: say where the diff will actually land.
        self.assertIn("THIS pane, not the other one", text)

    def test_doctor_reports_a_matching_socket_as_ready(self):
        self._clear_editor_env()
        self._fake_discovery("/tmp/fake-nvim.sock")
        os.environ["EDITOR"] = str(self.editor.path)
        text = doctor.report(self.timeline, None, None)
        self.assertIn("transport   remote", text)
        self.assertIn("Ready", text)

    def test_relatedness_prefers_the_closest_working_directory(self):
        # An nvim opened at $HOME is an ancestor of every repo; it must never
        # outrank one actually sitting in the project.
        repo = Path("/a/b/c")
        self.assertEqual(_relatedness(Path("/a/b/c"), repo), 0)
        self.assertEqual(_relatedness(Path("/a/b"), repo), 1)
        self.assertEqual(_relatedness(Path("/a"), repo), 2)
        self.assertEqual(_relatedness(Path("/a/b/c/src"), repo), 1)
        self.assertIsNone(_relatedness(Path("/x/y"), repo))

    def test_discovery_is_skipped_for_non_vim_editors(self):
        # Cursor has no socket to find; probing would just cost startup time.
        self.assertIsNone(discover_nvim_server(Path("/tmp"), "/usr/local/bin/cursor"))
        self.assertIsNone(discover_nvim_server(Path("/tmp"), None))

    def test_a_deleted_file_still_reports_rather_than_launching(self):
        util = next(t for t in self.timeline.track_order() if "util.py" in t.label)
        bridge = self.bridge()
        self.assertIn("does not exist", bridge.open_state(util.id, 7))
        self.assertEqual(self.editor.launches(), [])


class LayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = build(Path(cls._tmp.name) / "repo")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.timeline = Timeline.load(self.repo)
        self.addCleanup(self.timeline.close)
        self.app = ScrubApp(self.timeline, EditorBridge(self.timeline, None))

    def test_few_commits_stretch_to_fill_the_width(self):
        spans = self.app.spans(80)
        self.assertEqual(len(spans), len(self.timeline))
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[len(self.timeline) - 1][1], 80)

    def test_stretched_spans_are_contiguous_with_no_gaps(self):
        spans = self.app.spans(80)
        for index in range(1, len(self.timeline)):
            self.assertEqual(spans[index][0], spans[index - 1][1])

    def test_rounding_is_spread_rather_than_dropped_at_the_end(self):
        # 8 commits into 79 columns divides unevenly; the row must still end flush.
        spans = self.app.spans(79)
        widths = {x1 - x0 for x0, x1 in spans.values()}
        self.assertEqual(spans[len(self.timeline) - 1][1], 79)
        self.assertTrue(widths <= {9, 10}, widths)

    def test_more_commits_than_columns_falls_back_to_one_column_and_scrolls(self):
        spans = self.app.spans(4)
        self.assertTrue(all(x1 - x0 == 1 for x0, x1 in spans.values()))
        self.assertEqual(len(spans), 4)

    def test_scrolling_keeps_the_playhead_visible(self):
        self.app.playhead = 7
        self.app._scroll(track_count=4, body_h=10, grid_w=4)
        spans = self.app.spans(4)
        self.assertIn(7, spans)
        self.app.playhead = 0
        self.app._scroll(track_count=4, body_h=10, grid_w=4)
        self.assertIn(0, self.app.spans(4))

    def test_stretched_layout_never_scrolls(self):
        self.app.playhead = 0
        self.app._scroll(track_count=4, body_h=10, grid_w=80)
        self.assertEqual(self.app.commit_offset, 0)

    def test_ramp_never_uses_the_deleted_style(self):
        # Green/red was the collision being designed out; deletion must stay
        # on its own style regardless of churn.
        styles = {
            self.app.cell(track, i)[1]
            for track in self.timeline.tracks.values()
            for i in range(len(self.timeline))
        }
        ramp_styles = {s for s in styles if ST_RAMP <= s < ST_RAMP + 4}
        self.assertNotIn(ST_DELETED, ramp_styles)
        self.assertTrue(ramp_styles)


class RenderTest(unittest.TestCase):
    """Assert on what the app actually draws.

    The screen is read back out of the curses window with instr() rather than
    scraped from the terminal stream: ncurses compresses runs of identical
    cells with the REP escape, so the bytes on the wire are not a faithful
    picture of the grid.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = build(Path(cls._tmp.name) / "repo")
        cls.screen = _render(cls.repo, rows=16, cols=100)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def row(self, needle: str) -> str:
        return next(line for line in self.screen if line.startswith(needle))

    def test_grid_stretches_to_the_full_terminal_width(self):
        row = self.row("src/authentication.py")
        self.assertEqual(len(row.rstrip("\x00")), 100)

    def test_each_commit_becomes_a_wide_clip(self):
        # Eight commits across ~76 grid columns: runs, not single cells.
        heavy = glyphs.UNICODE.ramp[-1]
        self.assertIn(heavy * 8, self.row("src/authentication.py"))

    def test_ramp_glyphs_survive_the_locale(self):
        row = self.row("src/authentication.py")
        self.assertTrue(set(glyphs.UNICODE.ramp[1:]) & set(row), row)

    def test_deleted_track_renders_its_own_glyph(self):
        self.assertIn("×××", self.row("src/util.py"))

    def test_track_absent_before_it_is_added_stays_blank(self):
        row = self.row("tests/test_auth.py")
        self.assertTrue(row[23:40].isspace(), repr(row[23:40]))

    def test_commit_row_shows_the_whole_message_for_the_playhead(self):
        # The playhead defaults to the tip, whose subject was previously sliced
        # to "issue a s" by the per-commit layout.
        self.assertIn("issue a session token on login", self.row("commit"))

    def test_commit_row_carries_the_short_sha_and_attribution(self):
        row = self.row("commit")
        self.assertIn("Fixture", row)
        self.assertRegex(row, r"commit\s+[0-9a-f]{7}\s+issue a session token")

    def test_commit_row_follows_the_playhead(self):
        screen = _render(self.repo, rows=16, cols=100, playhead=0)
        row = next(line for line in screen if line.startswith("commit"))
        self.assertIn("scaffold auth", row)
        self.assertNotIn("issue a session token", row)

    def test_ruler_marks_the_playhead_with_a_caret(self):
        indices = [i for i, line in enumerate(self.screen) if line.startswith("commit")]
        ruler = self.screen[indices[0] - 1]
        self.assertIn("┼────", ruler)
        self.assertIn("▼", ruler)
        # The tip is the rightmost commit, so the caret belongs on that half.
        self.assertGreater(ruler.index("▼"), len(ruler.rstrip()) // 2)

    def test_clip_line_no_longer_repeats_the_sha(self):
        clip_line = next(line for line in self.screen if "+4 −1" in line)
        self.assertIn("M  src/authentication.py", clip_line)
        self.assertNotRegex(clip_line, r"[0-9a-f]{7}")

    def test_clip_line_sits_directly_under_the_commit_row(self):
        commit_at = next(i for i, line in enumerate(self.screen) if line.startswith("commit"))
        self.assertIn("+4 −1", self.screen[commit_at + 1])

    def test_clip_line_aligns_with_the_grid_not_the_left_edge(self):
        clip_line = next(line for line in self.screen if "+4 −1" in line)
        self.assertTrue(clip_line.startswith(" "), repr(clip_line[:8]))


def _render(repo: Path, rows: int, cols: int, playhead: int | None = None,
            default_pane: str = "diff", zoom_track: str | None = None) -> list[str]:
    """Draw one frame in a pty and return the window contents, line by line."""
    import fcntl
    import pickle
    import pty
    import struct
    import termios

    read_fd, write_fd = os.pipe()
    pid, fd = pty.fork()
    if pid == 0:
        os.close(read_fd)
        os.environ["TERM"] = "xterm-256color"
        os.environ.setdefault("LANG", "en_US.UTF-8")
        sys.path.insert(0, str(PROJECT))
        import curses
        import locale

        locale.setlocale(locale.LC_ALL, "")
        from scrub.bridge import EditorBridge
        from scrub.model import Timeline
        from scrub.tui import ScrubApp, _init_colors

        timeline = Timeline.load(repo)
        app = ScrubApp(timeline, EditorBridge(timeline, None), default_pane)
        if playhead is not None:
            app.playhead = playhead
        if zoom_track is not None:
            app.cursor = next(
                i for i, t in enumerate(app.tracks) if zoom_track in t.label
            )
            app.toggle_zoom()

        def draw_once(stdscr):
            _init_colors()
            app.draw(stdscr)
            height = stdscr.getmaxyx()[0]
            return [stdscr.instr(y, 0).decode("utf-8", "replace") for y in range(height)]

        lines = curses.wrapper(draw_once)
        os.write(write_fd, pickle.dumps(lines))
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    # The pty has to be drained alongside the result pipe: the child blocks
    # writing escape sequences once its buffer fills, and would never reach the
    # point of sending the screen back.
    import select

    payload = b""
    open_fds = {read_fd, fd}
    deadline = time.time() + 30
    while open_fds and time.time() < deadline:
        ready, _, _ = select.select(list(open_fds), [], [], 0.2)
        for ready_fd in ready:
            try:
                chunk = os.read(ready_fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                open_fds.discard(ready_fd)
            elif ready_fd == read_fd:
                payload += chunk

    os.close(read_fd)
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass
    os.waitpid(pid, 0)
    os.close(fd)
    if not payload:
        raise AssertionError("child never returned a screen")
    return pickle.loads(payload)


class TtyProbeTest(unittest.TestCase):
    """Socket discovery has to work from a real terminal.

    Under a tty, `nvim --remote-expr` starts a full TUI unless stdin is closed:
    it takes over the alt-screen, writes capability queries to the terminal, and
    returns escape sequences on stdout instead of the expression result. Every
    socket then reads as unrelated, the transport silently downgrades, and the
    user's prompt is left with escape-sequence litter. A subprocess-based test
    cannot see any of this — it only reproduces with a tty attached.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = build(Path(cls._tmp.name) / "repo")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @unittest.skipUnless(shutil.which("nvim"), "nvim not installed")
    def test_probe_returns_a_path_not_escape_sequences(self):
        import pty
        import subprocess

        nvim = subprocess.Popen(
            ["nvim", "--headless", "-n", "-u", "NONE", "."],
            cwd=self.repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(nvim.wait)
        self.addCleanup(nvim.kill)
        time.sleep(2)

        program = (
            f"import sys; sys.path.insert(0, {str(PROJECT)!r})\n"
            "from scrub.bridge import nvim_sockets, socket_cwd\n"
            "import shutil\n"
            "found = [socket_cwd(s, shutil.which('nvim')) for s in nvim_sockets()]\n"
            "sys.stderr.write('ANSWER:' + repr([f for f in found if f]) + '\\n')\n"
        )
        pid, fd = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, "-c", program])

        output = b""
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                output += os.read(fd, 65536)
            except OSError:
                pass
            if os.waitpid(pid, os.WNOHANG)[0]:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        os.close(fd)

        text = output.decode("utf-8", "replace")
        answer = next((l for l in text.splitlines() if "ANSWER:" in l), "")
        self.assertIn(str(Path(self.repo).resolve()), answer, answer[:300])
        self.assertNotIn("\\x1b", answer)


class CursesSmokeTest(unittest.TestCase):
    """Actually render the TUI in a pty and drive it with keystrokes."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = build(Path(cls._tmp.name) / "repo")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_tui_renders_and_exits_cleanly(self):
        import pty

        keys = b"\x1b[C\x1b[D\x1b[B\x1b[A]f[fgGq"  # arrows, jumps, solo, quit
        pid, fd = pty.fork()
        if pid == 0:  # child
            os.environ["TERM"] = "xterm-256color"
            os.chdir(PROJECT)
            os.execv(sys.executable, [sys.executable, "-m", "scrub", str(self.repo)])

        time.sleep(1.2)  # let the timeline load and paint
        os.write(fd, keys)
        output = b""
        deadline = time.time() + 8
        while time.time() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            output += chunk
            if done:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            self.fail("TUI did not exit on 'q'")

        os.close(fd)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        text = output.decode(errors="replace")
        self.assertIn("scrub", text)
        self.assertIn("authentication.py", text)
        self.assertIn("commit", text)


class DefaultPaneTest(unittest.TestCase):
    """⏎ is rebindable; the explicit per-pane keys always remain."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = build(Path(cls._tmp.name) / "repo")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def app(self, pane):
        timeline = Timeline.load(self.repo)
        self.addCleanup(timeline.close)
        return ScrubApp(timeline, EditorBridge(timeline, "/nonexistent"), pane)

    def test_unified_is_the_default(self):
        # One buffer with + and - coloured answers "what did this commit do",
        # which is the question asked most often.
        self.assertEqual(self.app("diff").default_pane, "diff")
        self.assertEqual(ScrubApp(
            Timeline.load(self.repo), EditorBridge(Timeline.load(self.repo), "/nonexistent")
        ).default_pane, "unified")

    def test_unified_has_its_own_key_too(self):
        self.assertIn("u unified", self.app("diff")._help())
        self.assertIn("d split", self.app("diff")._help())

    def test_enter_opens_the_configured_pane(self):
        self.assertEqual(self.app("state").default_pane, "state")
        self.assertEqual(self.app("cumulative").default_pane, "cumulative")

    def test_an_unknown_pane_falls_back_to_unified(self):
        self.assertEqual(self.app("nonsense").default_pane, "unified")

    def test_help_bar_names_the_current_default(self):
        # Rebinding ⏎ without saying so would leave the user guessing.
        self.assertIn("enter state", self.app("state")._help())
        self.assertIn("enter diff", self.app("diff")._help())

    def test_every_pane_keeps_its_own_key_regardless(self):
        help_text = self.app("state")._help()
        for key, name in (
            ("u", "unified"), ("d", "split"), ("s", "state"), ("c", "cumul")
        ):
            self.assertIn(f"{key} {name}", help_text)




class UnifiedDiffTest(unittest.TestCase):
    """The single-buffer diff view, and mapping a file line into it."""

    def test_file_line_maps_to_its_position_in_the_diff_text(self):
        diff = (
            "diff --git a/f.py b/f.py\n"
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,2 +1,2 @@\n"
            " keep\n"
            "-old\n"
            "+new\n"
            "@@ -40,2 +40,3 @@\n"
            " ctx\n"
            "+added\n"
        )
        # A zoomed region is a file coordinate; the buffer is the diff itself,
        # so the two do not share a numbering.
        self.assertEqual(bridge_mod._diff_line_for(diff, 40), 8)
        self.assertIsNone(bridge_mod._diff_line_for(diff, None))

    def test_a_line_past_the_end_does_not_crash(self):
        self.assertIsNone(bridge_mod._diff_line_for("@@ -1 +1 @@\n ctx\n", 9999))


class GlyphTest(unittest.TestCase):
    """Both character sets, and switching between them at runtime."""

    def tearDown(self):
        glyphs._current = glyphs.UNICODE

    def test_every_cell_state_stays_distinct_in_ascii(self):
        # If two states collapse to the same character the grid stops meaning
        # anything, which is worse than a missing glyph.
        a = glyphs.ASCII
        cells = set(a.ramp) | {a.unchanged, a.absent, a.deleted}
        self.assertEqual(len(cells), 7)

    def test_ramp_shares_a_block_with_the_full_block(self):
        # The shade characters U+2591-2593 are absent from many fonts that ship
        # U+2588, so a shade ramp renders as "?" on machines that draw the full
        # block fine. Keeping the ramp in the block-elements range travels.
        for ch in glyphs.UNICODE.ramp:
            self.assertTrue(0x2580 <= ord(ch) <= 0x259F, f"{ch} U+{ord(ch):04X}")
        self.assertEqual(glyphs.UNICODE.ramp[-1], "█")

    def test_both_sets_have_a_four_step_ramp(self):
        self.assertEqual(len(glyphs.UNICODE.ramp), 4)
        self.assertEqual(len(glyphs.ASCII.ramp), 4)

    def test_ascii_set_is_pure_ascii(self):
        for name, value in vars(glyphs.ASCII).items():
            value.encode("ascii")  # raises if not

    def test_switching_takes_effect_without_reimporting(self):
        # --ascii is parsed after import, so a glyph bound at import time would
        # leave the flag doing nothing at all.
        self.assertEqual(glyphs.active().ramp, glyphs.UNICODE.ramp)
        glyphs.use_ascii()
        self.assertEqual(glyphs.active().ramp, glyphs.ASCII.ramp)

    def test_the_enter_key_is_spelled_out_in_both(self):
        # U+23CE is the glyph most often missing from a monospace font, and a
        # help bar is the worst place to render a "?".
        self.assertEqual(glyphs.UNICODE.enter, "enter")
        self.assertEqual(glyphs.ASCII.enter, "enter")


class LiveTest(unittest.TestCase):
    """Picking up commits that land while the scrubber is open."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.commit("one")
        self.commit("two")
        self.timeline = Timeline.load(self.repo)
        self.addCleanup(self.timeline.close)
        self.app = ScrubApp(self.timeline, EditorBridge(self.timeline, "/nonexistent"))

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args],
                       check=True, stdout=subprocess.DEVNULL)

    def commit(self, message):
        (self.repo / f"{message}.py").write_text("x\n")
        self.git("add", "-A")
        self.git("-c", "user.email=t@e", "-c", "user.name=T",
                 "commit", "-q", "-m", message)

    def test_tip_matches_git_without_forking_it(self):
        asked = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(watch.tip(self.repo), asked)

    def test_tip_survives_packed_refs(self):
        # `git gc` moves loose refs into packed-refs; the loose file disappears.
        before = watch.tip(self.repo)
        self.git("pack-refs", "--all")
        self.assertEqual(watch.tip(self.repo), before)

    def test_refresh_picks_up_a_new_commit(self):
        self.assertEqual(len(self.app.timeline), 2)
        self.commit("three")
        self.app.refresh()
        self.assertEqual(len(self.app.timeline), 3)
        self.assertIn("1 new commit", self.app.status)

    def test_following_rides_the_tip(self):
        self.assertTrue(self.app.follow)
        self.commit("three")
        self.app.refresh()
        self.assertEqual(self.app.playhead, len(self.app.timeline) - 1)

    def test_stepping_back_stops_following(self):
        # Reading something is not watching it; the playhead must stay put.
        self.app.move_playhead(-1)
        self.assertFalse(self.app.follow)
        parked = self.app.playhead
        self.commit("three")
        self.app.refresh()
        self.assertEqual(self.app.playhead, parked)

    def test_jumping_to_the_end_resumes_following(self):
        self.app.move_playhead(-1)
        self.assertFalse(self.app.follow)
        self.app.playhead = len(self.app.timeline) - 1
        self.app.follow = True
        self.commit("three")
        self.app.refresh()
        self.assertEqual(self.app.playhead, len(self.app.timeline) - 1)

    def test_refresh_keeps_the_selected_file(self):
        order = self.app.tracks
        self.app.cursor = len(order) - 1
        chosen = self.app.selected.label
        self.commit("three")
        self.app.refresh()
        self.assertEqual(self.app.selected.label, chosen)

    def test_a_broken_reload_reports_instead_of_raising(self):
        self.app.timeline._load_args = (Path("/nonexistent-repo"), None, None)
        self.app.refresh()
        self.assertIn("reload failed", self.app.status)


class ChunkTest(unittest.TestCase):
    """Zooming a track into the regions of the file that actually changed."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls._tmp.name) / "repo"
        cls.repo.mkdir()
        run = lambda *a: subprocess.run(
            ["git", "-C", str(cls.repo), *a], check=True, stdout=subprocess.DEVNULL
        )
        run("init", "-q", "-b", "main")
        target = cls.repo / "service.py"

        body = []
        for name in ("connect", "authenticate", "query", "close"):
            body += [f"def {name}():"] + [f"    # {name} {i}" for i in range(8)] + [""]
        target.write_text("\n".join(body) + "\n")

        def commit(message):
            run("add", "-A")
            run("-c", "user.email=t@e", "-c", "user.name=T", "commit", "-q", "-m", message)

        commit("add service")
        for marker, replacement, message in (
            ("# authenticate 0", "    token = read_token()", "read a token"),
            ("# close 0", "    self.shutdown()", "shut down on close"),
            ("# connect 0", "    self.sock = socket()", "open a socket"),
            ("# authenticate 4", "    verify(token)", "verify the token"),
        ):
            lines = target.read_text().splitlines()
            for i, line in enumerate(lines):
                if marker in line:
                    lines[i] = replacement
                    break
            target.write_text("\n".join(lines) + "\n")
            commit(message)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.timeline = Timeline.load(self.repo)
        self.addCleanup(self.timeline.close)
        self.track = next(iter(self.timeline.tracks.values()))

    def built(self):
        return chunks.build(self.timeline, self.track.id, len(self.timeline) - 1)

    def test_separate_edits_become_separate_regions(self):
        # Four edits in three functions; the two in authenticate share a region.
        self.assertGreaterEqual(len(self.built()), 3)

    def test_regions_are_disjoint_and_ordered(self):
        found = self.built()
        for earlier, later in zip(found, found[1:]):
            self.assertLess(earlier.end, later.start)

    def test_file_creation_does_not_swallow_the_file(self):
        # The add touches every line; letting it set bounds collapses the whole
        # file into one row and there is nothing left to zoom into.
        self.assertGreater(len(self.built()), 1)

    def test_every_region_names_the_commits_that_touched_it(self):
        for chunk in self.built():
            self.assertTrue(chunk.weight_by_commit)
            for index in chunk.weight_by_commit:
                self.assertIn(index, self.track.clips)

    def test_a_region_edited_twice_records_both_commits(self):
        busiest = max(self.built(), key=lambda c: len(c.weight_by_commit))
        self.assertGreaterEqual(len(busiest.weight_by_commit), 2)

    def test_hunks_project_forward_through_later_edits(self):
        # A line above an insertion keeps its number; one below shifts by the
        # net growth. Without this the grid marks the wrong region.
        mid = [chunks.Hunk(old_start=50, old_count=0, new_start=51, new_count=30)]
        self.assertEqual(chunks.project(10, mid), 10)   # above — unmoved
        self.assertEqual(chunks.project(60, mid), 90)   # below — shifted by 30

        # Inserting at the very top pushes everything down, including line 1.
        top = [chunks.Hunk(old_start=0, old_count=0, new_start=1, new_count=30)]
        self.assertEqual(chunks.project(18, top), 48)

    def test_projection_composes_across_several_commits(self):
        # Two later insertions must both apply, or a region drifts by the
        # amount of whichever one was skipped.
        first = [chunks.Hunk(old_start=0, old_count=0, new_start=1, new_count=10)]
        second = [chunks.Hunk(old_start=0, old_count=0, new_start=1, new_count=5)]
        line = 20
        for hunks in (first, second):
            line = chunks.project(line, hunks)
        self.assertEqual(line, 35)

    def test_a_line_inside_a_rewrite_collapses_to_its_start(self):
        later = [chunks.Hunk(old_start=10, old_count=5, new_start=10, new_count=2)]
        self.assertEqual(chunks.project(12, later), 10)

    def test_hunk_headers_parse_with_and_without_counts(self):
        parsed = chunks.parse_hunks("@@ -1,4 +1,6 @@\n@@ -20 +22 @@\nnot a header")
        self.assertEqual(len(parsed), 2)
        self.assertEqual((parsed[1].old_count, parsed[1].new_count), (1, 1))

    def test_zoom_switches_rows_and_back(self):
        app = ScrubApp(self.timeline, EditorBridge(self.timeline, "/nonexistent"))
        self.assertEqual([r.label for r in app.rows], [t.label for t in app.tracks])
        app.toggle_zoom()
        self.assertIsNotNone(app.zoom)
        self.assertEqual(app.rows, app.chunks)
        self.assertTrue(all(r.label.startswith("L") for r in app.rows))
        app.toggle_zoom()
        self.assertIsNone(app.zoom)
        self.assertEqual([r.label for r in app.rows], [t.label for t in app.tracks])

    def test_handoff_targets_the_selected_region(self):
        app = ScrubApp(self.timeline, EditorBridge(self.timeline, "/nonexistent"))
        app.toggle_zoom()
        app.move_cursor(1)
        chunk = app.selected_chunk
        self.assertIsNotNone(chunk)
        self.assertEqual(chunk, app.chunks[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
