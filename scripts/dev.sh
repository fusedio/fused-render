#!/usr/bin/env bash
# Dev loop: shell watch-build + python server, one command (D54 workflow).
#
#   scripts/dev.sh [fused-render args…]     e.g. scripts/dev.sh --port 9000
#
# Pipeline: npm install (if needed) -> one gated build (tsc + vite, so type
# errors surface before anything starts) -> `vite build --watch` in the
# background -> fused-render server in the foreground, supervised by
# watchfiles for Python auto-reload. Ctrl-C stops everything.
#
# Two independent reload paths:
#   * Frontend: `vite build --watch` rebuilds into fused_render/static/
#     shell-dist/ on every shell edit; the server reads files per-request with
#     Cache-Control: no-cache, so a browser refresh picks up the new bundle —
#     no server restart needed. (The watch skips the tsc gate for speed; run
#     `npm run typecheck` or a full `npm run build` before committing.)
#   * Python: edits to fused_render/**/*.py restart the server automatically.
#     watchfiles supervises `python -m fused_render.cli`, watching only *.py
#     under fused_render/ (the vite shell-dist output is .html/.js/.css and is
#     ignored, so frontend rebuilds never restart the server). On each restart
#     watchfiles gracefully stops the old process (SIGINT + wait for exit)
#     before relaunching, so the port guard in cli.py is respected.
#
# Under the reloader the server runs with --no-browser (so a save doesn't spawn
# a new tab); dev.sh opens the browser once, after the port comes up.
#
# Knobs:
#   * --no-browser (passed through): dev.sh won't open a tab either.
#   * FUSED_RENDER_NO_RELOAD=1: disable Python auto-reload; run the server once
#     exactly as before (server opens its own browser tab).
# watchfiles is auto-installed into the venv if missing; if the install fails,
# dev.sh falls back to the original single-launch behavior.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND="$REPO_ROOT/frontend"

# Read core templates straight from the repo, skipping the stage-into-home copy
# (~/.fused-render/.core-templates). Without this the server serves the last
# version-staged snapshot, so template edits wouldn't show until a version bump
# or a manual wipe. Respect an already-set value so the caller can override.
export FUSED_RENDER_CORE_TEMPLATES="${FUSED_RENDER_CORE_TEMPLATES:-$REPO_ROOT/fused_render/templates}"

# Isolate each branch/worktree onto its own port + state dir. Without this every
# dev.sh run (main checkout and every worktree) defaults to the baseline port
# 1777 and clobbers the same ~/.fused-render state, so a server left running in
# one worktree collides with — or gets served stale to — another. Deriving the
# ref from the current branch gives each branch a deterministic port of its own
# (see fused_render/_branch.py). main/master and detached HEAD sanitize to the
# baseline, so this is a no-op there. Respect an already-set value so the caller
# can override (including to "" to force baseline).
#
# NOTE: on main/master this mirrors baseline (port 1777 + the shared
# ~/.fused-render state), which is exactly what the installed macOS desktop app
# uses. Running dev.sh on main alongside the installed app therefore collides:
# the port bind fails loudly (see cli.py _check_port_free) and, more subtly,
# both read/write the same baseline state dir. Work on a feature branch (or pass
# FUSED_RENDER_BRANCH / --port) to run dev fully isolated from the desktop app.
if [[ -z "${FUSED_RENDER_BRANCH+x}" ]]; then
  export FUSED_RENDER_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
fi

# Python: active venv first, then the repo-local .venv. With neither, bootstrap
# a repo-local .venv (with the `fused` + `bundled` extras) so a fresh worktree is
# self-contained. Without this the fallback was bare `python3` on PATH, whose
# fused_render resolves to whatever global/editable install happens to be there
# (often the main checkout's) and whose site-packages lack the sci deps the
# map/geotiff/zarr daemons need — a fresh worktree would silently run the wrong
# code or fail at daemon spawn. `-e` keeps the install pointed at this worktree's
# source. Uses uv when present (fast, no ensurepip dance), else stdlib venv+pip.
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
  PY="$VIRTUAL_ENV/bin/python"
elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PY="$REPO_ROOT/.venv/bin/python"
else
  echo "==> no venv found — creating $REPO_ROOT/.venv with the [fused,bundled] extras"
  # Run the install from REPO_ROOT with the `.[extras]` form: uv rejects an
  # absolute path carrying extras (parses it as a PEP508 requirement).
  if command -v uv >/dev/null 2>&1; then
    uv venv "$REPO_ROOT/.venv"
    (cd "$REPO_ROOT" && uv pip install --python "$REPO_ROOT/.venv/bin/python" -e ".[fused,bundled]")
  else
    python3 -m venv "$REPO_ROOT/.venv"
    "$REPO_ROOT/.venv/bin/python" -m pip install --upgrade pip
    (cd "$REPO_ROOT" && "$REPO_ROOT/.venv/bin/python" -m pip install -e ".[fused,bundled]")
  fi
  PY="$REPO_ROOT/.venv/bin/python"
fi

command -v npm >/dev/null || { echo "npm not found — the dev loop needs Node 22"; exit 1; }
"$PY" -c "import fused_render" 2>/dev/null || {
  echo "fused_render not importable from $PY — run: pip install -e \".[fused,bundled]\""
  exit 1
}

# Install deps when they're missing OR stale. `node_modules/.package-lock.json`
# is npm's own record of the last install; if the real package-lock.json is
# newer than it (a dependency bump, or a branch switch that changed the lock),
# node_modules no longer matches the manifest and the build fails on a missing
# module — reinstall to reconcile. `-nt` also fires when the marker is absent
# entirely (never installed, or a non-npm install left no marker), so a
# markerless node_modules self-heals on the next run.
if [[ ! -d "$FRONTEND/node_modules" ]]; then
  echo "==> npm install (first run)"
  (cd "$FRONTEND" && npm install --no-audit --no-fund)
elif [[ "$FRONTEND/package-lock.json" -nt "$FRONTEND/node_modules/.package-lock.json" ]]; then
  echo "==> npm install (package-lock.json changed since last install)"
  (cd "$FRONTEND" && npm install --no-audit --no-fund)
fi

echo "==> initial shell build (tsc + vite)"
(cd "$FRONTEND" && npm run build)

# `vite build --watch` empties fused_render/static/shell-dist/ before its first
# rebuild — so the bundle the initial build just produced vanishes for a beat.
# The server's startup check (create_app) fails hard if shell-dist is missing,
# so it must not launch during that gap. Delete the index first, then wait for
# the watch to re-emit it: its reappearance unambiguously means the watch's
# first build finished (checking before deletion would pass instantly on the
# initial build's copy and still race the empty). Bounded so a genuinely broken
# build surfaces instead of hanging forever.
DIST_INDEX="$REPO_ROOT/fused_render/static/shell-dist/index.html"
rm -f "$DIST_INDEX"

echo "==> starting vite watch + fused-render server (Ctrl-C stops both)"
(cd "$FRONTEND" && npm run watch) &
WATCH_PID=$!
# OPENER_PID is the one-shot browser opener (set below, reload path only). The
# trap references it lazily so it's harmless while still unset.
OPENER_PID=""
trap 'kill "$WATCH_PID" 2>/dev/null || true; [[ -n "$OPENER_PID" ]] && kill "$OPENER_PID" 2>/dev/null || true' EXIT INT TERM

echo "==> waiting for the vite watch to emit the shell bundle"
for _ in $(seq 1 60); do
  [[ -f "$DIST_INDEX" ]] && break
  sleep 0.5
done
[[ -f "$DIST_INDEX" ]] || { echo "shell bundle never appeared at $DIST_INDEX — check the vite watch output above"; exit 1; }

# Python auto-reload via watchfiles (opt out with FUSED_RENDER_NO_RELOAD).
# Restarts the server on any fused_render/**/*.py edit. watchfiles stops the old
# process (SIGINT + wait) before relaunching, so cli.py's port guard is honored.
RELOAD=1
[[ -n "${FUSED_RENDER_NO_RELOAD:-}" ]] && RELOAD=0

if [[ "$RELOAD" -eq 1 ]]; then
  # Ensure watchfiles is importable from this venv; install it if not. Match the
  # venv-bootstrap style above (uv when present, else pip). Any failure is
  # non-fatal — we fall back to the plain single launch below.
  if ! "$PY" -c 'import watchfiles' 2>/dev/null; then
    echo "==> installing watchfiles into the venv (for Python auto-reload)"
    if command -v uv >/dev/null 2>&1; then
      uv pip install --python "$PY" watchfiles || true
    else
      "$PY" -m pip install watchfiles || true
    fi
  fi
  if ! "$PY" -c 'import watchfiles' 2>/dev/null; then
    echo "==> WARNING: watchfiles unavailable — falling back to a single launch (no Python auto-reload)"
    RELOAD=0
  fi
fi

if [[ "$RELOAD" -eq 1 ]]; then
  # Decide whether to open a browser tab, and on which port. The server runs
  # with --no-browser under the reloader, so dev.sh opens the tab exactly once.
  NO_BROWSER=0
  PORT=""
  want_port=0
  for a in "$@"; do
    if [[ "$want_port" -eq 1 ]]; then PORT="$a"; want_port=0; continue; fi
    case "$a" in
      --no-browser) NO_BROWSER=1 ;;
      --port=*)     PORT="${a#--port=}" ;;
      --port)       want_port=1 ;;
    esac
  done
  # No explicit --port: fall back to the per-branch default the server derives.
  if [[ -z "$PORT" ]]; then
    PORT="$("$PY" -c 'from fused_render._branch import branch_port; print(branch_port())' 2>/dev/null || true)"
  fi

  # One-shot opener: wait for the port to accept a connection, then open the tab.
  if [[ "$NO_BROWSER" -eq 0 && -n "$PORT" ]]; then
    (
      for _ in $(seq 1 120); do
        if "$PY" -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(0 if s.connect_ex(('127.0.0.1', $PORT))==0 else 1)" 2>/dev/null; then
          break
        fi
        sleep 0.5
      done
      URL="http://127.0.0.1:$PORT/"
      # Open via Python's webbrowser (cross-platform, matches cli.py); a shell
      # open/xdg-open/start chain misses Windows/git-bash (start is a cmd
      # builtin, not a binary on PATH).
      "$PY" -c "import webbrowser; webbrowser.open('$URL')" >/dev/null 2>&1 || true
    ) &
    OPENER_PID=$!
  fi

  # watchfiles wants the target as a single shell-command string, then the watch
  # paths. printf %q quotes $PY and each passthrough arg so paths/args with
  # spaces survive. --filter python watches only *.py, so vite's shell-dist
  # output (.html/.js/.css) never triggers a restart. --ignore-paths excludes
  # fused_render/templates/ — those *.py are per-request UDF code, not imported
  # into the server process, so editing them shouldn't restart it (watchfiles
  # resolves ignore paths to absolute; comma-separate to add more).
  CMD="$(printf '%q' "$PY") -m fused_render.cli --no-browser"
  for a in "$@"; do CMD+=" $(printf '%q' "$a")"; done
  "$PY" -m watchfiles --filter python --ignore-paths "$REPO_ROOT/fused_render/templates" "$CMD" "$REPO_ROOT/fused_render"
else
  # Original single-launch behavior: the server opens its own browser tab.
  "$PY" -m fused_render.cli "$@"
fi
