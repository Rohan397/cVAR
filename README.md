# cVAR

Scrub a git branch the way you scrub a video timeline. Built for reviewing what
a coding agent did to a repo, one small commit at a time.

Agents produce plausible code faster than anyone can read it — internally
consistent, well formatted, and occasionally wrong in ways a diff hides. cVAR
is the review room: step through a branch commit by commit, see which files
churned and when, and hand any frame to your editor to actually read.

The command is `scrub`, because that is the verb.

Stdlib-only Python, no dependencies. Backend is git's plumbing layer.
MIT licensed.

## Model

| Video editor | Here |
| --- | --- |
| timeline | the commit sequence, oldest → newest |
| track | one logical file, followed across renames |
| clip | one file's change at one commit |
| playhead | the commit index currently in view |

A **track is a file identity, not a path**. When a branch renames or moves a
file, the track survives and `file_at()` transparently reads whichever path was
in effect at that point on the timeline. Refactors are exactly where a
path-keyed view falls apart, so this is the load-bearing decision.

Following one file is *soloing a track*, not a separate mode.

## Try it

```sh
python3 -m scrub /path/to/repo                # interactive scrubber
python3 -m scrub . --range main..HEAD
python3 -m scrub . --grid                     # print once and exit
```

| key | |
| --- | --- |
| `←` `→` | move the playhead one commit |
| `↑` `↓` | select a track |
| `[` `]` | jump to the previous/next commit that touched this track |
| `g` `G` | jump to the start/end of the branch |
| `f` | solo the selected track (following one file is soloing, not a mode) |
| `⏎` | open this commit's diff in the editor |
| `s` | open the file as it exists at the playhead |
| `c` | open the cumulative diff, base → playhead |
| `q` | quit |

The timeline stretches to fill the terminal, so a short branch spreads into
wide clips rather than huddling in the left corner. Only when commits outnumber
columns does it fall back to one column each and scroll.

```
scrub  4/8 commits · 4 tracks · 41 lines

src/authentication.py  ▓▓▓▓▓▓▓▓▓██████████·········░░░░░░░░░░██████████···················██████████
tests/test_auth.py                        █████████·······································██████████
src/app.py             ················································▓▓▓▓▓▓▓▓▓····················
src/util.py            ·························································××××××××××
                       ┼────────┼─────────┼────────┼───▼─────┼─────────┼────────┼─────────┼─────────
commit                 c4e2128  rename auth -> authentication                   Fixture · 2026-08-15
                       R  src/authentication.py  +0 −0  ← src/auth.py
```

The bottom track is the commit under the playhead, given the whole width — a
message sliced into per-commit cells is unreadable, and the one you are parked
on is the one you want. `▼` in the ruler is what marks position instead, with
the selected track's clip detail directly beneath.

## Colour

| | | |
| --- | --- | --- |
| `░` | dim cyan | lightest churn |
| `▒` | cyan | |
| `▓` | yellow | |
| `█` | bold yellow | heaviest churn |
| `×` | bold magenta | deleted |
| `·` ` ` | dim | untouched / absent |

Churn rides the **blue-yellow axis**, which survives both deuteranopia and
protanopia, and climbs in luminance as well as hue so the ramp still reads in
greyscale. Green is avoided entirely — it would sit beside the red of a
deletion and collapse into it for the most common forms of colour blindness, so
deletion takes magenta, the one hue distinct from both ends of the axis.

The glyphs `░▒▓█` encode magnitude on their own. Colour is reinforcement, never
the only channel carrying the signal.

## Terminal navigates, editor reads

The terminal is the navigator; the IDE is the reading surface. A TUI that
reimplements a code viewer loses to your editor on syntax highlighting,
go-to-definition and your own keybindings. What the editor is bad at is showing
the shape of a branch over time, which is exactly what the grid is for.

So nothing opens until you press a key. Scanning the grid never touches the
editor; `⏎`, `s` and `c` hand the current frame over. The bridge stages the
blobs at the playhead into a temp dir as `name@<short-sha>.py` — suffix
preserved so highlighting works, sha in the stem so the tab says where the
playhead is — then uses whichever transport the editor understands.

### Transports

Editors do not agree on how to be talked to, so there are three.

**`gui`** — VS Code, Cursor and friends. A detached CLI call messages the
running window and the scrubber keeps the terminal:

```sh
cursor --reuse-window --diff  name@c4e2128.py  name@665c392.py
```

**`suspend`** — the default for nvim and vim. Scrub drops out of curses, hands
the terminal to `nvim -d left right`, and redraws the grid when you `:qa`. No
second window, no socket, no focus fight — the oldest pattern in terminal
tooling and the one that needs no setup:

```sh
export EDITOR=nvim
python3 -m scrub /path/to/repo     # ⏎ opens nvim diff, :qa returns to the grid
```

**`remote`** — nvim over RPC, for a persistent side-by-side. This is the mode
for the two-pane layout: nvim in one terminal pane, scrub in another. **No
setup and no flags** — nvim already listens on a socket by default, so scrub
finds the one editing this repo:

```sh
nvim .                       # pane 1, exactly as you already start it
python3 -m scrub .           # pane 2
```

Discovery globs `$TMPDIR/nvim.$USER/*/nvim.<pid>.0` and asks each live nvim for
its `getcwd()`, then ranks by how closely that matches the repo. Ranking, not
first-match: an nvim opened at `$HOME` is an ancestor of every project and
would otherwise swallow handoffs meant for a nested one. Unrelated nvims are
never candidates. `--nvim-server <path>` overrides; `$NVIM` is used when scrub
runs inside nvim's own `:terminal`.

Each handoff reuses **one tab** rather than opening a new one — thirty presses
of `⏎` leave one diff tab, not thirty — and the rest of the layout is
untouched. The split is `vertical rightbelow`, or the user's `splitright`
setting would decide which revision lands where and silently invert the diff.

The behavior lives in `scrub/nvim_open.lua`, rewritten with the current request
and executed over RPC. A `--remote-send` keystroke string cannot reuse a tab,
restore focus, or tell a terminal window from an editor one.

> On macOS a unix socket path is capped at 104 bytes. If you pass
> `--nvim-server` explicitly, keep it short — `/tmp/…`, not somewhere deep.

### Which editor

Configuration beats evidence beats installed software:

1. `$SCRUB_EDITOR`
2. `$NVIM` — you are inside nvim's `:terminal` already
3. `$VISUAL` / `$EDITOR`
4. **a live nvim editing this repo** — found by socket discovery
5. a scan for `cursor`, `code`, …, `nvim`, `vim`

Step 4 is what makes the two-pane workflow work with no configuration. Having
Cursor installed says nothing about *this* repo; an nvim already open on it
does, so evidence outranks the scan — but never a stated `$EDITOR`, which is
left alone. `--editor` overrides everything, and a `--editor` that does not
resolve is reported rather than silently substituted.

## Panes

Three views of the same track, all anchored to the playhead:

- `diff` (`⏎`) — what this one commit did to the file.
- `state` (`s`) — the file as it exists at the playhead. The video-editor
  default: you see the frame, not the delta.
- `cumulative` (`c`) — everything the branch did to this file from base to
  playhead. Usually the most useful for review, since it skips the churn where
  an agent wrote something and rewrote it three commits later.

## Ranges

With no `--range`, the timeline is `merge-base(HEAD, main)..HEAD` — what *this
branch* did, rather than all of history. Falls back to recent `HEAD` history if
there is no mainline to measure against.

## Performance

Measured on a 400-commit branch touching 41 files:

| | |
| --- | --- |
| timeline load | ~180 ms (two `git log` passes, whole timeline) |
| state pane | ~0.1 ms/frame (long-lived `git cat-file --batch`) |
| diff pane | ~9 ms (forks `git diff-tree`) |

The state pane is the scrub path and is effectively free. The diff pane forks
per request and is the thing to cache or prefetch when the TUI lands.

## Layout

    scrub/gitio.py   plumbing wrappers, CatFileBatch, -z parsing
    scrub/model.py   Commit, Clip, Track, Timeline
    scrub/tui.py     the curses scrubber
    scrub/bridge.py  editor handoff, nvim socket discovery
    scrub/nvim_open.lua  what the RPC runs inside nvim
    scrub/render.py  static ASCII grid for --grid and pipes
    scrub/cli.py     python -m scrub
    tests/           make_fixture.py builds an agent-shaped repo

## Tests

```sh
python3 tests/test_timeline.py   # model, 15 tests
python3 tests/test_tui.py        # navigation, layout, bridges, rendering, 59
```

`test_tui.py` uses a fake editor that records its argv rather than opening
anything, launches the real TUI in a pty and drives it with keystrokes, and
asserts on the drawn grid by reading the curses window back with `instr()`.
That readback matters: ncurses compresses runs of identical cells with the REP
escape, so scraping the terminal stream gives a false picture of the grid.

## Not built yet

- Working-tree snapshots. Deliberately deferred — commits only, so the timeline
  stays legible.
- Anchor-follows-code. Scrubbing currently holds a file, not a function.
- Diff-pane prefetch. `diff_at` forks git per call (~9 ms); fine on demand,
  worth caching if the handoff ever becomes automatic.
