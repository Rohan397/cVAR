---
name: incremental-building
description: Use when implementing anything that will take more than one edit — a feature, a refactor, a bug fix with setup, a migration, a set of steps from a plan. Decides how the work is split across *branches* before it starts, then commits each piece separately inside them, so every branch stays a single reviewable story instead of a pile of unrelated work. Triggers on "build", "implement", "add support for", "refactor", "migrate", "let's work on", "fix", and on approval of a plan already discussed: "do steps 1-3", "go ahead", "proceed", "sounds good". Approval is the moment implementation begins, so it is the moment this applies. Also use when work is already underway, or when a branch has started collecting things it was not named for.
---

# Incremental building

Work in small, reviewable pieces at two levels: **the branch is the review
artifact, the commit is the step inside it.**

Small commits alone are not enough. Nine tidy commits spread across four
unrelated concerns still produce a branch nobody can read as one thing — the
timeline shows four stories interleaved, and the reviewer has to hold all of
them at once. Commit discipline makes each step legible; branch discipline is
what makes the *whole* legible.

The branch is also the pull request. Whatever ends up on it is what gets
reviewed together, so branch scope is the decision that determines what review
is even possible.

## The branch gate

**Announce and stop at the start of every new piece of work — including the
decision to stay on the branch you are already on.**

```
BRANCH — starting `ncurses-rep-workaround`, off main.
  Scope:     the terminfo workaround and its diagnostics.
  Not here:  the cvar rename — mechanical, its own branch.
  Test:      name test. "ncurses rep workaround" needs no "and".
Say go, or tell me to fold it into the current branch.
```

This blocks. It is the one gate worth paying for, because branch scope is the
only decision here that is genuinely expensive to reverse — commits inside a
branch can be reordered, but a branch that collected two concerns needs history
surgery to separate. Stopping at creation is stopping where correction is
cheapest: nothing has landed yet.

**Do not re-ask what was already agreed.** If a branch plan was shown and
approved — in plan mode, in conversation, anywhere — that approval *is* the
gate. Map the agreed pieces onto branches and start. Re-asking a question the
user already answered is how a useful gate turns into friction.

**This gate is loud on purpose, for now.** It is a calibration setting, not a
permanent law: it exists so the thresholds below can be corrected while they
are still guesses. Do not quietly relax it because it started to feel chatty —
if it is too much, that is a conversation, not a drift.

## Planning: decide the branches first

Before commits, before code. Two tests, both applied to work as *described* —
they are predictions, and predictions are sometimes wrong, which is what the
tripwires later are for.

**The name test.** Can the branch be named as a short noun phrase with no
"and"? `auth-token-rotation`, not `auth-and-logging`. If the name needs an
"and", or is vague enough to cover anything (`fixes`, `improvements`,
`cleanup`), it is more than one branch. Name it for the work, never the tooling
— never `claude-fix`.

**The mechanical-swamp test.** A rename, a reformat, a mass import rewrite —
anything that touches many files without changing behaviour — goes on its own
branch, not merely its own commit. Mechanical churn lights up every track in
the timeline at once and drowns the logic changes near it, so a reviewer
scrubbing the branch sees one enormous flash and nothing else. This is the
split people miss most often, because the change feels trivial. It is trivial
to *write* and expensive to *review alongside anything else*.

Then say which branches stack and which are independent:

- **Stacked** — B needs A. Branch B off A and say so, so the reviewer knows to read them in order.
- **Independent** — B does not need A. Branch off `main`. Do not serialise independent work onto one branch just because you are already standing there; independent branches can run as parallel worktrees, which is what the overview view is for.

## Then commits, inside a branch

Commits are silent. They are checkpoints within a scope already agreed at the
gate, so announcing each one is noise — the opposite of the branch decision,
which is news.

Invoking this skill *is* the authorization to commit. Do not ask before each
one, and do not leave everything uncommitted "until the user asks."

| Action | |
| --- | --- |
| Local commits | Pre-authorized. Just make them. |
| First push of a branch | Ask once. Pushing is outward-facing. |
| Later pushes to that branch | Covered by that first yes. |
| Opening a PR | Ask. |

Inside a branch, break the work into pieces that stand alone. A piece is one
commit: it should compile, pass tests, and be describable in a single sentence
with no "and". Past roughly 150 changed lines or 3 files, assume it was two
pieces and look for the seam — not a hard limit, since a generated file or a
mechanical rename is legitimately larger, but the default assumption.

Order them so each builds on the last, and for each piece: write it, verify it
by actually running it, commit it, move on.

**Commit when it works, not when it is finished.** Every point where the code
runs and the tests pass is a commit, whether or not the feature is done. A
module with no caller yet is committable.

**Commit the last thing before starting the next thing.** If you are about to
open a different file for a different reason, the previous piece is done and
should already be in. Accumulation happens by drift, not by decision.

**Tests ship with the code they test.** A trailing "add the tests" commit is
the most common way a small change becomes a large one.

An hour of steady work is several commits, not one. If an hour has passed and
`git log` is empty, the pieces were too big.

## Execution: tripwires

Planning tests are guesses. These are measurements, and they catch the splits
that could not have been seen in advance. Check them as commits land.

**The width test.** The timeline stretches to fill the terminal and only falls
back to one column per commit — and scrolling — once commits outnumber columns.
A branch that no longer fits on one screen has passed the point where it can be
taken in as a single picture. Check it directly:

```sh
cvar .                  # piped or non-tty: prints the grid once and exits
```

**The disjoint-cluster test.** If the files touched fall into groups that never
co-occur in any commit, those groups are separate branches. Two blocks in the
grid that never overlap is the visual signature, and it is the strongest signal
available because it is measured from what actually happened rather than from
what anyone intended.

**Scope drift.** Something arrived that the branch is not named for. The "while
I'm here" fix, the unrelated bug noticed in passing, the dependency bump. Each
is individually reasonable and collectively how a branch stops being one story.

### When a tripwire fires

**It decides where the *next* commit goes. It never rewrites history.**

Retroactive splitting means cherry-picking and rebasing, which loses the one
property that makes small commits worth having — that each was a point which
actually worked — and it is where this kind of tidying goes wrong. So: finish
the current piece, commit it, and start the next thing on a new branch.

The exception is a branch with nothing landed on it yet, where the fix is just
a rename.

Report it, and name the test, so the judgment can be corrected rather than
merely obeyed:

```
BRANCH — tripwire: disjoint clusters.
  `worktree-overview` now holds two groups that share no commit:
    trees.py / tui.py  (12 commits)   and   termfix.py / doctor.py  (2 commits)
  Nothing already committed moves. The next piece starts on its own branch
  unless you would rather review them together.
```

## Commit messages

Subject: imperative, under ~70 characters, says what changed.
Body: only when the *why* is not evident from the diff — a constraint, a
rejected alternative, a bug the change prevents. Never narrate the diff.

```
Track file identity across renames

Keying on path breaks exactly where refactors happen: a renamed file
reads as one track dying and another being born. Tracks now follow a
logical identity, so the timeline survives a mid-branch move.
```

## What not to do

- Do not commit directly to `main` or `master`.
- Do not commit unrelated changes together because they were in the tree at the same time.
- Do not commit broken intermediate states to "save progress."
- Do not slip in drive-by fixes or reformatting. Their own commit, their own branch, or nowhere.
- Do not push commits you have not verified.
- Do not open a branch whose name you would be embarrassed to see on a PR.

## If you are already deep in uncommitted work

Split what is in the tree into the commits it should have been, using
`git add -p` where a file spans two pieces, and commit them in dependency
order. Say plainly that the boundaries were reconstructed after the fact —
reconstructed boundaries are worth less than real ones, because nothing proves
each point actually worked.

If that work also spans two concerns, commit it where it is and put the *next*
concern on its own branch. Do not start the recovery with history surgery.

## Reviewing the result

`cvar` renders a branch as a scrubbable timeline — the shape of the work, which
files churned, what each commit did. Expect it to be **already running while
the work happens**, in another pane, following the branch as commits land.

```sh
cvar .                  # in the repo, with nvim open in another pane
cvar . --doctor         # if handoffs open somewhere unexpected
```

It is the instrument the tripwires above are read from: a file rewritten
several times across the branch means the commit boundaries were wrong, and two
clusters that never overlap mean the branch boundary was.
