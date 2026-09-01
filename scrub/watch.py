"""Cheap detection of new commits.

The obvious check — forking `git rev-parse HEAD` on a timer — costs ~8 ms and
answers nothing 99% of the time. Reading the ref straight off disk answers the
same question in ~12 µs, so the poll can run twice a second and stay invisible
in a process monitor.

Falls back through loose ref, packed refs, then git itself, so a repo that has
been garbage-collected or is a linked worktree still reports correctly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def git_dir(repo: Path) -> Path | None:
    """The .git directory, following the pointer file a worktree leaves."""
    candidate = Path(repo) / ".git"
    if candidate.is_dir():
        return candidate
    try:
        # A linked worktree or submodule has a file here: "gitdir: <path>".
        text = candidate.read_text().strip()
    except OSError:
        return None
    if text.startswith("gitdir:"):
        target = Path(text.split(":", 1)[1].strip())
        return target if target.is_absolute() else (Path(repo) / target).resolve()
    return None


def tip(repo: Path) -> str | None:
    """The sha the current branch points at, read without forking git."""
    base = git_dir(repo)
    if base is None:
        return _ask_git(repo)

    try:
        head = (base / "HEAD").read_text().strip()
    except OSError:
        return _ask_git(repo)

    if not head.startswith("ref:"):
        return head or None  # detached HEAD stores the sha directly

    ref = head[4:].strip()

    try:
        return (base / ref).read_text().strip() or None
    except OSError:
        pass  # not a loose ref — probably packed

    try:
        for line in (base / "packed-refs").read_text().splitlines():
            if line.endswith(f" {ref}"):
                return line.split(maxsplit=1)[0]
    except OSError:
        pass

    return _ask_git(repo)


def _ask_git(repo: Path) -> str | None:
    """Last resort. Correct everywhere, and slow enough to be worth avoiding."""
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None
