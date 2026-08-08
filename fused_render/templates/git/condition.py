"""Gate for the `git` template (SPEC CT-12, §33 / GT-3).

`main(path)` decides whether a path is inside a git work tree.

The answer is: anything inside one, file or directory alike. That is the WHOLE
rule now, and it used to be two rules with a set of exclusions bolted on.

`git` is the working-tree view — staging, discarding, stashing, committing,
branches, push/pull. The commit LOG belongs to `versions`, which renders the
same commits with a timeline this view never had. Because they answer different
questions, they no longer need to take turns: this gate used to refuse a fused
app folder ("versions handles app history") and its peer used to refuse every
directory that was not one, each with a comment telling the reader to keep the
two in step. Both refusals are gone, and the pair is simply offered together
(the registry binds them together too).

The binding was the other half of the same mistake. `git` sat on the universal
"/" DIRECTORY key alone, while the explorer gives a FOLDER no mode switcher of
its own — the only mode surface a browsing user has is the preview pane's, and
the pane acts on the SELECTED ROW, which is usually a file. Bound to no file
extension, the view was effectively unreachable without hand-writing
`?_mode=git`. It is now offered on every key `versions` is.

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

2. **Is there a directory to ask git FROM?** A directory asks about itself; a
   file asks from its parent (handing git a file as `-C`/`cwd` is an ENOTDIR,
   not an answer). Two `os.path.isdir` stats at most.

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
        # and then a git-backed registered linked app, on the grounds that
        # `versions` rendered the same history — one relpath + a `.git` stat for
        # the first, a whole second `rev-parse` fork for the second, both of
        # them on every directory the user opened. `git` is the working tree and
        # `versions` is the history; they answer different questions, so a
        # folder gets both and the two forks go away with the rules that needed
        # them.

        # (2) The directory to ask git from: the path itself, or a file's parent.
        # Two stats at most, never a listing.
        if os.path.isdir(path):
            cwd = path
        else:
            cwd = os.path.dirname(os.path.abspath(path))
            if not os.path.isdir(cwd):
                return False

        # (3) git is the authority. `--is-inside-work-tree` is false for a bare
        # repo and inside `.git`, which is what we want; exit 128 ("not a git
        # repository") is the ordinary negative and lands in the same False.
        proc = subprocess.run(
            ["git", "--no-pager", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            env={**os.environ, **_ENV},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_TIMEOUT_S,
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if sys.platform == "win32" else 0),
        )
        if proc.returncode != 0:
            return False
        return proc.stdout.strip() == b"true"
    except Exception:  # noqa: BLE001 — any probe error: fail closed, quietly
        return False
