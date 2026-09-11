# cVAR

Scrub a git branch the way you scrub a video timeline. Built for reviewing what
a coding agent did to a repo, one small commit at a time.

Agents produce plausible code faster than anyone can read it — internally
consistent, well formatted, and occasionally wrong in ways a diff hides. cVAR
is the review room: step through a branch commit by commit, see which files
churned and when, and hand any frame to your editor to actually read.

The command is `cvar`. Scrubbing is the verb; cVAR is the thing.

Stdlib-only Python, no dependencies. Backend is git's plumbing layer.
MIT licensed.

## Model

| Video editor | Here |
| --- | --- |
| timeline | the commit sequence, oldest → newest |
| track | one logical file, followed across renames |
| clip | one file's change at one commit |
| playhead | the commit index currently in view |
| sequence | one worktree — a whole branch, collapsed to one row |

A **track is a file identity, not a path**. When a branch renames or moves a
file, the track survives and `file_at()` transparently reads whichever path was
in effect at that point on the timeline. Refactors are exactly where a
path-keyed view falls apart, so this is the load-bearing decision.

Following one file is *soloing a track*, not a separate mode.

## Try it

```sh
cvar /path/to/repo                # interactive scrubber
cvar . --range main..HEAD
cvar . --grid                     # print once and exit
cvar . --no-trees                 # skip the worktree overview
```

| key | |
| --- | --- |
| `←` `→` | move the playhead one commit |
| `↑` `↓` | select a track |
| `[` `]` | jump to the previous/next commit that touched this track |
| `g` `G` | jump to the start/end of the branch |
| `f` | solo the selected track (following one file is soloing, not a mode) |
| `w` | step up to the worktrees, or back down |
| `⏎` | open the default pane — `i` unless you rebound it |
| `i` | split: base on the left, the playhead on the right |
| `c` | split: the previous commit on the left, the playhead on the right |
| `ui` `uc` | the same two spans as one unified buffer instead of a split |
| `s` | open the file as it exists at the playhead |
| `e` | jump to the tip and open the real file, editable |
| `q` | quit |

The timeline stretches to fill the terminal, so a short branch spreads into
wide clips rather than huddling in the left corner. Only when commits outnumber
columns does it fall back to one column each and scroll.

```
cvar  4/8 commits · 4 tracks · 41 lines

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

## Several sessions at once

One agent per worktree is the layout this is built around, and a worktree is
the closest thing git has to a session identity — discoverable and stable,
unlike a running process. When a repo has more than one, cvar opens on the
trees instead of the grid, because *which session* is then the first question:

```
cvar  4 worktrees · 19 commits · 351 lines

coding-experience  [main]        ::::::::::::::::*****:::::·····*****::::::::::*****
agent-api          [agent-api]   #################*****************#################
agent-auth*        [agent-auth]  ###########**********##########::::::::::**********
agent-docs         [agent-docs]  ***************************************************
                                 ──────────────────────────────────────────────────▼
agent-auth · 5 commits · 3 files · 157 lines · e00df21 tighten the clock skew window
```

`⏎` opens the selected tree's own grid, landing the playhead on the commit the
column cursor was standing on — drill into a busy stretch and you arrive at
that stretch, not at the tip. `w` steps back up. `*` marks the tree you are in.

**Each row is its own branch, stretched to the full width.** Column 10 of one
row and column 10 of another are unrelated commits: the trees are on different
branches of different lengths and share no clock, so there is no honest way to
put them on one axis. The ruler is unlabelled for that reason and the detail
line names the actual commit under the cursor. A row is never scrolled — a
branch showing only part of itself would make the shapes incomparable, which is
the one thing they exist to be.

**A column carries the churn of a typical commit in it, not the total.** Under
a sum, a one-commit branch — whose single weight repeats across every stretched
column — reads as heavier than a fifty-commit one, which is exactly backwards.

Every tree is polled while the overview is up, not just the one you launched
in, so work landing in another session shows up where it landed: the status bar
says `agent-api +2`, and distinguishes an amend or rebase, where a tip moves
without the count rising, from new commits.

`--trees` and `--no-trees` override the choice of opening view in both
directions.

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
editor; `⏎`, `s` and the span keys hand the current frame over. The bridge stages the
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
cvar /path/to/repo     # ⏎ opens nvim diff, :qa returns to the grid
```

**`remote`** — nvim over RPC, for a persistent side-by-side. This is the mode
for the two-pane layout: nvim in one terminal pane, cvar in another. **No
setup and no flags** — nvim already listens on a socket by default, so cvar
finds the one editing this repo:

```sh
nvim .                       # pane 1, exactly as you already start it
cvar .                       # pane 2
```

Discovery globs `$TMPDIR/nvim.$USER/*/nvim.<pid>.0` and asks each live nvim for
its `getcwd()`, then ranks by how closely that matches the repo. Ranking, not
first-match: an nvim opened at `$HOME` is an ancestor of every project and
would otherwise swallow handoffs meant for a nested one. Unrelated nvims are
never candidates. `--nvim-server <path>` overrides; `$NVIM` is used when cvar
runs inside nvim's own `:terminal`.

Each handoff reuses **one tab** rather than opening a new one — thirty presses
of `⏎` leave one diff tab, not thirty — and the rest of the layout is
untouched. The split is `vertical rightbelow`, or the user's `splitright`
setting would decide which revision lands where and silently invert the diff.

The behavior lives in `cvar/nvim_open.lua`, rewritten with the current request
and executed over RPC. A `--remote-send` keystroke string cannot reuse a tab,
restore focus, or tell a terminal window from an editor one.

> On macOS a unix socket path is capped at 104 bytes. If you pass
> `--nvim-server` explicitly, keep it short — `/tmp/…`, not somewhere deep.

### Which editor

Configuration beats evidence beats installed software:

1. `$CVAR_EDITOR`
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

Every pane is two independent choices, so the keys are a grid rather than a
list. **Span** — how far back the left-hand side reaches — and **layout** —
one buffer or two.

| | split (two buffers) | unified (one buffer) |
| --- | --- | --- |
| **base → playhead** | `i` | `ui` |
| **previous commit → playhead** | `c` | `uc` |

`u` alone arms the span question rather than answering it; `i` or `c` finishes
it. A mistyped second key is swallowed and reported, never quietly routed to
some other pane.

Base is where the branch forked — the parent of the oldest commit in the range.
**`i` is the default**, on `⏎` too, because it is the most useful for review:
it skips the churn where an agent wrote something and rewrote it three commits
later. `c` is for when you want that churn, which is when you are asking what
one specific commit was thinking.

The odd one out is `state` (`s`) — the file as it exists at the playhead, no
diff at all. The video-editor default: you see the frame, not the delta.

A base-to-playhead span is rarely empty, but a single commit usually leaves any
given file alone; when it does, `c` and `uc` open the file at the playhead
instead of refusing.

Every pane stages a copy of the revision under a temp path and opens it
read-only: they are for reading, and a buffer you can type into but never save
is a trap.

`e` is the exception, and the way out of the review room. It moves the playhead
to the tip, then opens the **actual file in the working tree**, unlocked — so
the thing you were reading becomes the thing you are editing without leaving
the grid. The jump to the tip is not a convenience: the working tree is the
newest revision, so editing against anything else would put the buffer and the
timeline on different versions of the file. It also makes a zoomed region's
line number mean the same thing in both, which is why `e` from a chunk lands on
that chunk.

A file the branch deleted says so rather than opening an empty buffer.

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
| trees overview | ~75 ms for four worktrees, loaded in parallel |
| watching four trees | ~0.9 ms/second (one ref read per tree, twice a second) |

The state pane is the scrub path and is effectively free. The diff pane forks
per request and is the thing to cache or prefetch when the TUI lands.

## Layout

    cvar/gitio.py   plumbing wrappers, CatFileBatch, -z parsing
    cvar/model.py   Commit, Clip, Track, Timeline
    cvar/trees.py   worktree discovery, one row per session
    cvar/tui.py     the curses scrubber
    cvar/bridge.py  editor handoff, nvim socket discovery
    cvar/nvim_open.lua  what the RPC runs inside nvim
    cvar/render.py  static ASCII grid for --grid and pipes
    cvar/cli.py     the cvar entry point
    tests/           make_fixture.py builds an agent-shaped repo

## Tests

```sh
python3 tests/test_timeline.py   # model, 18 tests
python3 tests/test_trees.py      # worktree discovery, column fitting, 24
python3 tests/test_tui.py        # navigation, layout, bridges, rendering, 183
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
