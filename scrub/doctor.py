"""Explain which editor and transport scrub picked, and why.

The handoff has several fallbacks and they are all silent — a diff opening in
the wrong place looks like a bug when it is usually a mismatch between where
nvim is running and which repo is being scrubbed. This prints the decision.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import bridge as _bridge
from .bridge import EditorBridge, _relatedness, nvim_sockets, socket_cwd
from .model import Timeline

TRANSPORTS = {
    "gui": "diff opens in the running VS Code / Cursor window",
    "remote": "diff opens in the nvim already running in another pane",
    "suspend": "scrub yields this terminal to nvim, and takes it back on :qa",
    "none": "no editor available — nothing will open",
}


def _why_editor(editor: str | None) -> str:
    if editor is None:
        return "nothing resolved"
    name = Path(editor).name
    for var in ("SCRUB_EDITOR", "VISUAL", "EDITOR"):
        value = os.environ.get(var, "").split()
        if value and Path(shutil.which(value[0]) or value[0]).name == name:
            return f"from ${var}"
    if os.environ.get("NVIM"):
        return "from $NVIM — scrub is running inside nvim's :terminal"
    if name in _bridge.VIM_FAMILY:
        return "a running nvim was found on this repo"
    return "found by scanning installed editors (no $EDITOR set)"


def report(timeline: Timeline, editor: str | None, server: str | None) -> str:
    made = EditorBridge(timeline, editor, server)
    try:
        repo = Path(timeline.repo).resolve()
        lines = [
            f"repo        {repo}",
            f"editor      {made.editor or '(none)'}   [{_why_editor(made.editor)}]",
            f"transport   {made.mode}   [{TRANSPORTS.get(made.mode, '')}]",
        ]
        if made.server:
            lines.append(f"nvim socket {made.server}")

        probe = shutil.which("nvim")
        lines += ["", "nvim sockets on this machine:"]
        if not probe:
            lines.append("  nvim is not installed")
        else:
            sockets = nvim_sockets()
            if not sockets:
                lines.append("  none — no nvim is running")
            for socket in sockets:
                cwd = socket_cwd(socket, probe)
                if cwd is None:
                    lines.append(f"  {socket.name:<16} (stale — that nvim has exited)")
                    continue
                distance = _relatedness(Path(cwd).resolve(), repo)
                verdict = (
                    "MATCH — this repo"
                    if distance == 0
                    else f"related, {distance} levels away"
                    if distance is not None
                    else "unrelated to this repo"
                )
                lines.append(f"  {socket.name:<16} cwd={cwd}\n  {'':<16} {verdict}")

        lines += ["", _advice(made, repo)]
        return "\n".join(lines)
    finally:
        made.close()


def _advice(made: EditorBridge, repo: Path) -> str:
    if made.mode == "remote":
        return "Ready: press ⏎ and the diff opens in the other pane."
    if made.mode == "suspend":
        return (
            "The diff will open in THIS pane, not the other one.\n"
            "To drive the nvim in your other pane, that nvim has to be running\n"
            f"with this repo as its working directory:\n"
            f"    cd {repo} && nvim ."
        )
    if made.mode == "gui":
        return (
            "Handoffs go to a GUI editor. For nvim instead, either set\n"
            "    export EDITOR=nvim\n"
            f"or open nvim on this repo:  cd {repo} && nvim ."
        )
    return "No editor resolved. Set $EDITOR, or pass --editor."
