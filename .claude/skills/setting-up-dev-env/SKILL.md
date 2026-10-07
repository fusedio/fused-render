---
name: setting-up-dev-env
description: Use when setting up a fused-render checkout or git worktree, or when starting, waiting for, restarting or stopping the dev server (scripts/dev.sh) from an agent session — before pytest, the server, or any daemon — so the server actually comes up, you know when it is up, and you never kill another worktree's server.
---

# Setting Up the Dev Env

## Overview

Every fresh checkout/worktree needs a **3.12 venv** and a **built React shell** — both are gitignored, so they don't carry into a worktree. `scripts/dev.sh` builds both and runs the server; it is the only supported way to start it. Never run `python -m fused_render.cli` or uvicorn by hand.

What `dev.sh` does, in order: bootstraps `.venv` (Python 3.12, extras `[dev,fused,bundled]` — **pytest included**), `npm install`, one gated build (`tsc` + vite), then `vite build --watch` + the server under `watchfiles` (every `fused_render/**/*.py` save restarts the server; every frontend save rebuilds `shell-dist/`). Port and state dir are per branch.

## Starting the Server From an Agent Session

The recipe below is what the sandbox hook accepts (absolute paths, no `$HOME`, no `( … &)` subshell, no heredoc, no bare `sleep N;` chains). Replace `/abs/dev.log` with a file in your scratchpad directory.

```bash
nohup scripts/dev.sh --no-browser > /abs/dev.log 2>&1 &
```

Then wait for the one line that means "accepting connections":

```bash
until grep -q '==> ready:' /abs/dev.log; do sleep 2; done; grep -E '==> (dev port|ready|NOT ready)' /abs/dev.log
```

- `==> dev port: N …` is in the first lines: the port this run binds. `==> ready: http://127.0.0.1:N/` arrives when uvicorn is bound. `==> NOT ready:` after 120 s means look above it for the cause.
- Do **not** wait for cli.py's `fused-render serving at …` line: it prints *before* the bind, so a `curl` right after it is refused. Do not grep `Uvicorn running` either — use the `==> ready:` line, or `curl -fsS http://127.0.0.1:N/api/health`.
- Timing: first run on a fresh worktree ≈ 1–2 min (uv venv + npm install + build); later runs ≈ 20–40 s. Never use fixed sleeps.
- The log is noisy with vite chunk lines. Filter: `grep -v 'vite:reporter\|imported by\|kB │' /abs/dev.log | tail -20`.
- zsh trap: `echo ===` fails with `(eval):1: == not found` (zsh `=cmd` expansion). Quote it: `echo '==='`.

## Restarting and Stopping

- **Restart:** just run `dev.sh` again. It reaps the previous tree for *this worktree* (pidfile in `.dev-pids/`) and starts fresh.
- **Stop:** `scripts/dev.sh --cleanup`. Reaps this worktree's tree and exits.
- **Never** `pkill -f 'dev.sh'`, `pkill -f vite`, `pkill -f fused_render.cli` or `killall`. Other worktrees on this machine run their own dev servers (and some deliberate long-lived ones); a pattern kill takes them all out. A session did this six times in one afternoon.
- `pgrep -fl dev.sh` shows 2–3 `bash scripts/dev.sh` lines per tree (the script plus its vite and opener subshells). That is one tree, not duplicates.

## After an Edit

- **Python edit** → watchfiles restarts the server (a new `fused-render serving at` line appears, then nothing else: the `==> ready:` line prints once per `dev.sh` run). Allow ~5–30 s before hitting the API.
- **Frontend edit** → vite rebuilds in ~3–6 s. During the rebuild `shell-dist/` is *empty*, so every page route 500s with a `React shell not built` traceback; wait and reload. Never run `npm run build` / `npx vite build` while `dev.sh` is up — it wipes the same `shell-dist/` (see memory `never-run-vite-build`); use `npx tsc --noEmit` from `frontend/` to type-check.
- **Both halves in one save** used to kill the server: the Python restart landed inside the rebuild window, `create_app` raised `React shell not built`, and watchfiles does not relaunch a process that exited by itself, so it stayed dead until the next `.py` save. `dev.sh` now holds the restart until the bundle is back (`==> shell bundle missing (vite rebuilding) — holding …` in the log). If you ever do find the server dead with watchfiles alive, `touch fused_render/cli.py` relaunches it.

## Ports, State, Workspace

| Thing | Where |
|---|---|
| Port | per branch, derived from `FUSED_RENDER_BRANCH` (`dev.sh` sets it from the current branch). `--port N` overrides. On `main`/`master` there is **no isolation**: port 1777 and `~/.fused-render`, the same as the installed desktop app — if the app is running the bind fails with `port 1777 is already in use`. Pass `FUSED_RENDER_BRANCH=<name>` or `--port`. |
| State dir | `~/.fused-render/branches/<ref>/` on a branch (`<ref>` = sanitized, ≤12 chars); `~/.fused-render/` at baseline. Delete it for a from-scratch run of that branch. |
| Workspace | `FUSED_RENDER_DIR=/abs/path` (export it before `nohup …`) to serve a fresh folder instead of `~/Fused`. |
| Server log | `~/Library/Logs/fused-render/fused-render-<pid>.log` (path printed at start); `dev.log` only has stdout/stderr of vite + uvicorn. |
| Pidfile | `.dev-pids/dev.sh.pid` in the worktree; `.dev-pids/serve.sh` is the wrapper `dev.sh` writes for watchfiles. Both gitignored. |

## Worktrees: Do Not Symlink Into the Main Checkout

For a quick `pytest` in a worktree, memory `worktree-frontend-node-modules` suggests symlinking `fused_render/static/shell-dist` (and `frontend/node_modules`) from the main checkout. That is a **pytest-only** shortcut. Under `dev.sh` a linked `shell-dist` makes the vite watch wipe the *main checkout's* bundle on every edit here, a linked `node_modules` gets `npm install`ed into, and a linked `.venv` gets `pip install -e <this worktree>` — repointing the main checkout's editable install at your branch. `dev.sh` now removes all three symlinks itself (and says so), then bootstraps the real thing. If you will run the server, skip the symlinks and let `dev.sh` do the ~1 min bootstrap.

## Setup for Tests Only

If you will never start the server in this checkout, the two manual steps are:

```bash
uv venv --python 3.12 .venv                                         # pinned, same as dev.sh and all three installers
uv pip install --python .venv/bin/python -e ".[dev,bundled,fused]"
cd frontend && npm install && npm run build && cd ..                # shell-dist/ — create_app() refuses to start without it
```

If you have run `dev.sh` once, both already exist. Verify: `ls fused_render/static/shell-dist/index.html` and `.venv/bin/python -m pytest -q` (~1170 pass). The suite is xdist-safe, so `pytest -n auto` works (~4.5× faster).

## Reference

| Item | Why |
|------|-----|
| Python 3.12 | **pinned, not a default** (D214): the server passes its own interpreter to `uv sync` as the base for every project venv, so this version decides which wheels those venvs can resolve. A 3.14 venv made project venvs cp314, and a folder declaring `tensorflow` (no cp314 wheels) was an unresolvable dead end. 3.12 is what all three installers ship. `dev.sh` enforces it and rebuilds a `.venv` on anything else |
| `dev` extra | pytest + xdist + httpx (TestClient), plus the pyobjc the clipboard bridge needs on macOS — which is why `dev.sh` installs it too |
| `bundled` extra | duckdb, pandas, numpy, pyarrow, pillow… (templates + daemons). NOT the geo/PDF/plotting stack (D276) — `map`, `vector`, `pdf_studio` declare those in their own folder `pyproject.toml`, installed into a project venv on first render |
| `fused` extra | compute-engine wheel for `/api/run` |
| `FUSED_RENDER_NO_RELOAD=1` | run the server once without watchfiles (no Python auto-reload; the server opens its own tab) |
| `FUSED_RENDER_CORE_TEMPLATES` | `dev.sh` points it at the repo so template edits show without a version bump |
