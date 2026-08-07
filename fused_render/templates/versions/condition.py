"""Gate for the `versions` template (app git history).

`main(path)` says whether the target — a file OR a directory — lives inside a
*fused app*: a folder exactly two levels under the workspace
(`<workspace>/<tag>/<name>`, `shell/seed.fused_dir()`) that is itself a git
repository. Only such folders get the auto-commit treatment
(`fused_render/app_git.py`), so only they have a history worth showing; the
template registry offers `versions` on many file types and on every directory,
and this gate is what narrows that to apps.

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
folders out of app_git.app_dir_for and the claude agents' _commit_turn sweep
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

        # Inside a registered linked app: ask git itself, the way
        # git/condition.py does. A linked folder is often a SUBFOLDER of the
        # user's repository (`.git` lives at an ancestor), and a hand-rolled
        # ascent gets the two `.git` shapes wrong (dir in a clone, file in a
        # worktree/submodule) — `rev-parse --is-inside-work-tree` answers all
        # of them from any depth in one bounded fork. Linked dirs are a small
        # registered set, so the fork happens rarely, never on ordinary stats.
        linked = linked_app_dir_for(path)
        if linked:
            import subprocess

            proc = subprocess.run(
                ["git", "--no-pager", "-C", linked, "rev-parse",
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

        root = workspace_dir()
        rel = os.path.relpath(os.path.abspath(path), root)
        if rel == os.curdir or rel.split(os.sep, 1)[0] == os.pardir:
            return False
        parts = rel.split(os.sep)
        if len(parts) < 2 or parts[0].startswith(".") or parts[1].startswith("."):
            return False
        app_dir = os.path.join(root, parts[0], parts[1])
        return os.path.isdir(os.path.join(app_dir, ".git"))
    except Exception:  # noqa: BLE001 — any probe error: fail closed, quietly
        return False
