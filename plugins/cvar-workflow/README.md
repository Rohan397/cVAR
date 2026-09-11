# cvar-workflow

The agent-facing half of cVAR.

cVAR is the review room: it renders a branch as a scrubbable timeline so you
can read what an agent did one commit at a time. That only works if the branch
is worth reading — small commits, one concern per branch. This plugin is the
half that makes that happen, so the tool and the discipline it depends on
install together and stay at the same commit.

| | |
| --- | --- |
| `incremental-building` | Skill. Decides how work splits across branches, then commits in small pieces inside them. |
| `/cvar-setup` | Installs the `cvar` command itself. |

## Install

```
/plugin marketplace add Rohan397/cVAR
/plugin install cvar-workflow@cvar
/cvar-setup
```

The first two give you the skill; the third puts `cvar` on your PATH. A plugin
cannot add a binary to your shell, which is why the last step is its own
command rather than something that happens invisibly at install time.
