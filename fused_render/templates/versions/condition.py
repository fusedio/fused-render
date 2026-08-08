"""Gate for the `versions` template — per-FILE history, plus app history on an
app FOLDER (D235).

`main(path)` answers for the two kinds of target the mode is bound to:

* **A FILE** → allowed when it is inside any git work tree, decided by git
  itself (`rev-parse --is-inside-work-tree` from the file's parent — the probe
  `git/condition.py` documents at length). `versions` is the file-side history
  view now that `git` is directory-only (D235), so a tracked file gets its
  timeline wherever it lives, not only inside a fused app. Outside an app the
  view is READ-ONLY — `versions.py` refuses `revert` there, because that writes
  a commit with the Fused identity into what is the user's own repository (the
  rule `fused_render/linked_apps.py` already sets for linked folders).
* **A DIRECTORY** → allowed only for a *fused app*: a folder exactly two levels
  under the workspace (`<workspace>/<tag>/<name>`, `shell/seed.fused_dir()`)
  that is itself a git repository, or a git-backed registered linked app. Only
  such folders get the auto-commit treatment (`fused_render/app_git.py`), so
  only they have a folder-level history worth showing — and this is what keeps
  the mode in the app-builder view (App.tsx APP_MODES) while an ordinary folder
  in the explorer offers the repo-wide `git` view instead.

The app-dir rule here mirrors `app_git.app_dir_for` (and the claude template's
`_app_dir_for`); keep the three in step. It is duplicated rather than imported
because a template must not import `fused_render` (SPEC PY-15 / D166) — the
workspace root travels as an env var via `../shared/appenv.py`.

Constant-time: one relpath computation and one `os.path.isdir` on `.git` —
never a listing (the rule `graph/condition.py` documents; this gate too runs
on every file and directory the user opens). Mount-backed paths are refused
outright: an app is by definition a local folder, and probing `.git` over a
kernel NFS mount is exactly the stat this gate must never issue.

Registered *linked apps* (FUSED_RENDER_LINKED_APPS) pass too, when git-backed:
their history is worth SHOWING like any app's. Git-backed is decided by git
itself (`rev-parse --is-inside-work-tree`, the git/condition.py probe), not a
`.git` stat on the folder — a linked folder is often a subfolder of the user's
repository, with `.git` at an ancestor. But only the read side — the
backend (versions.py) refuses `revert` for a linked folder, because that
writes a commit with the Fused identity into what is the user's OWN
repository (see fused_render/linked_apps.py). The same reasoning keeps linked
folders out of app_git.app_dir_for and the claude agent's _commit_turn sweep
entirely — no auto-commits.

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
            from appenv import is_mount_backed, linked_app_dir_for, workspace_dir
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

        # Inside a registered linked app: ask git itself. A linked folder is
        # often a SUBFOLDER of the user's repository (`.git` lives at an
        # ancestor). Linked dirs are a small registered set, so the fork happens
        # rarely, never on ordinary stats.
        linked = linked_app_dir_for(path)
        if linked:
            return _in_work_tree(linked)

        # A fused app, or anything inside one: one relpath + one `.git` stat.
        root = workspace_dir()
        rel = os.path.relpath(os.path.abspath(path), root)
        if rel != os.curdir and rel.split(os.sep, 1)[0] != os.pardir:
            parts = rel.split(os.sep)
            if (len(parts) >= 2
                    and not parts[0].startswith(".")
                    and not parts[1].startswith(".")):
                app_dir = os.path.join(root, parts[0], parts[1])
                if os.path.isdir(os.path.join(app_dir, ".git")):
                    return True

        # Not an app target (D235). A FILE still earns its own timeline from
        # whichever repository it happens to live in — that is the file-side
        # history view `git` no longer provides — and the view is read-only
        # there, enforced by versions.py rather than by hiding the mode. A
        # plain DIRECTORY does not: folder-wide history outside an app is the
        # `git` mode's story, and two modes for one story is what the peer
        # exclusion in git/condition.py exists to prevent.
        #
        # `isfile`, deliberately not `not isdir`: the loose form is also true for
        # every path that does NOT EXIST, so a missing name inside any repository
        # would gate true. Same one-word trap the peer gate documents
        # (claude/condition.py), and "cannot tell" must read as "refuse"
        # (CT-12).
        if not os.path.isfile(path):
            return False
        parent = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(parent):
            return False
        return _in_work_tree(parent)
    except Exception:  # noqa: BLE001 — any probe error: fail closed, quietly
        return False
