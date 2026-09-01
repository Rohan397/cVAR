"""The repo's worktrees, as rows for the overview.

One agent per worktree is the shape this exists for: several sessions editing
the same repository at once, each on its own branch, each with its own
timeline. A worktree is the closest thing git has to a session identity, and
unlike a running process it is discoverable and stable.

This module is data only — it finds the trees and loads a timeline for each.
Drawing them is the TUI's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import gitio
from .gitio import GitError
from .model import Timeline

DETACHED = "(detached)"


@dataclass(frozen=True)
class Tree:
    """One checked-out worktree of a repository."""

    path: Path
    head: str
    branch: str
    current: bool  # the tree scrub was pointed at

    @property
    def label(self) -> str:
        return self.path.name or str(self.path)


def discover(repo: Path) -> list[Tree]:
    """Every checked-out worktree of `repo`, in git's own listing order.

    The main worktree comes first, then the linked ones by name — git's order,
    kept rather than imposed, so the list does not reshuffle under you as tips
    move. Bare and prunable entries are dropped: neither has a working tree to
    scrub.
    """
    try:
        out = gitio.run_text(repo, "worktree", "list", "--porcelain")
    except GitError:
        return []

    here = Path(repo).resolve()
    trees: list[Tree] = []
    for block in out.split("\n\n"):
        fields = _fields(block)
        path = fields.get("worktree")
        if path is None or "bare" in fields or "prunable" in fields:
            continue
        resolved = Path(path).resolve()
        if not resolved.is_dir():
            continue
        branch = fields.get("branch", "")
        trees.append(
            Tree(
                path=resolved,
                head=fields.get("HEAD", ""),
                branch=branch.rsplit("/", 1)[-1] if branch else DETACHED,
                current=resolved == here,
            )
        )
    return trees


def _fields(block: str) -> dict[str, str]:
    """One porcelain record: `key value` lines, plus bare `key` flags."""
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(" ")
        fields[key] = value.strip()
    return fields


def load(tree: Tree, rev_range: str | None, limit: int | None) -> Timeline | None:
    """The timeline for one tree, or None if it has nothing to show.

    A tree whose branch has no commits of its own — freshly created, or sitting
    on the mainline — is not an error worth interrupting the overview for; it
    simply has no row.
    """
    try:
        timeline = Timeline.load(tree.path, rev_range, limit)
    except GitError:
        return None
    if not len(timeline):
        timeline.close()
        return None
    return timeline
