"""Hand a frame off to the editor.

The terminal is the navigator; the editor is the reading surface. Nothing here
tries to render code — it materialises the blobs at the playhead into a temp
directory and asks the editor to show them. Nothing fires until you press a
key, so scanning the grid stays free of editor churn.

Three transports, because editors do not agree on how to be talked to:

    gui      VS Code and friends: a detached CLI call messages a running
             window. The scrubber keeps the terminal.
    remote   nvim with a listening socket: the same idea over RPC. Used
             automatically when scrub runs inside nvim's :terminal, where
             $NVIM points at the parent.
    suspend  Everything else terminal-shaped: drop out of curses, run the
             editor on the terminal, restore the grid when it exits. The
             oldest pattern there is, and it sidesteps focus entirely.
"""

from __future__ import annotations

import contextlib
import getpass
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Iterator

from .model import Timeline

# Editors whose CLI speaks `--diff a b`, `--goto file` and `--reuse-window`.
VSCODE_FAMILY = {"cursor", "code", "code-insiders", "codium", "vscodium", "windsurf"}
# Editors that take `-d left right` for a side-by-side diff.
VIM_FAMILY = {"nvim", "vim", "vi", "gvim", "mvim"}


def stated_editor() -> str | None:
    """The editor the user has actually asked for, or None.

    Configuration only — nothing inferred from what happens to be installed.
    """
    explicit = os.environ.get("SCRUB_EDITOR", "").split()
    if explicit and shutil.which(explicit[0]):
        return shutil.which(explicit[0])

    # Running inside nvim's :terminal is the strongest signal there is.
    if os.environ.get("NVIM") and shutil.which("nvim"):
        return shutil.which("nvim")

    for env in ("VISUAL", "EDITOR"):
        value = os.environ.get(env, "").split()
        if value and shutil.which(value[0]):
            return shutil.which(value[0])
    return None


def installed_editor() -> str | None:
    """Last resort: whatever editor is on the machine."""
    for name in ("cursor", "code", "code-insiders", "codium", "windsurf", "nvim", "vim"):
        found = shutil.which(name)
        if found:
            return found
    return None


def detect_editor() -> str | None:
    return stated_editor() or installed_editor()


def discover_nvim_server(repo: Path, editor: str | None) -> str | None:
    """Find a running nvim that is editing this repo.

    nvim listens on a socket by default — `$TMPDIR/nvim.$USER/*/nvim.<pid>.0` —
    so a sibling terminal pane can drive it with no setup and no config change.

    Sockets are matched on the nvim's working directory rather than taken
    first-come: an unrelated nvim open in another project should never have
    someone's diffs thrown into it.
    """
    if not editor or Path(editor).name not in VIM_FAMILY:
        return None

    sockets = nvim_sockets()

    target = Path(repo).resolve()
    ranked: list[tuple[int, str]] = []
    for socket in sockets:
        cwd = socket_cwd(socket, editor)
        if cwd is None:  # stale socket from an nvim that has exited
            continue
        distance = _relatedness(Path(cwd).resolve(), target)
        if distance is not None:
            ranked.append((distance, str(socket)))

    # Closest wins. Ordering by recency alone would let an nvim opened at $HOME
    # — an ancestor of every repo — claim handoffs meant for a nested project.
    return min(ranked)[1] if ranked else None


def nvim_sockets() -> list[Path]:
    """Every socket nvim has left lying around, newest first.

    nvim listens on one of these by default, so there is normally nothing for
    the user to configure — but stale entries from exited instances linger,
    which is why callers probe before trusting one.
    """
    root = Path(os.environ.get("TMPDIR", "/tmp")) / f"nvim.{getpass.getuser()}"
    try:
        return sorted(
            root.glob("*/nvim.*.0"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return []


def socket_cwd(socket: Path, editor: str) -> str | None:
    """The working directory of the nvim behind a socket, or None if it is dead."""
    try:
        probe = subprocess.run(
            [editor, "--server", str(socket), "--remote-expr", "getcwd()"],
            capture_output=True,
            text=True,
            timeout=5,
            # Without this nvim sees a tty and starts a full TUI — alt-screen,
            # capability queries written straight to the terminal — and stdout
            # comes back as escape sequences instead of the answer. The probe
            # then silently reads as "unrelated" and the whole transport
            # downgrades. Closing stdin is what makes it non-interactive.
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    cwd = probe.stdout.strip()
    if probe.returncode != 0 or not cwd or "\x1b" in cwd:
        return None
    return cwd


def _relatedness(cwd: Path, target: Path) -> int | None:
    """How closely an nvim's working directory matches the repo, or None.

    0 is an exact match; larger numbers are directories further up or down the
    tree. Unrelated paths are not candidates at all.
    """
    if cwd == target:
        return 0
    if cwd in target.parents:  # nvim opened above the repo
        return len(target.parts) - len(cwd.parts)
    if target in cwd.parents:  # nvim opened inside the repo
        return len(cwd.parts) - len(target.parts)
    return None


@contextlib.contextmanager
def _no_suspend() -> Iterator[None]:
    yield


def _diff_line_for(diff: str, file_line: int | None) -> int | None:
    """Where in a unified diff to land, given a line number in the file.

    A zoomed region is a file coordinate, but the buffer being opened is the
    diff itself, so the two do not share a numbering. Walk the hunk headers
    until one covers the wanted line and return its position in the diff text.
    """
    if file_line is None:
        return None
    new_line = 0
    for offset, text in enumerate(diff.splitlines(), start=1):
        if text.startswith("@@"):
            match = re.search(r"\+(\d+)", text)
            if match:
                new_line = int(match.group(1))
                if new_line >= file_line:
                    return offset
        elif text.startswith(("+", " ")) and not text.startswith("+++"):
            new_line += 1
            if new_line >= file_line:
                return offset
    return None


def _to_lua(value: object) -> str:
    """Render a small Python value as a Lua literal.

    Only what a handoff request needs — strings, None, and a flat table. Lua
    has no null, so None becomes `nil`, which reads correctly in a table
    constructor and lets the script test `if request.right then`.
    """
    if value is None:
        return "nil"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, dict):
        fields = ", ".join(f"{k} = {_to_lua(v)}" for k, v in value.items())
        return "{ " + fields + " }"
    raise TypeError(f"cannot render {type(value).__name__} as Lua")


class EditorBridge:
    def __init__(
        self,
        timeline: Timeline,
        editor: str | None = None,
        server: str | None = None,
        suspend: Callable[[], contextlib.AbstractContextManager] | None = None,
    ) -> None:
        self.timeline = timeline
        # An editor named explicitly still has to exist. Resolving it up front
        # means a typo in --editor says so, rather than quietly staging temp
        # files for an editor that will never open them.
        # --editor names one editor and only that one. If it does not resolve,
        # leave it unset so the miss is reported; substituting a different
        # editor for the one that was asked for would hide the typo.
        named = bool(editor)
        stated = shutil.which(editor) if editor else stated_editor()
        self.editor = stated

        # $NVIM is set inside nvim's :terminal and points at the parent's
        # socket. A sibling terminal pane is a separate process, so fall back to
        # finding an nvim already editing this repo.
        self.server = server or os.environ.get("NVIM")

        if self.server is None and not named:
            nvim = shutil.which("nvim")
            # Only go looking when nvim could actually be the answer — a stated
            # preference for something else must not be second-guessed.
            if nvim and (stated is None or Path(stated).name in VIM_FAMILY):
                self.server = discover_nvim_server(timeline.repo, nvim)
                if self.server and stated is None:
                    # An nvim already open on this repo is evidence; Cursor
                    # merely being installed is not. Evidence wins.
                    self.editor = nvim

        if self.editor is None and not named:
            self.editor = installed_editor()
        self.suspend = suspend or _no_suspend
        self._dir = Path(tempfile.mkdtemp(prefix="scrub-"))

    @property
    def available(self) -> bool:
        return self.editor is not None

    @property
    def name(self) -> str:
        return Path(self.editor).name if self.editor else ""

    @property
    def mode(self) -> str:
        if not self.editor:
            return "none"
        if self.name in VSCODE_FAMILY:
            return "gui"
        if self.name in VIM_FAMILY and self.server:
            return "remote"
        return "suspend"

    # -- panes -----------------------------------------------------------

    def open_diff(self, track_id: str, index: int, line: int | None = None) -> str:
        """This commit's change to the track, side by side."""
        timeline = self.timeline
        after = timeline.file_at(track_id, index)
        before = timeline.file_before(track_id, index)
        if after is None and before is None:
            return "nothing to open at this commit"

        before_label = timeline.commits[index - 1].short if index > 0 else "base"
        after_label = timeline.commits[index].short
        return self._show_pair(track_id, index, before, before_label, after, after_label, line)

    def open_cumulative(self, track_id: str, index: int, line: int | None = None) -> str:
        """Everything the branch did to the track, base to playhead."""
        timeline = self.timeline
        after = timeline.file_at(track_id, index)
        before = timeline.file_before(track_id, 0)
        if after is None and before is None:
            return "nothing to open"
        return self._show_pair(
            track_id, index, before, "base", after, timeline.commits[index].short, line
        )

    def open_unified(self, track_id: str, index: int, line: int | None = None) -> str:
        """One buffer: this commit's change as a unified diff.

        Written with a .diff suffix so the editor's own diff syntax colours the
        + and - lines. Side-by-side answers "what do these two revisions look
        like"; this answers "what did this commit do", which is the question
        being asked most of the time.
        """
        text = self.timeline.diff_at(track_id, index)
        if not text.strip():
            return "nothing changed here"
        short = self.timeline.commits[index].short
        source = Path(self._path_at(track_id, index))
        path = self._dir / f"{source.stem}@{short}.diff"
        path.write_text(text)
        return self._show_one(path, _diff_line_for(text, line), short)

    def open_state(self, track_id: str, index: int, line: int | None = None) -> str:
        """The file as it exists at the playhead — a single buffer, no diff."""
        blob = self.timeline.file_at(track_id, index)
        if blob is None:
            return "file does not exist at this commit"
        short = self.timeline.commits[index].short
        path = self._write(self._path_at(track_id, index), short, blob)

        return self._show_one(path, line, short)

    def _show_one(self, path: Path, line: int | None, label: str) -> str:
        mode = self.mode
        if mode == "gui":
            target = f"{path}:{line}" if line else str(path)
            self._detach(["--reuse-window", "--goto", target])
        elif mode == "remote":
            self._to_nvim(path, None, line)
        elif mode == "suspend":
            jump = [f"+{line}"] if line and self.name in VIM_FAMILY else []
            self._blocking(["-R", *jump, str(path)] if self.name in VIM_FAMILY else [str(path)])
        else:
            return f"wrote {path}"
        return f"opened {path.name}"

    # -- plumbing --------------------------------------------------------

    def _show_pair(
        self,
        track_id: str,
        index: int,
        before: bytes | None,
        before_label: str,
        after: bytes | None,
        after_label: str,
        line: int | None = None,
    ) -> str:
        # A file that was added has no left-hand side, and one that was deleted
        # has no right-hand side; an empty buffer is the honest stand-in.
        left = self._write(self._path_at(track_id, max(index - 1, 0)), before_label, before or b"")
        right = self._write(self._path_at(track_id, index), after_label, after or b"")

        mode = self.mode
        if mode == "gui":
            self._detach(["--reuse-window", "--diff", str(left), str(right)])
        elif mode == "remote":
            self._to_nvim(left, right, line)
        elif mode == "suspend":
            # -R: these are throwaway snapshots in a temp dir that is deleted on
            # exit. Editing one looks like editing the file and loses the work.
            jump = [f"+{line}"] if line and self.name in VIM_FAMILY else []
            self._blocking(["-R", "-d", *jump, str(left), str(right)]
                           if self.name in VIM_FAMILY else [str(left), str(right)])
        else:
            return f"wrote {left.name} and {right.name}"
        return f"opened {before_label} ↔ {after_label} in {self.name}"

    def _path_at(self, track_id: str, index: int) -> str:
        track = self.timeline.tracks[track_id]
        return track.path_at(index) or track.label

    def _write(self, repo_path: str, label: str, blob: bytes) -> Path:
        """Stage one revision of a file under a name the editor tab can explain.

        The suffix is preserved so syntax highlighting survives, and the short
        sha rides in the stem so the tab title says where the playhead is.
        """
        source = Path(repo_path)
        target = self._dir / f"{source.stem}@{label}{source.suffix}"
        target.write_bytes(blob)
        return target

    def _detach(self, args: list[str]) -> None:
        assert self.editor
        subprocess.Popen(
            [self.editor, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _to_nvim(self, left: Path, right: Path | None, line: int | None = None) -> None:
        """Drive a running nvim over RPC to display this handoff.

        The behaviour lives in Lua rather than a `--remote-send` keystroke
        string: a keystroke string cannot reuse a tab, restore focus, or tell a
        terminal window from an editor one, and any path it carries has to
        survive two layers of escaping.
        """
        assert self.editor and self.server
        template = (Path(__file__).parent / "nvim_open.lua").read_text()
        request = {
            "left": str(left),
            "right": str(right) if right else None,
            "line": line,
        }
        script = self._dir / "open.lua"
        script.write_text(template.replace("__SCRUB_REQUEST__", _to_lua(request)))

        # luaeval's second argument arrives as `_A`, so the path never has to be
        # quoted inside the Vim expression.
        subprocess.Popen(
            [self.editor, "--server", self.server, "--remote-expr",
             f"luaeval('dofile(_A)', '{script}')"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _blocking(self, args: list[str]) -> None:
        """Give the terminal to the editor until it exits, then take it back."""
        assert self.editor
        with self.suspend():
            subprocess.call([self.editor, *args])

    def close(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)
