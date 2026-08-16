#!/usr/bin/env bash
# Dev loop: shell watch-build + python server, one command (D54 workflow).
#
#   scripts/dev.sh [fused-render args…]     e.g. scripts/dev.sh --port 9000
#
# Pipeline: npm install (if needed) -> one gated build (tsc + vite, so type
# errors surface before anything starts) -> `vite build --watch` in the
# background -> fused-render server (supervised by watchfiles for Python
# auto-reload) ALSO in the background, with dev.sh parked on `wait` for it.
# Ctrl-C — or a plain `kill` of this script — stops everything.
#
# The server is deliberately not a foreground child: bash defers a trap handler
# until the current foreground command returns, so while it ran in the
# foreground a signal to dev.sh merely QUEUED the handler and the whole tree
# survived. `wait` is interruptible, so the handler runs at once. See the
# process-cleanup section below, which owns the teardown for every pid.
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
#   * --cleanup: reap this worktree's running dev.sh tree and exit, starting
#     nothing. Consumed here, never forwarded to the server.
#   * FUSED_RENDER_NO_RELOAD=1: disable Python auto-reload; run the server once
#     exactly as before (server opens its own browser tab).
# watchfiles is auto-installed into the venv if missing; if the install fails,
# dev.sh falls back to the original single-launch behavior.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND="$REPO_ROOT/frontend"

# --cleanup is dev.sh's own flag and must not reach the server: cli.py's parser
# rejects unknown options, so leaving it in "$@" would turn a cleanup into a
# usage error. Strip it here, before either passthrough site (the watchfiles
# CMD builder and the single-launch argv) reads "$@".
CLEANUP_ONLY=0
_ARGS=()
for _a in "$@"; do
  if [[ "$_a" == "--cleanup" ]]; then CLEANUP_ONLY=1; else _ARGS+=("$_a"); fi
done
# `${_ARGS[@]+…}` because bash 3.2 (what macOS ships) treats an empty array as
# unset under `set -u` and would abort on a bare `"${_ARGS[@]}"`.
set -- ${_ARGS[@]+"${_ARGS[@]}"}

# Isolate each branch/worktree onto its own port + state dir. Without this every
# dev.sh run (main checkout and every worktree) defaults to the baseline port
# 1777 and clobbers the same ~/.fused-render state, so a server left running in
# one worktree collides with — or gets served stale to — another. Deriving the
# ref from the current branch gives each branch a deterministic port of its own
# (see fused_render/_branch.py). main/master and detached HEAD sanitize to the
# baseline, so this is a no-op there. Respect an already-set value so the caller
# can override (including to "" to force baseline).
#
# Resolved this early (before any of the setup below) because the port it feeds
# is what the startup reap waits on for the old server to let go of the bind, and
# that reap has to happen before dev.sh starts rebuilding the shell — otherwise
# the old run's `vite build --watch` is still writing shell-dist/ while this one
# builds into it. (The pidfile deliberately does NOT depend on this ref; see
# dev_pidfile_path.)
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

# ---------------------------------------------------------------------------
# Process cleanup: kill_tree, the per-worktree pidfile, and the one shutdown
# handler every trap installs.
#
# dev.sh used to leak entire process trees. Eleven orphans were hand-killed off
# one machine, some 10-20 days old, from three separate holes:
#
#   1. `(cd "$FRONTEND" && npm run watch) &` makes $! the SUBSHELL, not vite.
#      The observed chain was dev.sh -> subshell -> npm -> node vite; the trap
#      killed the subshell, and npm + vite reparented to init and kept
#      rebuilding into shell-dist/ forever. Same shape for the core_apps poll
#      loop (which has a `sleep` child) and the browser opener.
#   2. The watchfiles-supervised server ran in the FOREGROUND and appeared in no
#      trap at all — and bash defers a trap handler until the current foreground
#      command returns, so `kill <dev.sh>` was merely queued. Those trees only
#      collapsed once the watchfiles children were signalled directly.
#   3. Nothing noticed an already-running dev.sh for the same worktree. cli.py's
#      port guard catches a second SERVER, but two vite watchers coexist happily
#      while both write the same shell-dist/ — which is how one worktree ended
#      up with two complete dev.sh trees.
#
# Everything below reaps ONLY the pids this run owns, or the tree the pidfile
# names. Never a pattern kill: other worktrees on this machine run their own
# dev servers and orphans that are none of our business.
# ---------------------------------------------------------------------------

# Every walk below is `pgrep -P`, so a machine without pgrep would degrade
# silently back to the exact orphan bug this section exists to fix (the walks
# swallow the failure with `|| true` and only the root pid gets signalled). Say
# so once, loudly, rather than leaking vite watchers for another three weeks.
if ! command -v pgrep >/dev/null 2>&1; then
  echo "==> WARNING: pgrep not found — dev.sh can only signal its direct" >&2
  echo "    children, so a vite watch or server may be left running after exit." >&2
fi

# macOS ships bash 3.2 and has no `setsid`, so there is no process group to
# signal — walk the tree by hand. `pgrep -P` exists on both macOS and Linux
# (CI runs linux-desktop), unlike the BSD-only ps flags. Depth-first: children
# die before their parent, so nothing reparents to init mid-kill and escapes.
kill_tree() {
  local pid="$1" child
  [[ -n "$pid" ]] || return 0
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do
    kill_tree "$child"
  done
  kill -TERM "$pid" 2>/dev/null || true
}

# The same walk, but only collecting pids. Snapshotting BEFORE the kill is what
# makes the KILL escalation possible at all: once TERM lands, the parent links
# are gone and the tree can no longer be rediscovered.
tree_pids() {
  local pid="$1" child
  [[ -n "$pid" ]] || return 0
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do
    tree_pids "$child"
  done
  printf '%s\n' "$pid"
}

# Alive AND not a zombie. `kill -0` answers yes for a zombie (it exists until
# it is waited for), which would make every shutdown burn the full grace period
# on our own just-TERMed children.
pid_alive() {
  local st
  st="$(ps -o stat= -p "$1" 2>/dev/null | tr -d '[:space:]')"
  [[ -n "$st" && "${st:0:1}" != "Z" ]]
}

# TERM every tree rooted at the (space-separated) pids in $1, then KILL whatever
# is still standing. The grace period is bounded (~2s) rather than a
# wait-until-gone loop: a child that traps TERM must not be able to hang the
# Ctrl-C the developer just pressed.
#
# One function for both callers (the startup reap and dev_shutdown) so the
# escalation policy cannot differ between "reap the previous run" and "reap this
# one" — the two trap sites already drifted apart once.
reap_trees() {
  local roots="$1" pids="" p i alive newpid
  for p in $roots; do
    [[ -n "$p" ]] || continue
    pids="$pids $(tree_pids "$p")"
    kill_tree "$p"
  done
  [[ -n "${pids// /}" ]] || return 0
  for i in $(seq 1 10); do
    # Re-walk from the roots on every pass: a tree can GROW mid-teardown. The
    # core_apps poll loop touches a *.py trigger, which is exactly what makes
    # watchfiles spawn a REPLACEMENT server — so a restart can land between the
    # walk above and the TERM, and that new server would inherit the port with
    # nothing supervising it. Anything that appears is TERMed and joins the
    # escalation list. (A root that is already gone contributes nothing, which is
    # why this cannot chase a pid we never owned.)
    for p in $roots; do
      [[ -n "$p" ]] || continue
      for newpid in $(tree_pids "$p"); do
        case " $pids " in
          *" $newpid "*) ;;
          *)
            pids="$pids $newpid"
            kill -TERM "$newpid" 2>/dev/null || true
            ;;
        esac
      done
    done
    alive=0
    for p in $pids; do
      if pid_alive "$p"; then alive=1; break; fi
    done
    [[ "$alive" -eq 0 ]] && return 0
    sleep 0.2
  done
  for p in $pids; do
    if pid_alive "$p"; then kill -KILL "$p" 2>/dev/null || true; fi
  done
  return 0
}

# Single-tree convenience wrapper (the startup reap has exactly one root).
kill_tree_hard() {
  [[ -n "$1" ]] || return 0
  reap_trees "$1"
}

# A python that can import this worktree's fused_render._branch. Deliberately
# NOT the $PY resolved further down: the pidfile has to be readable before the
# venv bootstrap runs (and by `--cleanup`, which bootstraps nothing). _branch is
# stdlib-only and is imported from the source tree via cwd, so any python3 gives
# the same answer the server will.
dev_python() {
  if [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
    printf '%s\n' "$VIRTUAL_ENV/bin/python"
  elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    printf '%s\n' "$REPO_ROOT/.venv/bin/python"
  else
    # Last resort, and it CAN come up empty: on a machine with no venv yet and
    # only a versioned `python3.12` on PATH (the setup the venv bootstrap below
    # exists to serve) there is no bare python3 to find. Callers must treat ""
    # as "not yet resolvable" and say so — see the PORT note below.
    command -v python3 || command -v python || true
  fi
}

# Where this worktree records its dev.sh pid. Keyed on the WORKTREE — one fixed
# filename — because the worktree is what the record actually protects:
# shell-dist/, node_modules, and the `vite build --watch` writing into them are
# per-checkout, not per-branch. `.dev-pids/` lives inside $REPO_ROOT, so the
# directory is already worktree-unique by construction and a branch component in
# the name would add nothing but two ways to MISS a live process:
#
#   * this function used to shell out to dev_python() to sanitize the ref, and
#     dev_python() legitimately returns "" on the machine the venv bootstrap
#     below exists to serve (no $VIRTUAL_ENV, no .venv yet, only a versioned
#     python3.12 that is not on PATH as `python3`). The name silently fell back
#     to a literal `_baseline` even on a feature branch, so the FIRST run
#     recorded itself under one name and the next — bootstrap done, interpreter
#     now resolvable — read another, found nothing, and stacked a second tree;
#   * `git checkout` while dev.sh is running changed the derived ref, so the next
#     run looked up a different file and left the old tree alive, with two vite
#     watchers writing the same shell-dist/.
#
# Keying on the path also drops the interpreter dependency entirely, which is
# what makes `--cleanup` and the startup reap work before any venv exists.
# Distinguishing one worktree's record from another's is now solely the job of
# the recorded repo root in dev_pidfile_is_ours — see the note there.
dev_pidfile_path() {
  printf '%s/.dev-pids/dev.sh.pid\n' "$REPO_ROOT"
}

# Is pid $1 really the dev.sh this pidfile was written for? Four checks, ALL
# mandatory, because getting this wrong means kill_tree_hard on an innocent
# process:
#   * a recorded start time exists at all;
#   * it is alive;
#   * its command line still mentions dev.sh;
#   * its start time matches the one recorded alongside the pid — the only check
#     that actually rules out pid reuse, since a *new* dev.sh in another
#     worktree satisfies the other two just as well as ours does.
# The start-time check is required rather than best-effort for exactly that
# reason: were it skipped when the record is empty (a `ps -o lstart=` that
# failed at write time), a recycled pid landing on ANY process whose argv
# mentions dev.sh — another worktree's dev.sh included — would be reaped.
# Failing closed costs a missed auto-restart; failing open kills someone's work.
# The recorded repo root is compared too, and that check carries more weight now
# that the filename is fixed rather than branch-derived: it is the ONLY thing in
# the record that names a worktree. It catches a pidfile that arrived from
# another checkout by any route — copied by hand, restored from a backup, or
# (the realistic one) a $REPO_ROOT that moved because the directory was renamed
# or a worktree was re-created at a new path while a dev.sh from the old path was
# still running. Never drop it on the grounds that the file's location already
# implies the root.
dev_pidfile_is_ours() {
  local pid="$1" want_start="$2" want_root="$3" cmd cur_start
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [[ "$pid" != "$$" ]] || return 1
  [[ -n "$want_start" ]] || return 1
  [[ "$want_root" == "$REPO_ROOT" ]] || return 1
  pid_alive "$pid" || return 1
  # `-ww` is load-bearing on Linux and NOT cosmetic: GNU procps truncates the
  # command column to 80 chars whenever stdout is not a terminal (it is a pipe
  # here), and dev.sh lives at the END of its own command line. Any checkout
  # deeper than ~74 characters — e.g. a git worktree under
  # <repo>/.claude/worktrees/<name>/scripts/dev.sh — would lose the "dev.sh"
  # suffix, this check would reject the pidfile forever, and the startup reap
  # would silently do nothing while dev.sh went back to stacking duplicate
  # trees. Exactly the invisible failure this whole section exists to remove.
  # BSD ps (macOS) accepts -ww too and never truncated a piped -o in the first
  # place, so this needs no platform branch.
  cmd="$(ps -ww -o command= -p "$pid" 2>/dev/null || true)"
  case "$cmd" in
    *dev.sh*) ;;
    *) return 1 ;;
  esac
  cur_start="$(ps -o lstart= -p "$pid" 2>/dev/null | sed -e 's/^ *//' -e 's/ *$//')"
  [[ "$cur_start" == "$want_start" ]] || return 1
  return 0
}

# Every background pid this run owns. Set before the traps so the handler can
# read them lazily under `set -u` no matter how early we die.
WATCH_PID=""
CORE_WATCH_PID=""
OPENER_PID=""
SERVER_PID=""
DEV_PIDFILE=""

# The single shutdown handler — one function, installed by every trap, so the
# two trap sites that used to carry DIFFERENT pid lists (and neither of them the
# server) cannot drift apart again.
dev_shutdown() {
  # Disarm first: the INT/TERM handlers exit, which re-enters via EXIT.
  trap - EXIT INT TERM
  local roots="$WATCH_PID $CORE_WATCH_PID $OPENER_PID $SERVER_PID"
  if [[ -n "${roots// /}" ]]; then
    reap_trees "$roots"
    # Reap our own children rather than leaving zombies parked on this shell.
    # Safe from hanging: anything that ignored TERM has just been KILLed.
    wait 2>/dev/null || true
  fi
  [[ -n "$DEV_PIDFILE" ]] && rm -f "$DEV_PIDFILE"
  return 0
}

# The port this run will use, for the post-reap wait below and for the browser
# opener further down (derived in one place so the two cannot disagree). An
# explicit --port wins — in either spelling — otherwise it is the per-branch
# default the server derives. Returns "" when no interpreter is resolvable yet;
# every caller checks.
dev_effective_port() {
  local a want=0 port="" py
  for a in "$@"; do
    if [[ "$want" -eq 1 ]]; then port="$a"; want=0; continue; fi
    case "$a" in
      --port=*) port="${a#--port=}" ;;
      --port)   want=1 ;;
    esac
  done
  if [[ -z "$port" ]]; then
    py="$(dev_python)"
    if [[ -n "$py" ]]; then
      port="$(cd "$REPO_ROOT" && "$py" -c 'from fused_render._branch import branch_port; print(branch_port())' 2>/dev/null || true)"
    fi
  fi
  printf '%s\n' "$port"
}

# Is $1 accepting connections? Same probe the browser opener uses.
port_is_open() {
  local py
  py="$(dev_python)"
  [[ -n "$py" && -n "$1" ]] || return 1
  "$py" -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(0 if s.connect_ex(('127.0.0.1', int(sys.argv[1])))==0 else 1)" "$1" 2>/dev/null
}

# Library mode: define the helpers, then stop before doing anything. Lets the
# tests drive kill_tree, the stale-pidfile decision and the --port extraction
# against the real code instead of a copy that can rot
# (tests/test_dev_sh_process_cleanup.py). Everything testable belongs ABOVE
# this line.
if [[ -n "${FUSED_RENDER_DEV_SH_LIB:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi

# Best effort at this point in the script: the reap has to run before the venv
# bootstrap (it must stop the old vite watch before this run rebuilds
# shell-dist/), so on a machine whose only interpreter is the versioned
# python3.12 that the bootstrap is about to use, there is nothing to ask yet.
# That is not fatal — it costs the post-reap port wait, and PORT is re-derived
# once $PY exists — but it is never silent.
PORT="$(dev_effective_port "$@")"
if [[ -z "$PORT" ]]; then
  echo "==> NOTE: no interpreter on PATH yet, so the dev port is still unknown;" >&2
  echo "    skipping the post-reap port wait (re-derived after the venv is ready)." >&2
fi

# Reap a previous dev.sh for THIS worktree. Restarting is nearly always what the
# developer meant by running dev.sh again — and the alternative is hole 3 above:
# a second full tree whose vite watch fights this one over shell-dist/.
DEV_PIDFILE="$(dev_pidfile_path)"
REAPED=0
# Every record in .dev-pids/, not just the current filename. The pidfile used to
# be named after the sanitized branch ref, so a dev.sh started before this change
# — or before a `git checkout` — left its record in this same directory under a
# different name, and honouring only the new name would leave those trees running
# forever with nothing but a pattern kill able to find them.
#
# Reading the leftovers is safe, and reading them is not a weaker check than
# reading the current one: .dev-pids/ lives inside $REPO_ROOT, so every file in
# it describes a process started from THIS worktree, and each one still has to
# pass all of dev_pidfile_is_ours (live, argv says dev.sh, start time matches,
# recorded root == $REPO_ROOT) before anything is signalled. A leftover that
# cannot be verified is simply deleted, so the directory does not accumulate dead
# branch names. The glob is second so the current name is handled first and the
# messages come out in the order the developer expects; the `-f` test skips the
# repeat (already removed) and the no-match literal when the dir does not exist.
for _pf in "$DEV_PIDFILE" "$REPO_ROOT"/.dev-pids/*; do
  [[ -f "$_pf" ]] || continue
  rec_pid="" rec_start="" rec_root=""
  { read -r rec_pid || true; read -r rec_start || true; read -r rec_root || true; } < "$_pf"
  if dev_pidfile_is_ours "$rec_pid" "$rec_start" "$rec_root"; then
    echo "==> reaping the dev.sh already running for this worktree (pid $rec_pid)"
    kill_tree_hard "$rec_pid"
    REAPED=1
  else
    echo "==> ignoring a stale dev.sh pidfile (pid ${rec_pid:-?} is gone or is not ours)"
  fi
  rm -f "$_pf"
done

# Wait for the port to actually free: the old server holds the bind for a beat
# after its process dies, and cli.py's port guard would SystemExit on it.
# Bounded — a port still busy after this belongs to something else, and the guard
# should say so loudly rather than have dev.sh hang here. Done once, after the
# whole loop, because every record above belongs to this worktree and so every
# reaped server was contending for the same port.
if [[ "$REAPED" -eq 1 && -n "$PORT" ]]; then
  for _ in $(seq 1 40); do
    port_is_open "$PORT" || break
    sleep 0.25
  done
fi

if [[ "$CLEANUP_ONLY" -eq 1 ]]; then
  if [[ "$REAPED" -eq 1 ]]; then
    echo "==> --cleanup: done, nothing left running for this worktree"
  else
    # Say what was NOT done, and why. A dev.sh started before this change wrote
    # no pidfile at all, which is the common case on the first upgrade — and
    # finding it would take a pattern match over every process on the machine,
    # which is precisely what must never happen here (other worktrees run their
    # own servers, and at least one deliberate orphan). Hand the developer the
    # search instead of guessing on their behalf.
    echo "==> --cleanup: no dev.sh recorded for this worktree — nothing reaped"
    echo "    (a dev.sh started before this pidfile existed leaves no record;"
    echo "     find it with: pgrep -laf dev.sh, and check its worktree first)"
  fi
  exit 0
fi

# Record this run. Written after the reap so the dying predecessor's own EXIT
# trap (which removes the pidfile) cannot delete the entry we just made —
# kill_tree_hard does not return until that process is gone. The start time goes
# in alongside the pid so a recycled pid can be recognized and left alone.
mkdir -p "$REPO_ROOT/.dev-pids"
SELF_START="$(ps -o lstart= -p $$ 2>/dev/null | sed -e 's/^ *//' -e 's/ *$//')"
if [[ -z "$SELF_START" ]]; then
  # dev_pidfile_is_ours fails closed without a start time, so the record would
  # be inert: the next run would call it stale and start a second tree instead
  # of restarting. Better to lose the auto-restart than to reap on two weaker
  # checks, but the developer should know why `dev.sh` stopped replacing itself.
  echo "==> WARNING: could not read this process's start time (ps -o lstart=)," >&2
  echo "    so a later run will not be able to verify this pidfile and will not" >&2
  echo "    reap this dev.sh. Stop it with Ctrl-C, or scripts/dev.sh --cleanup" >&2
  echo "    will report nothing to do." >&2
fi
printf '%s\n%s\n%s\n' "$$" "$SELF_START" "$REPO_ROOT" > "$DEV_PIDFILE"

# Armed from here on, so an abort anywhere in the setup below (a failed npm
# install, a Ctrl-C during the venv bootstrap) still clears the pidfile and
# reaps whatever had started.
trap 'dev_shutdown' EXIT
trap 'dev_shutdown; exit 130' INT
trap 'dev_shutdown; exit 143' TERM

# Read core templates straight from the repo, skipping the stage-into-home copy
# (~/.fused-render/.core-templates). Without this the server serves the last
# version-staged snapshot, so template edits wouldn't show until a version bump
# or a manual wipe. Respect an already-set value so the caller can override.
export FUSED_RENDER_CORE_TEMPLATES="${FUSED_RENDER_CORE_TEMPLATES:-$REPO_ROOT/fused_render/templates}"

# Stage the builtin-mount zips (learn.zip, sessions.zip) so a
# dev server gets the Learn/Sessions sub-apps just like the packaged
# app. The packaged builds create these at DMG/installer time (build_dmg.sh
# step 4e and its Windows mirror); a dev checkout only has the loose content
# dirs, so without this the builtin mounts never resolve a zip and the sidebar
# entries hide. The staging itself lives in scripts/stage_builtin_zips.sh
# (gitignored .dev-zips/ output, same exclusions as the packaged zips) because
# TWO callers need it: this startup pass, and dev_server_run.sh before every
# watchfiles server restart — which is what makes a core_apps/ edit land in a
# fresh zip that the restarting server force-remounts (the mount serves a zip
# SNAPSHOT, never the live dir; the core_apps poll loop below turns any edit
# into such a restart). Respect an already-set env var so a caller can point a
# mount at their own zip (stage_builtin_zips.sh skips those too).
DEV_ZIPS="$REPO_ROOT/.dev-zips"
bash "$REPO_ROOT/scripts/stage_builtin_zips.sh"
for pair in learn:FUSED_RENDER_LEARN_ZIP sessions:FUSED_RENDER_SESSIONS_ZIP; do
  name="${pair%%:*}" var="${pair#*:}"
  if [[ -z "${!var:-}" && -f "$DEV_ZIPS/$name.zip" ]]; then
    export "$var=$DEV_ZIPS/$name.zip"
    echo "==> staged builtin zip: $name.zip"
  fi
done

# Keep the rclone rcd daemon (and its mounts + warm VFS cache) alive across the
# watchfiles server restarts that fire on every .py edit — without this the
# daemon dies with the server (production teardown) and each restart pays the
# re-mount + cache re-warm cost. Production leaves this unset. Respect an
# already-set value so the caller can override (including to "0" to force the
# production dies-with-server behavior).
export FUSED_RENDER_RCLONE_PERSIST="${FUSED_RENDER_RCLONE_PERSIST:-1}"

# Python: active venv first, then the repo-local .venv. With neither, bootstrap
# a repo-local .venv (with the `dev` + `fused` + `bundled` extras) so a fresh
# worktree is self-contained. Without this the fallback was bare `python3` on
# PATH, whose fused_render resolves to whatever global/editable install happens
# to be there (often the main checkout's) and whose site-packages lack the sci
# deps the map/geotiff/zarr daemons need — a fresh worktree would silently run
# the wrong code or fail at daemon spawn. `-e` keeps the install pointed at this
# worktree's source. Uses uv when present (fast, no ensurepip dance), else
# stdlib venv+pip.
#
# `dev` is in the list even though this script only ever *runs* the server, not
# the suite: it carries the platform bindings the server itself needs to light
# up its OS-facing features (on macOS, the pyobjc that the clipboard bridge
# reads NSPasteboard through). Leaving it out is how the Finder-paste bridge
# came to report `supported: false` on every dev machine while the tests that
# would have caught it skipped for the same missing dependency.

# The interpreter version a dev venv is built on (D214), and NOT merely a
# preference: the server hands its own interpreter to `fused`'s venv builder as the
# base for every PEP 723 script venv, so this version decides which wheels those
# venvs can resolve. A bare `uv venv` takes uv's default, which is the newest
# CPython it knows about — and a .venv built on 3.14 made every script venv cp314,
# so a script declaring tensorflow (no cp314 wheels) hit an unresolvable dead end
# that no rebuild could repair. 3.12 is what all three packaged builds already
# ship, so a dev checkout on it behaves like the shipped app rather than like a
# machine nobody tests on.
DEV_PYTHON_VERSION="3.12"

# Is the venv at $1 on DEV_PYTHON_VERSION? Asked of the venv's OWN interpreter
# rather than inferred from a `lib/python3.x` directory name, because that is the
# interpreter the server will actually run under.
venv_is_pinned_version() {
  local found
  found="$("$1/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  [[ "$found" == "$DEV_PYTHON_VERSION" ]]
}

# Install the pinned dependency set into $1 (a venv dir). Runs from REPO_ROOT
# with the `.[extras]` form: uv rejects an absolute path carrying extras (parses
# it as a PEP508 requirement). Shared by the bootstrap and the staleness resync
# below so the two can never drift on which extras a dev venv carries.
install_python_deps() {
  if command -v uv >/dev/null 2>&1; then
    (cd "$REPO_ROOT" && uv pip install --python "$1/bin/python" -e ".[dev,fused,bundled]")
  else
    (cd "$REPO_ROOT" && "$1/bin/python" -m pip install -e ".[dev,fused,bundled]")
  fi
}

if [[ -n "${VIRTUAL_ENV:-}" ]]; then
  PY="$VIRTUAL_ENV/bin/python"
  VENV_DIR="$VIRTUAL_ENV"
  # WARN, never rebuild. An activated venv is the developer's explicit choice and
  # may hold work this script knows nothing about; destroying it to enforce a pin
  # would be a far worse surprise than running on the wrong version. The server
  # still copes — it resolves a uv-managed 3.12 for script venvs (D214) — so this
  # is a heads-up about a download, not a broken setup.
  if ! venv_is_pinned_version "$VIRTUAL_ENV"; then
    echo "==> NOTE: the active venv is not Python $DEV_PYTHON_VERSION." >&2
    echo "    PEP 723 script venvs are pinned to $DEV_PYTHON_VERSION, so the server will" >&2
    echo "    resolve a uv-managed $DEV_PYTHON_VERSION for them and download one on first" >&2
    echo "    use if this machine has none. Deactivate to use $REPO_ROOT/.venv," >&2
    echo "    or rebuild this venv on $DEV_PYTHON_VERSION, to avoid that." >&2
  fi
elif [[ -x "$REPO_ROOT/.venv/bin/python" ]] && venv_is_pinned_version "$REPO_ROOT/.venv"; then
  PY="$REPO_ROOT/.venv/bin/python"
  VENV_DIR="$REPO_ROOT/.venv"
else
  # A wrong-version .venv is REBUILT rather than adopted, and that is the half of
  # this change that actually reaches existing checkouts: the branch above used to
  # adopt `.venv` on existence alone, so pinning the creation below would have
  # fixed nothing on any machine that already had one — including the machine the
  # tensorflow report came from. Safe to delete because the directory is
  # gitignored and its entire contents are what `install_python_deps` puts there;
  # same reasoning as the dependency resync further down, which already rewrites
  # it unasked.
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    echo "==> $REPO_ROOT/.venv is not Python $DEV_PYTHON_VERSION — rebuilding it"
    rm -rf "$REPO_ROOT/.venv"
  else
    echo "==> no venv found — creating $REPO_ROOT/.venv with the [dev,fused,bundled] extras"
  fi
  if command -v uv >/dev/null 2>&1; then
    # --python: a bare `uv venv` takes uv's newest known CPython, which is how a
    # 3.14 dev venv came to exist in the first place. uv downloads a managed
    # interpreter here if the machine has none.
    uv venv --python "$DEV_PYTHON_VERSION" "$REPO_ROOT/.venv"
  else
    # No uv, so no interpreter downloads: the pinned python has to already be here.
    # Failing loudly beats silently building on `python3` — that is the exact
    # substitution this whole change exists to undo.
    BASE_PY="$(command -v "python$DEV_PYTHON_VERSION" || true)"
    if [[ -z "$BASE_PY" ]]; then
      echo "FATAL: need Python $DEV_PYTHON_VERSION to create $REPO_ROOT/.venv, and neither" >&2
      echo "       uv nor python$DEV_PYTHON_VERSION is on PATH." >&2
      echo "       Install uv (https://docs.astral.sh/uv/) — it will fetch $DEV_PYTHON_VERSION" >&2
      echo "       itself — or install python$DEV_PYTHON_VERSION and re-run." >&2
      exit 1
    fi
    "$BASE_PY" -m venv "$REPO_ROOT/.venv"
    "$REPO_ROOT/.venv/bin/python" -m pip install --upgrade pip
  fi
  install_python_deps "$REPO_ROOT/.venv"
  PY="$REPO_ROOT/.venv/bin/python"
  VENV_DIR="$REPO_ROOT/.venv"
  # Stamp here too, or the freshly-bootstrapped venv would immediately look
  # unstamped to the resync below and install the same set a second time.
  touch "$REPO_ROOT/.venv/.fused-render-deps"
fi

# Resync the venv when pyproject.toml has moved on since the last install — the
# Python half of the `package-lock.json -nt node_modules/.package-lock.json`
# check below, and load-bearing for the same reason. Before this, the venv was
# adopted on *existence* alone, so bumping a pin re-synced nothing on any
# machine that already had a .venv: `.venv` sat on `fused` 2.9.3.post3 while
# pyproject pinned post13, and post3's local compute backend spawns its runner
# with `cwd=` + the default close_fds — a fork(), which trips PROJ's
# pthread_atfork handler and SIGSEGVs before exec. Every /api/run died with a
# bare `Runner exited with code -11` and no traceback, in code that had not
# changed; worktrees ranged across post3/post7/post10/post13 purely by setup
# date. The `import fused_render` guard below cannot see this — it passes
# happily against a stale dependency set.
#
# A missing stamp reads as stale (`-nt` is true when the target is absent), so
# venvs predating this check — and any venv restored, copied, or half-built
# without one — self-heal on the next run instead of needing a manual install.
# The stamp is only touched AFTER a successful install; with `set -e` a failed
# resync aborts dev.sh, so a broken sync can never be recorded as a good one.
DEPS_STAMP="$VENV_DIR/.fused-render-deps"
if [[ ! -e "$DEPS_STAMP" ]]; then
  echo "==> syncing python deps into $VENV_DIR (no install stamp yet)"
  install_python_deps "$VENV_DIR"
  touch "$DEPS_STAMP"
elif [[ "$REPO_ROOT/pyproject.toml" -nt "$DEPS_STAMP" ]]; then
  echo "==> syncing python deps into $VENV_DIR (pyproject.toml changed since last install)"
  install_python_deps "$VENV_DIR"
  touch "$DEPS_STAMP"
fi

command -v npm >/dev/null || { echo "npm not found — the dev loop needs Node 22"; exit 1; }
"$PY" -c "import fused_render" 2>/dev/null || {
  echo "fused_render not importable from $PY — run: pip install -e \".[dev,fused,bundled]\""
  exit 1
}

# Header-less scripts (no pyproject.toml — every core_apps/ helper) run on
# "the app's own interpreter" under the fused engine, and
# engine.py resolves that by probing candidates. In a dev checkout the probe
# can reject everything (sys.executable nuances, PATH pythons without the
# bundled set) and every builtin sub-app's runPython dies with "could not
# resolve a usable Python interpreter". The dev venv IS the app interpreter
# here — it carries [bundled] — so point the escape hatch at it. Respect an
# already-set value.
export FUSED_RENDER_APP_PYTHON="${FUSED_RENDER_APP_PYTHON:-$PY}"

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
# $! is the SUBSHELL, not vite: the real chain is subshell -> npm -> node vite.
# dev_shutdown walks down from here with kill_tree; killing this pid alone is
# exactly what left three `vite build --watch` nodes running under init for
# weeks. The traps are already armed (see the cleanup section at the top) and
# read WATCH_PID lazily, so there is nothing to re-install here.
WATCH_PID=$!

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
  # Decide whether to open a browser tab. The server runs with --no-browser
  # under the reloader, so dev.sh opens the tab exactly once. $PORT is already
  # resolved (dev_effective_port, at the top) — the startup reap needs the same
  # number to wait on, and deriving it twice is how the two would drift.
  NO_BROWSER=0
  for a in "$@"; do
    case "$a" in
      --no-browser) NO_BROWSER=1 ;;
    esac
  done
  # Second attempt for the case the early derivation warned about: back then
  # there may have been no interpreter at all, and by now the venv bootstrap has
  # made one. Without this the opener would silently never fire on a
  # freshly-created checkout.
  if [[ -z "$PORT" ]]; then
    PORT="$(dev_effective_port "$@")"
    # Spelled as an `if`, not `[[ … ]] && echo`: under `set -e` that idiom
    # aborts the script whenever the test is false and it is the last command
    # in the enclosing block.
    if [[ -z "$PORT" ]]; then
      echo "==> NOTE: dev port still unresolved — not opening a browser tab" >&2
    fi
  fi

  # One-shot opener: wait for the port to accept a connection, then open the tab.
  if [[ "$NO_BROWSER" -eq 0 && -n "$PORT" ]]; then
    (
      ready=0
      for _ in $(seq 1 120); do
        if "$PY" -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(0 if s.connect_ex(('127.0.0.1', $PORT))==0 else 1)" 2>/dev/null; then
          ready=1
          break
        fi
        sleep 0.5
      done
      # Only open if the server actually came up — otherwise (e.g. the port
      # guard SystemExited on a stale server) we'd pop a dead tab after timeout.
      if [[ "$ready" -eq 1 ]]; then
        URL="http://127.0.0.1:$PORT/"
        # Open via Python's webbrowser (cross-platform, matches cli.py); a shell
        # open/xdg-open/start chain misses Windows/git-bash (start is a cmd
        # builtin, not a binary on PATH). Pass the URL through argv, not
        # interpolated into the -c source: a path with an apostrophe
        # (e.g. a home dir containing ') would otherwise break the string literal.
        "$PY" -c "import sys, webbrowser; webbrowser.open(sys.argv[1])" "$URL" >/dev/null 2>&1 || true
      fi
    ) &
    OPENER_PID=$!
  fi

  # watchfiles wants the target as a single shell-command string, then the watch
  # paths. printf %q quotes $PY and each passthrough arg so paths/args with
  # spaces survive. --filter python watches only *.py under the whole
  # fused_render/ tree, so vite's shell-dist output (.html/.js/.css) never
  # triggers a restart. Editing a template UDF (fused_render/templates/**/*.py)
  # triggers a harmless extra server restart; we don't exclude templates/
  # because watchfiles' --ignore-paths matches by bare prefix (filters.py:
  # startswith), so it would also hide the imported sibling templates_api.py.
  #
  # --no-browser goes AFTER the passthrough args: cli.py's main() injects a
  # default `serve` only when argv[0] isn't already a subcommand, so a leading
  # `serve` (e.g. `dev.sh serve --port N`) must stay argv[0]. Prepending
  # --no-browser would shift it and trigger a duplicate-`serve` parse error.
  # core_apps content watcher: watchfiles' python filter only reacts to *.py,
  # but builtin sub-app edits are mostly .html/.md/.svg. Poll for ANY newer
  # file under core_apps/ and poke a gitignored .py trigger inside the watched
  # tree — watchfiles sees a .py change, restarts the server through
  # dev_server_run.sh, which re-stages the zips first. Net effect: save a file
  # under core_apps/, the server restarts on fresh zips, a browser refresh
  # shows the edit. ~2s poll; core_apps is tiny.
  CORE_TRIGGER="$REPO_ROOT/core_apps/.dev-reload-trigger.py"
  touch "$CORE_TRIGGER"
  (
    # *.py excluded: watchfiles' python filter already restarts on those (and
    # the restart re-stages zips via dev_server_run.sh) — poking the trigger
    # too would queue a SECOND restart for the same edit. Also skip junk that
    # staging never packages anyway (.DS_Store, editor swaps, *.html.json
    # sidecars) so it can't force pointless restarts.
    while sleep 2; do
      if [[ -n "$(find "$REPO_ROOT/core_apps" -type f \
                    ! -name "*.py" ! -name ".DS_Store" ! -name "*~" \
                    ! -name "*.swp" ! -name "*.swx" ! -name "*.json.tmp" \
                    ! -name "*.html.json" ! -name "*.md.json" ! -name "*.py.json" \
                    ! -name "*.txt.json" ! -name "*.markdown.json" \
                    ! -path "*/__pycache__/*" \
                    -newer "$CORE_TRIGGER" -print -quit 2>/dev/null)" ]]; then
        touch "$CORE_TRIGGER"
      fi
    done
  ) &
  # Another subshell pid, and this one has a `sleep` child of its own — same
  # kill_tree treatment as the vite watch above.
  CORE_WATCH_PID=$!

  # The restart target is dev_server_run.sh (re-stage builtin zips, then exec
  # the server) so every restart mounts current core_apps content — not the
  # snapshot from dev.sh launch time.
  CMD="bash $(printf '%q' "$REPO_ROOT/scripts/dev_server_run.sh") $(printf '%q' "$PY")"
  for a in "$@"; do CMD+=" $(printf '%q' "$a")"; done
  CMD+=" --no-browser"
  # BACKGROUNDED, then waited on — not run in the foreground, which is how this
  # whole tree used to survive `kill <dev.sh>`: bash defers a trap handler until
  # the current foreground command returns, so the signal only queued a handler
  # that never got to run, and watchfiles + the server it supervises were in no
  # trap anyway. `wait` is interruptible, so the handler fires immediately.
  "$PY" -m watchfiles --filter python "$CMD" "$REPO_ROOT/fused_render" "$REPO_ROOT/core_apps" &
  SERVER_PID=$!
  # `wait` returns the child's status and a signalled child returns non-zero, so
  # `set -e` would abort here on an ordinary Ctrl-C; the status is captured
  # instead and re-raised below, which keeps a genuinely failing server failing.
  set +e
  wait "$SERVER_PID"
  SERVER_STATUS=$?
  set -e
  SERVER_PID=""
  exit "$SERVER_STATUS"
else
  # Original single-launch behavior: the server opens its own browser tab.
  # Backgrounded + waited on for the same reason as the reload path above: a
  # foreground server defers the trap, and the vite watch started earlier would
  # be left running.
  "$PY" -m fused_render.cli "$@" &
  SERVER_PID=$!
  set +e
  wait "$SERVER_PID"
  SERVER_STATUS=$?
  set -e
  SERVER_PID=""
  exit "$SERVER_STATUS"
fi
