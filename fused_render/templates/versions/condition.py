"""Gate for the `versions` template — the HISTORY view, for anything inside a
git work tree.

`main(path)` asks git itself one question — `rev-parse --is-inside-work-tree`,
from the directory when the target is one and from the parent when it is a file
(the probe `git/condition.py` documents at length). A file, a nested folder, a
repository root: all of them have a timeline, so all of them are offered it.

This used to be two rules and a carve-out. A FILE passed anywhere in any work
tree, but a DIRECTORY passed only for a *fused app* — a folder exactly two
levels under the workspace that was itself a repository, or a git-backed
registered linked app — on the grounds that folder-wide history outside an app
was the `git` mode's story, and two modes for one story was to be avoided. That
reasoning is spent: `git` is the WORKING TREE view now (staging, discarding,
stashing, committing, branches) and draws no history at all, so there is no
second story to collide with. Its gate dropped the mirror-image exclusion at the
same time; the pair is now simply offered together, and the registry binds them
together.

Consequences of the widening, both already handled elsewhere:

* **Write authority is unchanged.** `versions.py` still refuses `revert` outside
  a fused app — that would write a commit with the Fused identity into what is
  the user's own repository (the rule `fused_render/linked_apps.py` sets for
  linked folders) — so the view is READ-ONLY there. Enforced by the module
  rather than by hiding the mode: the gate is the UX, the module is the
  guarantee (MD-11).
* **Auto-commits are unchanged.** Only real app folders get the `app_git.py`
  treatment; being *offered* a timeline has never implied being given one.

Constant-time, and never a listing (the rule `graph/condition.py` documents;
this gate runs on every file and directory the user opens): two stats and one
bounded fork, no relpath arithmetic and no `.git` probing left. Mount-backed
paths are refused outright, before any subprocess — git over an rclone-NFS
mount stats and lists its way through the work tree, the exact pattern that
wedges a flat million-key S3 prefix.

Fails closed: any exception returns False.
"""


def main(path: str) -> bool:
    import os
    import sys

    try:
        shared = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
        # Guarded insert: _run_condition re-execs this module on every stat.
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

        def _in_work_tree(cwd: str) -> bool:
            """`git rev-parse --is-inside-work-tree` asked from `cwd` — one
            bounded fork, never a search of the tree. The reasoning for asking
            git rather than stat'ing `.git` (a nested path has no `.git` of its
            own; a clone's `.git` is a dir and a worktree's is a file) lives in
            git/condition.py; this is the same probe, not a second copy of the
            rule."""
            import subprocess

            proc = subprocess.run(
                ["git", "--no-pager", "-C", cwd, "rev-parse",
                 "--is-inside-work-tree"],
                env={**os.environ,
                     "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
                     "GIT_PAGER": "cat", "GIT_ASKPASS": ""},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=2.0,
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if sys.platform == "win32" else 0),
            )
            return proc.returncode == 0 and proc.stdout.strip() == b"true"

        # One rule, for both shapes: is this inside a work tree? A DIRECTORY
        # asks about itself, a FILE asks from its parent (handing git a file as
        # `-C` is an ENOTDIR, not an answer).
        #
        # The elaborate app-dir and linked-app branches that used to stand here
        # are gone, and so is the plain-directory refusal they existed to carve
        # exceptions out of. That refusal said folder-wide history outside a
        # fused app was the `git` mode's story and two modes for one story was
        # to be avoided — but `git` is the WORKING TREE view now (staging,
        # committing) and does not draw history at all, so there is no second
        # story to collide with. A folder has a timeline like anything else,
        # wherever it lives, and this stops asking whether it is special first.
        #
        # `isdir` first and `isfile` second, deliberately never `not isdir`:
        # the loose form is also true for every path that does NOT EXIST, so a
        # missing name inside any repository would gate true. Same one-word trap
        # the peer gate documents (claude/condition.py), and "cannot tell" must
        # read as "refuse" (CT-12).
        if os.path.isdir(path):
            return _in_work_tree(path)
        if not os.path.isfile(path):
            return False
        parent = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(parent):
            return False
        return _in_work_tree(parent)
    except Exception:  # noqa: BLE001 — any probe error: fail closed, quietly
        return False
