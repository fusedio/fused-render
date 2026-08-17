"""Gate for the `git` template (SPEC CT-12, §33 / GT-3).

`main(path)` decides whether a path is a DIRECTORY inside a git work tree.

The answer is: a directory inside one, and nothing else. `git` is a FOLDER-ONLY
mode.

`git` is the working-tree view — staging, discarding, stashing, committing,
branches, push/pull. Every one of those is a REPOSITORY-level action, not
something you do to one file: you do not stash a file, you stash a tree, and the
working tree a file sits in is its FOLDER's working tree, not the file's. So the
mode belongs to the folder and is offered there alone. The per-file question —
which commits touched THIS path, and what did the file look like at one of them —
is answered by this same view rather than by a mode of its own: the commit list is
SCOPED to the open target, and opening a commit renders the file as of it. There
is nothing left for a per-file binding to add, so folder-only costs nothing.

The gate and the binding say the same thing twice, on purpose. The registry drops
`git` from every file extension and keeps it on the universal "/" DIRECTORY key;
this gate refuses anything that is not a directory. A hand-written `?_mode=git`
on a file therefore renders nothing the user asked for, and the runtime modules
still tolerate a file target rather than crash on one (MD-11: the gate is the UX,
the module is the guarantee).

The reason `git` was once bound to file keys is gone. It used to be that the
explorer gave a FOLDER no mode switcher of its own — the only mode surface a
browsing user had was the preview pane's, and the pane acted on the SELECTED ROW,
which was always a file — so a mode bound to "/" alone was unreachable without
hand-writing a URL, and riding along on file keys was the workaround. The preview
pane now selects and previews FOLDER rows too (the folder peek), so a folder's
mode switcher is reachable and the workaround can go.

Two questions, in this order, because the first is a refusal rather than a
preference:

1. **Is the path mount-backed?** Then False, always, and the refusal happens
   BEFORE any subprocess. `graph/condition.py` refuses one for the shape of I/O
   it would cause, and this is the same shape for a worse reason: the reader
   shells out to `git status` / `git log` on the path, and git over an
   rclone-NFS mount stats and lists its way through the work tree — the exact
   pattern that wedges a flat million-key S3 prefix. So the mode is never
   OFFERED on a mount, and `log.py` also refuses a mount-backed target outright,
   so a hand-written `_mode=git` URL cannot reach git either. The gate is the
   UX; the module is the guarantee (MD-11).

   The detector is the app's own rule via `../shared/appenv.is_mount_backed`,
   the same mechanism `graph/condition.py` uses — not a second copy. If that
   import fails we cannot tell, and "cannot tell" must read as "refuse".

2. **Is the path a directory?** If not, False — a file is never offered this
   mode, so there is nothing to ask git about. (This also happens to be what
   git's own CLI needs: handing it a file as `-C`/`cwd` is an ENOTDIR, not an
   answer.) One `os.path.isdir` stat, never a listing.

   Then: **does git say this is inside a work tree?** `git rev-parse
   --is-inside-work-tree`, one bounded subprocess, and its literal `true` is the
   only answer that passes.

CRITICAL: this never enumerates (`os.listdir`, `os.scandir`, `glob`, recursion)
and never walks the tree looking for a marker — the rule `zarr_aoi/condition.py`
documents, and load-bearing here because the gate runs on every directory the
user opens. It is also why the CLI is the authority
rather than a `.git` probe: a `.git` entry exists only at the repository ROOT,
so a probe would have to ASCEND to answer a nested path (`repo/pkg/` has no
`.git` of its own), and the two shapes it would then have to know about — a
`.git` *directory* in a clone, a `.git` *file* in a linked worktree or submodule
— are exactly the cases a hand-rolled probe gets wrong. `rev-parse` answers all
of them, from any depth, in one fork; git's own ascent is O(depth) stats, never a
descent. A `.git` stat fast path was considered and dropped: it could only ever
turn a correct answer into a guess, and one exec is already cheap.

Deliberately True for an **empty repository** (initialized, no commits): it IS a
repository, and the view has a real empty state for it (GT-9) — refusing would
hide the mode on exactly the fresh project where "what have I got so far" is
most useful. Deliberately False for a **bare repository** and for anything inside
`.git`: no work tree means no `git status` and no path to scope history to, so
`--is-inside-work-tree` says false and so do we — not offered rather than
offered-then-broken.

Fails closed: a missing git binary, a timeout, a non-zero exit, stdout that
isn't literally `true`, an unreadable path, any exception at all → False.

Fails closed but no longer SILENTLY. Answering False for both "this is not a
repository" (the common, correct case) and "git ran and refused" made the two
indistinguishable, and the second one hides the Git panel on EVERY repository at
once with nothing in the log — a real investigation concluded "not reproducible"
from that silence. So git's stderr is captured, and a negative that CONTRADICTS
the filesystem (a non-zero exit whose words are not "not a git repository", or
exit zero with a negative answer for a directory that has a `.git`) is logged
once a minute with the process's git environment, which is where a stray
`GIT_DIR` — invisible from outside — would show up. The verdict is unchanged;
only the silence is. See `_warn_suspicious_negative`.

Self-contained apart from `../shared/appenv.py` (itself stdlib-only, env vars
only) — the module is exec'd standalone (not imported as part of a package), so
nothing here imports fused_render.
"""

# Hard ceiling on the one subprocess. `rev-parse` is a local plumbing command
# with no network step, so a second is already generous; the bound exists for the
# pathological case (a stalled filesystem under the ascent), and the gate's own
# host bounds it again with GATE_PROBE_BUDGET_S.
_TIMEOUT_S = 2.0

# Non-interactivity, as environment. Nothing here reaches a remote, but a repo
# can carry config that makes even a local command ask a human something, and a
# gate that blocks on a prompt blocks the stat pipeline behind it.
#
# Deliberately NOT disabling the user's git config (no GIT_CONFIG_GLOBAL to
# /dev/null): `safe.directory` lives there, and a repo the user has explicitly
# marked safe must keep working here. The knobs that could corrupt our parsing
# are overridden per-command with `-c` instead (see log.py).
_ENV = {
    "GIT_TERMINAL_PROMPT": "0",   # never prompt for credentials
    "GIT_OPTIONAL_LOCKS": "0",    # never take a lock just to answer a question
    "GIT_PAGER": "cat",           # a pager on a pipe would deadlock
    "GIT_ASKPASS": "",            # no GUI/askpass helper
    "SSH_ASKPASS": "",
    "GCM_INTERACTIVE": "Never",   # git-credential-manager
    "GIT_LFS_SKIP_SMUDGE": "1",   # never fetch an LFS object to answer this
}

# How often a negative that looks WRONG may be logged. The gate answers on every
# directory the user opens, so an unthrottled line would be one per stat.
_WARN_INTERVAL_S = 60.0

# The environment that can make a healthy git answer about the WRONG repository,
# plus the two that decide which config it reads. `git -C <repo> rev-parse`
# honours `GIT_DIR` over `-C`, so a stray value here silently redirects the
# question — and it is invisible from outside the process, which is exactly why
# it belongs in the log line.
_REPORT_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
    "HOME", "XDG_CONFIG_HOME",
)

# git's own words for the one negative that is ORDINARY. Everything else it can
# say on the way to a non-zero exit — `detected dubious ownership`, a bad config,
# `cannot get current working directory` — is a reason the mode disappeared that
# a human needs to see. The exit code cannot tell them apart (all 128); the words
# can.
_ORDINARY_NEGATIVES = ("not a git repository",)

# THE bug this gate shipped, and the reason the Git panel vanished for every
# repository at once. With libproj resident in the host process — and it becomes
# resident the moment any map / geotiff / zarr template or daemon imports
# rasterio or pyproj — a plain fork() runs PROJ's pthread_atfork child handler
# into a SIGSEGV *before* exec. The child dies with signal 11: `returncode ==
# -11`, empty stdout, empty stderr, and NO exception, because the spawn itself
# succeeded. This gate then answered False on a git that never ran.
#
# CPython avoids fork only when EVERY clause of this holds
# (`subprocess.py::_execute_child`):
#
#     _USE_POSIX_SPAWN and os.path.dirname(executable) and preexec_fn is None
#     and not close_fds and not pass_fds and cwd is None and ... and umask < 0
#
# This gate already passed `close_fds=False`, which is why it looked correct. It
# was violating two other clauses: argv[0] was the bare name "git" (dirname "" —
# falsy) and it passed `cwd=`. Either one alone forces the fork path. All three
# parts are required together; `tests/test_git_posix_spawn.py` pins them.
_GIT_BIN = None


def _git_bin():
    """An absolute path to git, resolved once per exec of this module.

    The module is re-exec'd per stat so the cache is short-lived, and that is
    fine: `shutil.which` is a handful of stats and the alternative — a bare name
    — is what forks. The bare-name fallback keeps a PATH-less environment raising
    the FileNotFoundError the caller already handles.
    """
    global _GIT_BIN
    if _GIT_BIN is None:
        import shutil
        _GIT_BIN = shutil.which("git") or "git"
    return _GIT_BIN


def _warn_suspicious_negative(kind, path, detail):
    """Log a negative that contradicts the filesystem, at most once a minute.

    Why this exists: a gate that answers False both for "this is not a
    repository" (the common, correct case) and for "git ran and refused" is
    undiagnosable, and it disables the Git side panel for EVERY repository with
    nothing whatsoever in the log. A real investigation concluded "not
    reproducible" from that silence.

    The throttle state cannot live in a module global: `_run_condition` re-execs
    this file on every stat, so a global would be reset before it was ever read.
    It rides on the `logging.Logger` object instead, which the logging manager
    caches for the life of the PROCESS — the lifetime the throttle is about.

    stdlib only, like the rest of this module (SPEC PY-15), and it never changes
    the verdict: every caller has already decided to fail closed.
    """
    try:
        import logging
        import os
        import time

        log = logging.getLogger("fused_render.templates.git.condition")
        attr = "_fused_warned_" + kind
        now = time.monotonic()
        last = getattr(log, attr, None)
        if last is not None and now - last < _WARN_INTERVAL_S:
            return
        setattr(log, attr, now)

        env = [f"{k}={os.environ[k]!r}" for k in _REPORT_ENV if os.environ.get(k)]
        try:
            env.append(f"cwd={os.getcwd()!r}")
        except OSError as exc:   # the cwd was removed under this process
            env.append(f"cwd=UNAVAILABLE ({exc})")
        log.warning(
            "the git mode is being hidden for %s and it looks wrong: %s — git "
            "ran, so this is not a spawn problem. This process's git "
            "environment is: %s",
            path, detail, ", ".join(env) or "no git-related environment set")
    except Exception:  # noqa: BLE001 — a gate must never fail because of its log
        pass


def main(path: str) -> bool:
    import os
    import subprocess
    import sys

    try:
        # (1) A mount-backed path is refused before ANY subprocess is forked.
        #
        # Through `shared/appenv` (env vars only, stdlib only) rather than by
        # importing fused_render, so the mount rule has ONE home for every
        # template — this gate happens to be exec'd in-process by
        # server._run_condition, where the package IS importable, but that is an
        # implementation detail of the gate's host and not something a template
        # may rely on (SPEC PY-15).
        shared = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
        # Guarded insert: _run_condition re-execs this module on EVERY stat, so
        # an unconditional insert would grow sys.path without bound.
        if shared not in sys.path:
            sys.path.insert(0, shared)
        try:
            from appenv import is_mount_backed
        except Exception:  # noqa: BLE001 — cannot tell -> refuse (CT-12)
            return False
        if is_mount_backed(path):
            return False

        if not path:
            return False

        # NO peer exclusions here. This gate used to refuse a fused app folder,
        # and then a git-backed registered linked app, on the grounds that a
        # SEPARATE per-path history mode already rendered their history — one
        # relpath + a `.git` stat for the first, a whole second `rev-parse` fork
        # for the second, both of them on every directory the user opened. That
        # peer is gone and this view answers the history question itself, so
        # there is no longer anything to defer to: a work tree is a work tree
        # whoever else claims the folder, and the two forks go with the rules
        # that needed them.

        # (2) Folder-only: a file is refused outright. This used to fall back to
        # the file's PARENT directory, back when `git` was offered on file keys
        # and the pane's mode surface only ever pointed at a file. It is not a
        # fallback any more, it is the rule: the working tree is the folder's,
        # so the folder is what gets asked. One stat, never a listing.
        if not os.path.isdir(path):
            return False
        cwd = path

        # (3) git is the authority. `--is-inside-work-tree` is false for a bare
        # repo and inside `.git`, which is what we want; exit 128 ("not a git
        # repository") is the ordinary negative and lands in the same False.
        # argv[0] ABSOLUTE and NO `cwd=`, both load-bearing — see _git_bin.
        # `-C cwd` already pins the repository, and it is stricter than `cwd=`
        # was: it cannot be changed by this process's working directory.
        proc = subprocess.run(
            [_git_bin(), "--no-pager", "-C", cwd,
             "rev-parse", "--is-inside-work-tree"],
            env={**os.environ, **_ENV},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            # CAPTURED, not discarded. This is the only place git says WHY it
            # refused, and discarding it is what made "the Git panel is gone for
            # every repository" indistinguishable from "this is not a
            # repository". `rev-parse` writes one short line, so there is no
            # pipe-filling risk.
            stderr=subprocess.PIPE,
            timeout=_TIMEOUT_S,
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if sys.platform == "win32" else 0),
            close_fds=False,
        )
        if proc.returncode != 0:
            said = proc.stderr.decode("utf-8", "replace").lower()
            if not any(phrase in said for phrase in _ORDINARY_NEGATIVES):
                _warn_suspicious_negative(
                    "refused", path,
                    "git exited %s saying %r" % (
                        proc.returncode,
                        proc.stderr.decode("utf-8", "replace").strip()
                        or "(nothing)"))
            return False
        if proc.stdout.strip() == b"true":
            return True
        # Exit ZERO with a negative answer. Ordinary for a bare repo and inside
        # `.git`; NOT ordinary for a directory carrying a `.git` of its own,
        # which is the shape a stray GIT_DIR produces — and it leaves no stderr
        # at all, so the environment is the only thing that can explain it. One
        # stat, only on the negative, and it never changes the verdict.
        try:
            has_git = os.path.exists(os.path.join(path, ".git"))
        except OSError:
            has_git = False
        if has_git:
            _warn_suspicious_negative(
                "contradicted", path,
                "it has a .git entry but `rev-parse --is-inside-work-tree` "
                "answered %r" % proc.stdout.decode("utf-8", "replace").strip())
        return False
    except Exception:  # noqa: BLE001 — any probe error: fail closed, quietly
        return False
