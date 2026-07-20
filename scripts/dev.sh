#!/usr/bin/env bash
# Dev loop: shell watch-build + python server, one command (D54 workflow).
#
#   scripts/dev.sh [fused-render args…]     e.g. scripts/dev.sh --port 9000
#
# Pipeline: npm install (if needed) -> one gated build (tsc + vite, so type
# errors surface before anything starts) -> `vite build --watch` in the
# background -> fused-render server in the foreground. Ctrl-C stops both.
#
# The watch rebuilds into fused_render/static/shell-dist/ on every shell
# edit; the server reads files per-request with Cache-Control: no-cache, so
# a browser refresh picks up the new bundle — no server restart needed.
# (Note: the watch skips the tsc gate for speed; run `npm run typecheck` or
# a full `npm run build` before committing.)
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
if [[ -z "${FUSED_RENDER_BRANCH+x}" ]]; then
  export FUSED_RENDER_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
fi

# Python: active venv first, then the repo-local .venv, then PATH.
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
  PY="$VIRTUAL_ENV/bin/python"
elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PY="$REPO_ROOT/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

command -v npm >/dev/null || { echo "npm not found — the dev loop needs Node 22"; exit 1; }
"$PY" -c "import fused_render" 2>/dev/null || {
  echo "fused_render not importable from $PY — run: pip install -e ."
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
trap 'kill "$WATCH_PID" 2>/dev/null || true' EXIT INT TERM

echo "==> waiting for the vite watch to emit the shell bundle"
for _ in $(seq 1 60); do
  [[ -f "$DIST_INDEX" ]] && break
  sleep 0.5
done
[[ -f "$DIST_INDEX" ]] || { echo "shell bundle never appeared at $DIST_INDEX — check the vite watch output above"; exit 1; }

"$PY" -m fused_render.cli "$@"
