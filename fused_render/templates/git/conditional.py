"""Inverse gate for the `git` template — the exact opposite of
`templates/versions/condition.py`.

`main(path)` is True precisely where the `versions` gate is False: the target
does NOT live inside a *fused app* (a git-initialized folder exactly two levels
under the workspace, `<workspace>/<tag>/<name>`). App folders get the
`versions` history view; everything else falls through to this gate, so the two
modes never overlap.

Two refusals are NOT inverted, deliberately:

- **Mount-backed paths** still return False. The refusal in the `versions` gate
  exists to avoid stats over a kernel NFS mount, and inverting it would create
  exactly that I/O here instead. A refusal-for-I/O-shape is a property of the
  probe, not of the answer.
- **Errors** still return False (fail closed, CT-12). "Cannot tell" must read
  as "refuse" in every gate, whichever way its positive answer points.

Same duplication rule as the original: the app-dir rule mirrors
`app_git.app_dir_for`; a template must not import `fused_render`
(SPEC PY-15 / D166), so the workspace root travels via `../shared/appenv.py`.
Constant-time: one relpath and one `.git` isdir stat, never a listing.
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
            from appenv import is_mount_backed, workspace_dir
        except Exception:  # noqa: BLE001 — cannot tell -> refuse (CT-12)
            return False
        if is_mount_backed(path):
            return False

        root = workspace_dir()
        rel = os.path.relpath(os.path.abspath(path), root)
        if rel == os.curdir or rel.split(os.sep, 1)[0] == os.pardir:
            return True
        parts = rel.split(os.sep)
        if len(parts) < 2 or parts[0].startswith(".") or parts[1].startswith("."):
            return True
        app_dir = os.path.join(root, parts[0], parts[1])
        return not os.path.isdir(os.path.join(app_dir, ".git"))
    except Exception:  # noqa: BLE001 — any probe error: fail closed, quietly
        return False
