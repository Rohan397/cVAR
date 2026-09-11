"""The ncurses `rep` workaround.

The bug it exists for: ncurses emits only the trailing byte of a multibyte
character before `CSI <n> b`, so a long run of one glyph becomes a run of
replacement characters. It only bites when the terminfo entry in play
advertises `rep` — Apple's xterm-256color does not, terminals that ship their
own database often do.
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cvar import termfix  # noqa: E402


def _terminfo_with_rep(into: Path) -> str | None:
    """Compile a terminal that advertises `rep`, whatever this machine ships."""
    described = subprocess.run(
        ["infocmp", "-x", "xterm-256color"], capture_output=True, text=True
    )
    if described.returncode != 0:
        return None
    source = described.stdout
    if "rep=" not in source:
        source = source.replace(
            "\n\t", "\n\trep=%p1%c\\E[%p2%{1}%-%db,\n\t", 1
        )
    # infocmp leads with a comment line, so the header is not at index 0.
    source = re.sub(
        r"^xterm-256color\|[^,]*,", "cvar-reptest|rep test,", source, count=1, flags=re.M
    )
    if "cvar-reptest" not in source:
        return None
    written = into / "reptest.ti"
    written.write_text(source)
    built = subprocess.run(
        ["tic", "-x", "-o", str(into), str(written)], capture_output=True, text=True
    )
    return "cvar-reptest" if built.returncode == 0 else None


class EnvironmentTest(unittest.TestCase):
    """What disable_rep does to the environment it is handed."""

    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        self._home = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = self.cache.name
        self.addCleanup(self._restore)

    def _restore(self):
        if self._home is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._home

    def test_the_escape_hatch_is_honoured(self):
        env = {"TERM": "xterm-256color", "CVAR_KEEP_REP": "1"}
        self.assertIn("CVAR_KEEP_REP", termfix.disable_rep(env))
        self.assertNotIn("TERMINFO", env)

    def test_a_terminal_with_no_name_is_left_alone(self):
        self.assertIsNotNone(termfix.disable_rep({"TERM": ""}))
        self.assertIsNotNone(termfix.disable_rep({"TERM": "dumb"}))

    def test_an_unknown_terminal_is_reported_not_raised(self):
        reason = termfix.disable_rep({"TERM": "no-such-terminal-anywhere"})
        self.assertIsNotNone(reason)

    def test_a_terminal_without_rep_needs_no_patch(self):
        env = {"TERM": "dumb"}
        self.assertIsNotNone(termfix.disable_rep(env))
        self.assertNotIn("TERMINFO", env)

    def test_patching_claims_terminfo_because_it_outranks_the_dirs(self):
        with tempfile.TemporaryDirectory() as build:
            term = _terminfo_with_rep(Path(build))
            if term is None:
                self.skipTest("infocmp/tic unavailable")
            env = {"TERM": term, "TERMINFO": build}
            self.assertIsNone(termfix.disable_rep(env))
            self.assertEqual(env["TERMINFO"], str(termfix.cache_dir()))

    def test_the_database_it_came_from_stays_reachable(self):
        with tempfile.TemporaryDirectory() as build:
            term = _terminfo_with_rep(Path(build))
            if term is None:
                self.skipTest("infocmp/tic unavailable")
            env = {"TERM": term, "TERMINFO": build}
            termfix.disable_rep(env)
            self.assertIn(build, env["TERMINFO_DIRS"])

    def test_the_second_call_reuses_the_compiled_entry(self):
        with tempfile.TemporaryDirectory() as build:
            term = _terminfo_with_rep(Path(build))
            if term is None:
                self.skipTest("infocmp/tic unavailable")
            first = {"TERM": term, "TERMINFO": build}
            self.assertIsNone(termfix.disable_rep(first))
            # Take the compiler away; a cached entry must not need it.
            second = {"TERM": term, "TERMINFO": build, "PATH": ""}
            self.assertIsNone(termfix.disable_rep(second))


class EmittedBytesTest(unittest.TestCase):
    """The point of all of it: what actually reaches the terminal."""

    @staticmethod
    def _draw(term: str, database: str, keep_rep: bool) -> bytes:
        import pty, time, fcntl, struct, termios

        script = (
            "import curses, locale, sys\n"
            "locale.setlocale(locale.LC_ALL, '')\n"
            "sys.path.insert(0, %r)\n" % str(Path(__file__).resolve().parent.parent) +
            "from cvar import termfix\n"
            "termfix.disable_rep()\n"
            "curses.wrapper(lambda s: (s.addstr(0, 0, '·' * 150), s.refresh()))\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
            handle.write(script)
            path = handle.name

        pid, fd = pty.fork()
        if pid == 0:
            os.environ["TERM"] = term
            os.environ["TERMINFO"] = database
            os.environ.pop("TERMINFO_DIRS", None)
            os.environ["LANG"] = "en_US.UTF-8"
            if keep_rep:
                os.environ["CVAR_KEEP_REP"] = "1"
            os.execv(sys.executable, [sys.executable, path])

        time.sleep(1.2)
        out = b""
        os.set_blocking(fd, False)
        for _ in range(60):
            try:
                out += os.read(fd, 65536)
            except (BlockingIOError, OSError):
                pass
            time.sleep(0.02)
        os.unlink(path)
        return out

    def setUp(self):
        self.build = tempfile.TemporaryDirectory()
        self.addCleanup(self.build.cleanup)
        self.term = _terminfo_with_rep(Path(self.build.name))
        if self.term is None:
            self.skipTest("infocmp/tic unavailable")
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        self._home = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = self.cache.name
        self.addCleanup(
            lambda: os.environ.__setitem__("XDG_CACHE_HOME", self._home)
            if self._home else os.environ.pop("XDG_CACHE_HOME", None)
        )

    def test_a_rep_terminal_mangles_a_long_run_without_the_workaround(self):
        out = self._draw(self.term, self.build.name, keep_rep=True)
        broken = re.findall(rb"(?<![\xc0-\xff])[\x80-\xbf]\x1b\[\d+b", out)
        if not broken:
            self.skipTest("this ncurses does not use rep for the run")
        self.assertTrue(broken, "expected the bug the workaround exists for")

    def test_the_workaround_leaves_every_character_intact(self):
        out = self._draw(self.term, self.build.name, keep_rep=False)
        self.assertEqual(re.findall(rb"\x1b\[\d+b", out), [])
        self.assertEqual(out.count("·".encode()), 150)
        # And the whole stream is still well-formed UTF-8.
        remaining, invalid = out, 0
        while remaining:
            try:
                remaining.decode("utf-8")
                break
            except UnicodeDecodeError as exc:
                invalid += 1
                remaining = remaining[exc.start + max(1, exc.end - exc.start):]
        self.assertEqual(invalid, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
