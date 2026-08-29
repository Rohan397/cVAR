"""The timeline model.

Vocabulary, borrowed from a video editor:

    timeline  the commit sequence, oldest -> newest (the x-axis)
    track     one logical file, followed across renames (the y-axis)
    clip      one file's change at one commit (a cell in the grid)
    playhead  the commit index currently in view

A track is a *logical* file identity, not a path. When an agent renames or moves
a file mid-branch the track survives; that is the whole point, since refactors
are exactly where a path-keyed view falls apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal

from . import gitio
from .gitio import EMPTY_TREE, US, CatFileBatch, GitError

ChangeKind = Literal["A", "M", "D", "R", "C", "T"]
TrackState = Literal["absent", "live", "deleted"]


@dataclass(frozen=True)
class Commit:
    index: int
    sha: str
    short: str
    author: str
    when: str  # ISO-8601, as git emitted it
    parents: tuple[str, ...]
    subject: str


@dataclass(frozen=True)
class Clip:
    """One track's change at one commit."""

    track_id: str
    commit_index: int
    path: str  # the path as of this commit (post-rename)
    old_path: str | None
    kind: ChangeKind
    added: int = 0
    deleted: int = 0

    @property
    def weight(self) -> int:
        return self.added + self.deleted


@dataclass
class Track:
    id: str
    label: str  # most recent path, for display
    clips: dict[int, Clip] = field(default_factory=dict)
    # (commit_index, path) transitions, oldest first.
    renames: list[tuple[int, str]] = field(default_factory=list)

    @property
    def first_index(self) -> int:
        return min(self.clips)

    @property
    def last_index(self) -> int:
        return max(self.clips)

    @property
    def weight(self) -> int:
        return sum(c.weight for c in self.clips.values())

    def path_at(self, index: int) -> str | None:
        """The path this file occupied at `index`, or None if it did not exist."""
        if not self.renames:
            return None
        current = None
        for at, path in self.renames:
            if at > index:
                break
            current = path
        # Before the first recorded transition the file was already on disk at
        # whatever path it started under.
        return current if current is not None else self.renames[0][1]

    @property
    def preexisting(self) -> bool:
        """Whether the file already existed when the timeline started.

        Only an add or a copy creates a file; a track whose first clip is a
        modify, delete or rename was inherited from the base revision and must
        render as live before that point rather than as empty space.
        """
        return self.clips[self.first_index].kind not in ("A", "C")

    def state_at(self, index: int) -> TrackState:
        prior = [i for i in self.clips if i <= index]
        if not prior:
            return "live" if self.preexisting else "absent"
        return "deleted" if self.clips[max(prior)].kind == "D" else "live"


class Timeline:
    """A loaded branch: commits, tracks, and the read path into git objects."""

    def __init__(self, repo: Path, commits: list[Commit], tracks: dict[str, Track], base_sha: str) -> None:
        # Callers reasonably pass a string; the annotation is only a promise
        # unless it is enforced here, and .resolve() downstream depends on it.
        self.repo = Path(repo)
        self.commits = commits
        self.tracks = tracks
        self.base_sha = base_sha
        self._cat = CatFileBatch(repo)

    # -- construction ----------------------------------------------------

    @classmethod
    def load(cls, repo: Path, rev_range: str | None = None, limit: int | None = None) -> Timeline:
        if not gitio.is_repo(repo):
            where = Path(repo).resolve()
            raise GitError(
                f"{where} is not a git work tree.\n"
                f"       There is no timeline without commits — run `git init` there, "
                f"or point scrub at a repo: python3 -m scrub /path/to/repo"
            )

        revs = gitio.resolve_range(repo, rev_range, limit)
        status_records = gitio.log_records(repo, revs, limit, ["--name-status"])
        numstat_records = gitio.log_records(repo, revs, limit, ["--numstat"])

        commits: list[Commit] = []
        tracks: dict[str, Track] = {}
        # Live path -> track id. Rebuilt as we walk forward through history.
        by_path: dict[str, str] = {}
        # Paths that were deleted, kept so a re-added file rejoins its old track.
        buried: dict[str, str] = {}

        for index, ((meta, status_payload), (_, numstat_payload)) in enumerate(
            zip(status_records, numstat_records)
        ):
            commits.append(_parse_commit(index, meta))
            stats = _parse_numstat(gitio.tokens(numstat_payload))

            for kind, old_path, path in _parse_name_status(gitio.tokens(status_payload)):
                track_id = _claim_track(tracks, by_path, buried, kind, old_path, path, index)
                track = tracks[track_id]
                if kind == "R" and old_path and not track.renames:
                    # The rename is the first we have seen of this file, so it
                    # entered the range already living at its old path.
                    track.renames.append((0, old_path))
                if not track.renames or track.renames[-1][1] != path:
                    track.renames.append((index, path))
                track.label = path
                added, deleted = stats.get(path, (0, 0))
                track.clips[index] = Clip(
                    track_id=track_id,
                    commit_index=index,
                    path=path,
                    old_path=old_path,
                    kind=kind,
                    added=added,
                    deleted=deleted,
                )

        base_sha = _base_of(repo, commits)
        return cls(repo, commits, tracks, base_sha)

    # -- queries ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self.commits)

    def track_order(self) -> list[Track]:
        """Tracks in reading order: when they first appear, then by churn."""
        return sorted(self.tracks.values(), key=lambda t: (t.first_index, -t.weight, t.label))

    def active_tracks(self, index: int) -> list[Track]:
        return [t for t in self.track_order() if t.state_at(index) != "absent"]

    def clips_at(self, index: int) -> list[Clip]:
        return [t.clips[index] for t in self.track_order() if index in t.clips]

    def sha_at(self, index: int) -> str:
        return self.commits[index].sha

    def file_at(self, track_id: str, index: int) -> bytes | None:
        """Contents of a track's file as of the playhead — the 'state' pane."""
        track = self.tracks[track_id]
        if track.state_at(index) != "live":
            return None
        path = track.path_at(index)
        if path is None:
            return None
        return self._cat.read(f"{self.sha_at(index)}:{path}")

    def file_before(self, track_id: str, index: int) -> bytes | None:
        """Contents of a track's file just *before* the commit at `index`.

        At the head of the timeline this reaches back to the base revision, so
        the first commit still has something to diff against.
        """
        if index > 0:
            return self.file_at(track_id, index - 1)
        track = self.tracks[track_id]
        if not track.preexisting:
            return None
        path = track.path_at(0)
        return self._cat.read(f"{self.base_sha}:{path}") if path else None

    def diff_at(self, track_id: str, index: int) -> str:
        """This commit's change to the track — the 'diff' pane."""
        track = self.tracks[track_id]
        clip = track.clips.get(index)
        if clip is None:
            return ""
        paths = [clip.path] + ([clip.old_path] if clip.old_path else [])
        return gitio.run_text(
            self.repo,
            "diff-tree", "-p", "-M", "--root", "--no-commit-id",
            self.sha_at(index), "--", *paths,
        )

    def cumulative_diff(self, track_id: str, index: int) -> str:
        """Everything this branch did to the track up to the playhead.

        This is usually the more useful of the two when reviewing an agent: it
        skips the intermediate churn where the agent wrote something and then
        rewrote it two commits later.
        """
        track = self.tracks[track_id]
        paths = {path for at, path in track.renames if at <= index}
        if not paths:
            paths.add(track.label)
        return gitio.run_text(
            self.repo,
            "diff", "-M", self.base_sha, self.sha_at(index), "--", *sorted(paths),
        )

    def close(self) -> None:
        self._cat.close()

    def __enter__(self) -> Timeline:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# -- parsing helpers -----------------------------------------------------


def _parse_commit(index: int, meta: bytes) -> Commit:
    fields = meta.decode(errors="replace").split(US)
    fields += [""] * (6 - len(fields))
    sha, short, author, when, parents, subject = fields[:6]
    return Commit(
        index=index,
        sha=sha,
        short=short,
        author=author,
        when=when,
        parents=tuple(p for p in parents.split() if p),
        subject=subject,
    )


def _parse_name_status(toks: list[str]) -> Iterator[tuple[ChangeKind, str | None, str]]:
    """Walk a `--name-status -z` token stream.

    Renames and copies carry a similarity score (R100) and consume two paths;
    everything else consumes one.
    """
    i = 0
    while i < len(toks):
        raw = toks[i]
        i += 1
        kind = raw[0]
        if kind in ("R", "C"):
            if i + 1 >= len(toks):
                break
            old_path, path = toks[i], toks[i + 1]
            i += 2
            yield kind, old_path, path  # type: ignore[misc]
        else:
            if i >= len(toks):
                break
            path = toks[i]
            i += 1
            yield (kind if kind in "AMDT" else "M"), None, path  # type: ignore[misc]


def _parse_numstat(toks: list[str]) -> dict[str, tuple[int, int]]:
    """Walk a `--numstat -z` token stream into {path: (added, deleted)}.

    Normal entries pack the path into the same token as the counts. Renames
    leave that field empty and follow with two separate path tokens.
    """
    stats: dict[str, tuple[int, int]] = {}
    i = 0
    while i < len(toks):
        parts = toks[i].split("\t")
        i += 1
        if len(parts) < 3:
            continue
        added = int(parts[0]) if parts[0].isdigit() else 0  # "-" for binary files
        deleted = int(parts[1]) if parts[1].isdigit() else 0
        if parts[2]:
            stats[parts[2]] = (added, deleted)
        elif i + 1 < len(toks):
            # Rename: the counts stand alone and the next two tokens are the
            # old and new paths. Key on the new one, which is what clips carry.
            stats[toks[i + 1]] = (added, deleted)
            i += 2
    return stats


def _claim_track(
    tracks: dict[str, Track],
    by_path: dict[str, str],
    buried: dict[str, str],
    kind: ChangeKind,
    old_path: str | None,
    path: str,
    index: int,
) -> str:
    """Resolve which track a change belongs to, creating one if needed."""
    if kind in ("R", "C") and old_path:
        track_id = by_path.pop(old_path, None) if kind == "R" else by_path.get(old_path)
        if track_id is None:
            track_id = _new_track(tracks, path, index)
        by_path[path] = track_id
        return track_id

    track_id = by_path.get(path) or buried.pop(path, None)
    if track_id is None:
        track_id = _new_track(tracks, path, index)

    if kind == "D":
        by_path.pop(path, None)
        buried[path] = track_id
    else:
        by_path[path] = track_id
    return track_id


def _new_track(tracks: dict[str, Track], path: str, index: int) -> str:
    track_id = path
    suffix = 2
    while track_id in tracks:
        track_id = f"{path}#{suffix}"
        suffix += 1
    tracks[track_id] = Track(id=track_id, label=path)
    return track_id


def _base_of(repo: Path, commits: list[Commit]) -> str:
    """The revision the timeline is measured against."""
    if not commits:
        return EMPTY_TREE
    parents = commits[0].parents
    return parents[0] if parents else EMPTY_TREE
