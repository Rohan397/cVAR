"""Worktree discovery and the overview rows built from it."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrub import trees  # noqa: E402


class DiscoveryTest(unittest.TestCase):
    """Several sessions on one repo: the shape the overview exists for."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "main"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.commit("one")

    def git(self, *args, repo=None):
        subprocess.run(["git", "-C", str(repo or self.repo), *args],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def commit(self, message, repo=None):
        target = repo or self.repo
        (target / f"{message}.py").write_text(f"# {message}\n")
        self.git("add", "-A", repo=target)
        self.git("-c", "user.email=t@e", "-c", "user.name=T",
                 "commit", "-q", "-m", message, repo=target)

    def add_worktree(self, name):
        path = self.root / name
        self.git("worktree", "add", "-q", "-b", name, str(path))
        return path

    def test_a_plain_repo_is_one_tree(self):
        found = trees.discover(self.repo)
        self.assertEqual([t.label for t in found], ["main"])
        self.assertTrue(found[0].current)

    def test_every_worktree_becomes_a_row(self):
        self.add_worktree("agent-auth")
        self.add_worktree("agent-api")
        found = trees.discover(self.repo)
        # The main tree leads; git orders the linked ones by name, and keeping
        # its order means the list holds still while tips move underneath.
        self.assertEqual([t.label for t in found], ["main", "agent-api", "agent-auth"])
        self.assertEqual([t.branch for t in found], ["main", "agent-api", "agent-auth"])

    def test_the_tree_scrub_was_pointed_at_is_the_current_one(self):
        linked = self.add_worktree("agent-auth")
        found = trees.discover(linked)
        current = [t.label for t in found if t.current]
        self.assertEqual(current, ["agent-auth"])

    def test_discovery_from_a_worktree_sees_its_siblings(self):
        self.add_worktree("agent-auth")
        second = self.add_worktree("agent-api")
        self.assertEqual(len(trees.discover(second)), 3)

    def test_a_detached_head_is_labelled_rather_than_dropped(self):
        path = self.root / "loose"
        self.git("worktree", "add", "-q", "--detach", str(path))
        found = {t.label: t.branch for t in trees.discover(self.repo)}
        self.assertEqual(found["loose"], trees.DETACHED)

    def test_a_deleted_worktree_directory_is_not_a_row(self):
        path = self.add_worktree("gone")
        for item in sorted(path.rglob("*"), reverse=True):
            item.unlink() if item.is_file() else item.rmdir()
        path.rmdir()
        self.assertEqual([t.label for t in trees.discover(self.repo)], ["main"])

    def test_a_bare_repo_has_no_rows(self):
        bare = self.root / "bare.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.repo), str(bare)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(trees.discover(bare), [])

    def test_a_directory_that_is_not_a_repo_yields_nothing(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assertEqual(trees.discover(plain), [])

    def test_each_tree_loads_its_own_branch(self):
        linked = self.add_worktree("agent-auth")
        self.commit("two", repo=linked)
        self.commit("three", repo=linked)
        by_label = {t.label: t for t in trees.discover(self.repo)}

        loaded = trees.load(by_label["agent-auth"], None, None)
        self.addCleanup(loaded.close)
        self.assertEqual([c.subject for c in loaded.commits], ["two", "three"])

    def test_a_tree_with_no_commits_of_its_own_has_no_timeline(self):
        linked = self.add_worktree("agent-auth")
        by_label = {t.label: t for t in trees.discover(self.repo)}
        self.assertIsNone(trees.load(by_label["agent-auth"], "main..HEAD", None))


class ColumnTest(unittest.TestCase):
    """Fitting a whole branch into a fixed number of columns, never scrolled."""

    WIDTH = 32

    def cols(self, total):
        return [list(trees.column_range(total, self.WIDTH, x)) for x in range(self.WIDTH)]

    def test_no_commit_is_ever_dropped(self):
        for total in (1, 2, 4, 31, 32, 33, 100, 401):
            covered = sorted({i for c in self.cols(total) for i in c})
            self.assertEqual(covered, list(range(total)), f"total={total}")

    def test_a_short_branch_stretches_one_commit_across_several_columns(self):
        widths = [len(c) for c in self.cols(4)]
        self.assertEqual(set(widths), {1}, "a stretched column stands for one commit")
        self.assertEqual([c[0] for c in self.cols(4)][:9], [0] * 8 + [1])

    def test_a_long_branch_compresses_several_commits_into_one_column(self):
        self.assertEqual(self.cols(100)[0], [0, 1, 2])

    def test_columns_are_contiguous_and_ordered(self):
        for total in (4, 100):
            flat = [i for c in self.cols(total) for i in c]
            self.assertEqual(flat, sorted(flat))

    def test_the_last_column_always_reaches_the_tip(self):
        for total in (1, 4, 32, 100, 401):
            self.assertEqual(
                trees.column_commit(total, self.WIDTH, self.WIDTH - 1), total - 1
            )

    def test_drilling_a_column_lands_on_its_newest_commit(self):
        # Column 0 of a 100-commit branch stands for commits 0-2; the state
        # being summarised is the one at the end of it.
        self.assertEqual(trees.column_commit(100, self.WIDTH, 0), 2)

    def test_a_column_past_the_end_clamps_rather_than_raising(self):
        self.assertEqual(trees.column_commit(10, self.WIDTH, 999), 9)
        self.assertEqual(trees.column_commit(10, self.WIDTH, -5), 0)


class ChurnColumnTest(unittest.TestCase):
    """Churn per column, the value the ramp glyph is chosen from."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        run = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a],
                                        check=True, stdout=subprocess.DEVNULL)
        run("init", "-q", "-b", "main")
        for n, lines in enumerate([1, 20, 3]):
            (self.repo / f"f{n}.py").write_text("x\n" * lines)
            run("add", "-A")
            run("-c", "user.email=t@e", "-c", "user.name=T", "commit", "-q", "-m", f"c{n}")
        from scrub.model import Timeline
        self.timeline = Timeline.load(self.repo)
        self.addCleanup(self.timeline.close)

    def test_per_commit_churn_sums_every_track(self):
        self.assertEqual(trees.commit_churn(self.timeline), [1, 20, 3])

    def test_a_stretched_row_repeats_each_commits_weight(self):
        cols = trees.churn_columns(self.timeline, 30)
        self.assertEqual(cols[:10], [1] * 10)
        self.assertEqual(cols[10:20], [20] * 10)

    def test_a_compressed_row_averages_the_commits_in_the_bucket(self):
        # 1 + 20 + 3 lines over three commits. A sum here would make this
        # branch read heavier than a long one whose commits are all small.
        self.assertEqual(trees.churn_columns(self.timeline, 1), [8])

    def test_a_short_branch_does_not_outweigh_a_long_one(self):
        """The failure that made rows incomparable: stretching inflated churn."""
        short = trees.churn_columns(self.timeline, 60)
        self.assertEqual(max(short), 20, "a stretched column is one commit's weight")

    def test_an_empty_timeline_yields_a_flat_row_rather_than_raising(self):
        class Empty:
            commits: list = []
            tracks: dict = {}
            def __len__(self):
                return 0
        self.assertEqual(trees.churn_columns(Empty(), 4), [0, 0, 0, 0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
