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


if __name__ == "__main__":
    unittest.main(verbosity=2)
