"""Thin wrappers over git's plumbing layer.

Everything here talks to machine-oriented commands with stable output formats.
No porcelain parsing: `git log --format` is used only as a field emitter, and the
file-level data comes from --name-status/--numstat with -z framing.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

# The canonical empty tree. Used as the diff base when a timeline starts at the
# repo's root commit and there is no parent to compare against.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Record separator between commits, unit separator between fields. Both are
# control characters git will never emit inside a subject or a path.
RS = "\x1e"
US = "\x1f"


class GitError(RuntimeError):
    pass


def run(repo: Path, *args: str) -> bytes:
    """Run a git command in `repo` and return raw stdout."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        cmd = " ".join(args)
        raise GitError(f"git {cmd} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def run_text(repo: Path, *args: str) -> str:
    return run(repo, *args).decode(errors="replace")


def is_repo(repo: Path) -> bool:
    try:
        return run_text(repo, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except GitError:
        return False


class CatFileBatch:
    """A long-lived `git cat-file --batch` process.

    This is the scrub-speed read path: one fork for the whole session, then
    request/response over pipes. Forking a `git show` per frame is what makes
    naive timeline UIs feel sluggish.
    """

    def __init__(self, repo: Path) -> None:
        self._proc = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        self._lock = threading.Lock()

    def read(self, spec: str) -> bytes | None:
        """Resolve a rev-spec (e.g. "<sha>:path/to/file") to its blob contents.

        Returns None if the object does not exist at that revision.
        """
        assert self._proc.stdin and self._proc.stdout
        with self._lock:
            self._proc.stdin.write(spec.encode() + b"\n")
            self._proc.stdin.flush()

            header = self._proc.stdout.readline().decode(errors="replace").strip()
            if not header:
                raise GitError("cat-file --batch closed unexpectedly")
            parts = header.split()
            if len(parts) < 3 or parts[-1] in ("missing", "ambiguous"):
                return None

            size = int(parts[2])
            buf = bytearray()
            while len(buf) < size:
                chunk = self._proc.stdout.read(size - len(buf))
                if not chunk:
                    raise GitError("cat-file --batch truncated")
                buf.extend(chunk)
            self._proc.stdout.read(1)  # trailing newline git appends
            return bytes(buf)

    def close(self) -> None:
        if self._proc.poll() is None and self._proc.stdin:
            self._proc.stdin.close()
            self._proc.wait(timeout=5)
        for stream in (self._proc.stdin, self._proc.stdout):
            if stream is not None and not stream.closed:
                stream.close()

    def __enter__(self) -> CatFileBatch:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def resolve_range(repo: Path, rev_range: str | None, limit: int | None) -> list[str]:
    """Turn a range spec into concrete rev-list arguments.

    With no explicit range, prefer the merge-base against a mainline branch so
    the timeline covers "what this branch did" rather than all of history.
    """
    if rev_range:
        return [rev_range]

    head = run_text(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    for candidate in ("main", "master", "develop"):
        if candidate == head:
            continue
        try:
            run_text(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}")
        except GitError:
            continue
        try:
            base = run_text(repo, "merge-base", "HEAD", candidate).strip()
        except GitError:
            continue
        if base and base != run_text(repo, "rev-parse", "HEAD").strip():
            return [f"{base}..HEAD"]

    # No mainline to diff against (or we are on it): fall back to recent history.
    return ["HEAD"] if limit else ["HEAD", "--max-count=200"]


def log_records(repo: Path, revs: list[str], limit: int | None, extra: list[str]) -> list[tuple[bytes, bytes]]:
    """One `git log` pass over the whole timeline.

    Returns (metadata_bytes, payload_bytes) per commit, oldest first. `extra`
    selects the per-file payload (--name-status or --numstat).
    """
    fmt = f"{RS}%H{US}%h{US}%an{US}%aI{US}%P{US}%s"
    args = ["log", "--reverse", "--root", "-M", "-z", f"--format={fmt}", *extra]
    if limit:
        args.append(f"--max-count={limit}")
    args += revs

    out = run(repo, *args)
    records: list[tuple[bytes, bytes]] = []
    for raw in out.split(RS.encode())[1:]:
        # The metadata line ends at the first NUL or newline; everything after
        # is the NUL-framed per-file payload for this commit.
        cut = len(raw)
        for sep in (b"\x00", b"\n"):
            found = raw.find(sep)
            if found != -1:
                cut = min(cut, found)
        # git separates the format line from the -z payload with a newline as
        # well as the NUL, so the payload needs both trimmed off the front or
        # the first token arrives as "\nA" instead of "A".
        records.append((raw[:cut], raw[cut + 1 :].lstrip(b"\n")))
    return records


def tokens(payload: bytes) -> list[str]:
    """Split a -z payload into non-empty NUL-delimited tokens."""
    return [t.decode(errors="replace") for t in payload.split(b"\x00") if t]
