"""Work around an ncurses bug that turns long runs of one glyph into "?".

The grid draws hundreds of identical cells in a row — a file that exists but
was untouched is a run of `·` as wide as the branch. ncurses optimises runs
like that with the terminfo `rep` capability: emit the character once, then
`CSI <n> b` to repeat it.

The ncurses Apple ships (`/usr/lib/libncurses.5.4.dylib`, a 2015 snapshot of
6.0) gets this wrong for multibyte characters. It emits only the *trailing*
byte of the UTF-8 sequence before the repeat:

    \\xb7 \\x1b[78b        instead of        \\xc2\\xb7 \\x1b[78b

A lone 0xb7 is not valid UTF-8, so the terminal draws a replacement character
and then dutifully repeats it 78 times. The result looks exactly like a missing
font glyph, which is why it is worth naming: no font change fixes it, and the
same character renders correctly wherever the run is short enough that ncurses
does not reach for `rep`.

Whether it bites depends on the terminfo database in play, not the terminal.
Apple's own entry for xterm-256color has no `rep`, so a plain login shell never
shows this. Terminals that ship their own database — cmux sets
TERMINFO=.../cmux.app/Contents/Resources/terminfo — do advertise `rep`, and
that is where the runs turn to mojibake.

The fix is to take `rep` away. A terminfo entry without it is compiled once
into a cache directory, leaving every other capability — colours, keys, mouse —
exactly as it was. It has to be pointed at with TERMINFO, not just
TERMINFO_DIRS: ncurses checks TERMINFO first, so a terminal that set it would
otherwise go on winning.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_REP = re.compile(r"[ \t]*\brep=[^,]*,")


def cache_dir() -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(root) / "cvar" / "terminfo"


def _compiled(cache: Path, term: str) -> bool:
    """tic files an entry under a directory named for its first character."""
    return any(cache.glob(f"*/{term}"))


def disable_rep(env: dict[str, str] | None = None) -> str | None:
    """Point TERMINFO_DIRS at a copy of this terminal's entry minus `rep`.

    Returns the reason it did nothing, or None when the workaround is in
    force. Never raises: a terminal that cannot be patched is worth degrading
    for, not crashing over.
    """
    env = os.environ if env is None else env

    if env.get("CVAR_KEEP_REP"):
        return "disabled by $CVAR_KEEP_REP"

    term = env.get("TERM", "")
    if not term or term == "dumb":
        return "no TERM to patch"

    cache = cache_dir()
    if _compiled(cache, term):
        _point_at(env, cache)
        return None

    if not (shutil.which("infocmp") and shutil.which("tic")):
        return "infocmp and tic are not installed"

    # The tools read TERM and TERMINFO themselves, so they have to be told the
    # same environment being patched rather than whatever this process holds.
    child = {**os.environ, **env}

    try:
        described = subprocess.run(
            ["infocmp", "-x", term],
            capture_output=True, text=True, timeout=10, env=child,
        )
    except (OSError, subprocess.SubprocessError):
        return "infocmp could not be run"
    if described.returncode != 0:
        return f"no terminfo entry for {term}"

    if "rep=" not in described.stdout:
        return f"{term} does not use rep"

    try:
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".ti", delete=False) as handle:
            handle.write(_REP.sub("", described.stdout))
            source = handle.name
        try:
            built = subprocess.run(
                ["tic", "-x", "-o", str(cache), source],
                capture_output=True, text=True, timeout=30, env=child,
            )
        finally:
            os.unlink(source)
    except (OSError, subprocess.SubprocessError):
        return "tic could not be run"

    if built.returncode != 0 or not _compiled(cache, term):
        return "tic could not compile the patched entry"

    _point_at(env, cache)
    return None


def _point_at(env: dict[str, str], cache: Path) -> None:
    """Make the patched entry win, without hiding the database it came from.

    TERMINFO is checked before TERMINFO_DIRS, so the cache has to claim it.
    Whatever TERMINFO held stays reachable through TERMINFO_DIRS, so a
    terminal that ships its own database keeps it for every other lookup.
    """
    previous = env.get("TERMINFO")
    searched = [str(cache)]
    if previous and previous != str(cache):
        searched.append(previous)
    if env.get("TERMINFO_DIRS"):
        searched.append(env["TERMINFO_DIRS"])
    env["TERMINFO"] = str(cache)
    env["TERMINFO_DIRS"] = ":".join(searched) + ":"
