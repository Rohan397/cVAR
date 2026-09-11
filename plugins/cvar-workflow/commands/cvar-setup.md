---
description: Install the cvar command-line tool so `cvar` works from any repo. Run once per machine.
allowed-tools: Bash(python3:*), Bash(uv:*), Bash(pipx:*), Bash(command:*), Bash(ls:*), Bash(mkdir:*), Bash(ln:*), Bash(echo:*), Bash(cvar:*), Bash(test:*)
---

## Your task

Put the `cvar` executable on the user's PATH. A plugin cannot do this itself —
it ships skills and commands, not binaries — which is why this is a command the
user runs rather than something that happens at install time.

Work through the steps below, report which one succeeded, and stop as soon as
`cvar` runs.

### 1. Is it already there?

```bash
command -v cvar && cvar --version 2>/dev/null || echo "not installed"
```

If it is already on PATH, say so and stop. Do not reinstall over a working
install without being asked.

### 2. Find the source

Prefer the marketplace clone: it is the same commit as the plugin the user
installed, so the tool and the skill stay in step.

```bash
ls ~/.claude/plugins/marketplaces/cvar/pyproject.toml 2>/dev/null
```

If that exists, the source is `~/.claude/plugins/marketplaces/cvar`. If it does
not — the marketplace was added from a local directory, or has not been cloned —
fall back to `git+https://github.com/Rohan397/cVAR`, and tell the user the
installed tool tracks `main` rather than the plugin's commit.

### 3. Install it

Try these in order and use the first that works. `<source>` is whatever step 2
resolved to.

```bash
uv tool install <source>                      # if uv exists
pipx install <source>                         # if pipx exists
python3 -m pip install --user <source>        # plain pip
```

Many Pythons — Homebrew's especially — are marked externally managed and will
refuse the third with `error: externally-managed-environment`. **Do not pass
`--break-system-packages`**: that writes into a Python the OS package manager
owns. Use a dedicated environment instead, which is what `pipx` would have done:

```bash
python3 -m venv ~/.local/share/cvar
~/.local/share/cvar/bin/python -m pip install --quiet <source>
mkdir -p ~/.local/bin
ln -sf ~/.local/share/cvar/bin/cvar ~/.local/bin/cvar
```

### 4. Confirm, and check PATH

```bash
command -v cvar || echo "installed but not on PATH"
cvar --glyphs | head -3
```

If `cvar` installed but is not on PATH, the bin directory is missing from it.
Tell the user which directory, and give them the line to add to `~/.zshrc`:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Do not edit their shell config yourself unless they ask.

### 5. Tell them how to use it

Two panes: their editor in one, `cvar .` in the other. It follows the branch
live as commits land. `cvar . --doctor` explains where handoffs will open if
they land somewhere unexpected.

If their editor is nvim and handoffs open in a GUI editor instead, the fix is
`export CVAR_EDITOR=nvim`.
