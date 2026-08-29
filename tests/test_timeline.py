"""Timeline model tests, run against a generated fixture repo.

    python3 tests/test_timeline.py

Deliberately stdlib-only (unittest, no pytest) so this stays dependency-free.
The parsing cases below are the ones that actually broke: git interposes a
newline between the --format line and the -z payload, and files inherited from
the base revision have no add clip to anchor their existence.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from make_fixture import build  # noqa: E402
from scrub.model import Timeline, _parse_name_status, _parse_numstat  # noqa: E402


class ParsingTest(unittest.TestCase):
    def test_name_status_consumes_two_paths_for_a_rename(self):
        toks = ["R100", "src/auth.py", "src/authentication.py", "M", "src/app.py"]
        self.assertEqual(
            list(_parse_name_status(toks)),
            [("R", "src/auth.py", "src/authentication.py"), ("M", None, "src/app.py")],
        )

    def test_numstat_packs_the_path_with_the_counts(self):
        self.assertEqual(_parse_numstat(["6\t2\tsrc/auth.py"]), {"src/auth.py": (6, 2)})

    def test_numstat_rename_keys_on_the_new_path(self):
        toks = ["0\t0\t", "src/auth.py", "src/authentication.py"]
        self.assertEqual(_parse_numstat(toks), {"src/authentication.py": (0, 0)})

    def test_numstat_treats_binary_dashes_as_zero(self):
        self.assertEqual(_parse_numstat(["-\t-\tlogo.png"]), {"logo.png": (0, 0)})


class TimelineTest(unittest.TestCase):
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

    def track(self, needle: str):
        return next(t for t in self.timeline.track_order() if needle in t.label)

    def test_range_defaults_to_the_branch_not_all_of_history(self):
        self.assertEqual(len(self.timeline), 8)
        self.assertEqual(self.timeline.commits[0].subject, "scaffold auth")

    def test_rename_keeps_one_track(self):
        labels = [t.label for t in self.timeline.track_order()]
        self.assertIn("src/authentication.py", labels)
        self.assertNotIn("src/auth.py", labels)
        self.assertEqual(len(self.timeline.tracks), 4)

    def test_track_reads_its_old_path_before_the_rename(self):
        track = self.track("authentication")
        self.assertEqual(track.path_at(1), "src/auth.py")
        self.assertEqual(track.path_at(7), "src/authentication.py")

    def test_file_at_resolves_across_the_rename(self):
        track = self.track("authentication")
        early = self.timeline.file_at(track.id, 1)
        late = self.timeline.file_at(track.id, 7)
        self.assertIsNotNone(early)
        self.assertIsNotNone(late)
        self.assertNotIn(b"secrets", early)
        self.assertIn(b"secrets", late)

    def test_churn_counts_are_parsed(self):
        self.assertEqual(self.track("test_auth").weight, 13)
        self.assertGreater(self.track("authentication").weight, 20)

    def test_file_inherited_from_base_is_live_before_it_changes(self):
        # src/app.py predates the branch and is only modified at commit 5.
        track = self.track("app.py")
        self.assertTrue(track.preexisting)
        self.assertEqual(track.state_at(0), "live")
        self.assertEqual(track.state_at(7), "live")

    def test_deleted_file_goes_absent_and_stays_absent(self):
        track = self.track("util.py")
        self.assertEqual(track.state_at(5), "live")
        self.assertEqual(track.state_at(6), "deleted")
        self.assertEqual(track.state_at(7), "deleted")
        self.assertIsNone(self.timeline.file_at(track.id, 7))

    def test_new_file_is_absent_before_it_is_added(self):
        track = self.track("test_auth")
        self.assertFalse(track.preexisting)
        self.assertEqual(track.state_at(0), "absent")
        self.assertEqual(track.state_at(2), "live")

    def test_diff_pane_shows_the_rename(self):
        track = self.track("authentication")
        diff = self.timeline.diff_at(track.id, 3)
        self.assertIn("rename from src/auth.py", diff)

    def test_cumulative_pane_collapses_intermediate_churn(self):
        track = self.track("authentication")
        cumulative = self.timeline.cumulative_diff(track.id, 7)
        # The agent rewrote this file four times; against base it is one add.
        self.assertIn("new file", cumulative)
        self.assertNotIn("-    return digest == user.password_hash", cumulative)

    def test_clips_land_on_the_commits_that_touched_them(self):
        track = self.track("app.py")
        self.assertEqual(sorted(track.clips), [5])


if __name__ == "__main__":
    unittest.main(verbosity=2)
