"""The repo's worktrees, as rows for the overview.

One agent per worktree is the shape this exists for: several sessions editing
the same repository at once, each on its own branch, each with its own
timeline. A worktree is the closest thing git has to a session identity, and
unlike a running process it is discoverable and stable.

This module is data only — it finds the trees and loads a timeline for each.
Drawing them is the TUI's job.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import gitio, watch
from .gitio import GitError
from .model import Timeline

DETACHED = "(detached)"


@dataclass(frozen=True)
class Tree:
    """One checked-out worktree of a repository."""

    path: Path
    head: str
    branch: str
    current: bool  # the tree cvar was pointed at

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


@dataclass
class Row:
    """One tree with its branch loaded — an overview row."""

    tree: Tree
    timeline: Timeline
    # The tip when this row was built, so a cheap poll can tell whether the
    # session behind it has committed since.
    tip: str = ""

    @property
    def label(self) -> str:
        return self.tree.label

    @property
    def id(self) -> str:
        return str(self.tree.path)

    @property
    def weight(self) -> int:
        return sum(t.weight for t in self.timeline.tracks.values())


def rows(repo: Path, rev_range: str | None, limit: int | None) -> list[Row]:
    """Every worktree that has a branch worth showing, in listing order.

    Loaded in parallel. Each tree costs two `git log` passes and a handful of
    rev-parses, all of them waiting on a subprocess rather than holding the
    GIL, so the walk is latency the thread pool simply overlaps — measured at
    3x for four trees, and the gap widens with each one. `map` preserves
    order, so the rows still come back in git's.
    """
    trees_found = discover(repo)
    if not trees_found:
        return []

    def build(tree: Tree) -> Row | None:
        timeline = load(tree, rev_range, limit)
        if timeline is None:
            return None
        return Row(tree=tree, timeline=timeline, tip=watch.tip(tree.path) or "")

    with ThreadPoolExecutor(max_workers=min(8, len(trees_found))) as pool:
        built = pool.map(build, trees_found)
    return [row for row in built if row is not None]


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


# -- overview rows -------------------------------------------------------


def commit_churn(timeline: Timeline) -> list[int]:
    """Lines changed at each commit, summed across every track."""
    totals = [0] * len(timeline)
    for track in timeline.tracks.values():
        for index, clip in track.clips.items():
            totals[index] += clip.weight
    return totals


def column_range(total: int, width: int, column: int) -> range:
    """The commits one column of an overview row stands for.

    Two regimes, one rule. A branch shorter than the grid stretches, each
    commit painted across several columns exactly as the main grid does it. A
    branch longer than the grid compresses, a column standing for the commits
    that fall inside it.

    There is deliberately no third regime where the row scrolls. Rows here are
    different branches of different lengths, and a row that showed only part of
    its branch would make the shapes incomparable — which is the one thing the
    overview is for.
    """
    start = column * total // width
    # The upper bound collapses to the lower one whenever a commit spans more
    # than a column; a column always stands for at least the commit it starts on.
    return range(start, max((column + 1) * total // width, start + 1))


def churn_columns(timeline: Timeline, width: int) -> list[int]:
    """One row's worth of churn, bucketed into `width` columns.

    A column carries the churn of a *typical* commit inside it, not the total.
    Summing would make the rows incomparable, which is the one thing they have
    to be: a branch shorter than the grid stretches, so every column repeats
    one commit's whole weight, while a longer branch compresses several
    commits into each. Under a sum, a one-commit branch reads as heavy as a
    fifty-commit one and the overview says the opposite of the truth.
    """
    total = len(timeline)
    if total == 0 or width <= 0:
        return [0] * max(width, 0)
    churn = commit_churn(timeline)
    columns = []
    for x in range(width):
        covered = column_range(total, width, x)
        columns.append(sum(churn[i] for i in covered) // len(covered))
    return columns


def column_commit(total: int, width: int, column: int) -> int:
    """Which commit to land on when a column is drilled into.

    The newest in the bucket: a column standing for several commits is a
    summary of what happened across them, and the state at the end of it is
    what the reader is being shown.
    """
    if total <= 0:
        return 0
    covered = column_range(total, width, max(0, min(column, width - 1)))
    return min(covered[-1], total - 1)
