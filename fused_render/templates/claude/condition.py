"""Gate for the `claude_split` template — the split view: the target's own
preview beside a Claude chat, with the annotation / app_state machinery
(D230).

`main(path)` answers for the two kinds of target the mode is bound to:

* **A FILE** (every key in the registry's authored-file set — source, config,
  prose, data, image assets) → allowed. This is the file-scoped chat: the left
  pane renders the file in its OWN default template and the annotation tools
  work over that, which is the whole reason `claude_split` replaced the plain
  `claude` mode on file keys (D230). Nothing more is asked of a file: the
  registry already decided which extensions offer the mode, and a file needs
  neither a workspace nor a repository to be worth talking about.
* **A DIRECTORY** (the universal "/" key) → allowed ONLY for a project folder,
  i.e. a directory exactly two levels below the workspace root
  (<workspace>/<tag>/<project>), or a registered *linked app* folder
  (`FUSED_RENDER_LINKED_APPS`), which may live anywhere on disk. Anywhere else
  (the root itself, a tag folder, a nested subfolder, an unrelated directory)
  the mode stays hidden — an ordinary folder's chat is the `claude` mode, whose
  left pane would have no app entry to render. This is what keeps the mode in
  the app-builder view (App.tsx APP_MODES) without leaking it onto every
  directory in the explorer.

The file/directory split is `os.path.isdir`, ONE stat — deliberately the same
question `app/condition.py` never has to ask, because that gate is bound to "/"
alone and this one is not.

CRITICAL: this never lists or walks the directory (`os.listdir`,
`os.scandir`, `glob`, recursion) and never resolves symlinks — the gate runs
for every directory the explorer stats, some on remote mounts, and pure path
arithmetic on the already-known path is the only I/O-free answer. Mount-backed
paths are refused outright, same as the peer gates.
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
            from appenv import is_linked_app_dir, is_mount_backed, workspace_dir
        except Exception:  # noqa: BLE001 — cannot tell -> refuse (CT-12)
            return False
        if is_mount_backed(path):
            return False

        # A file target is the file-scoped chat: allowed anywhere on disk. The
        # test is `isfile`, an EXISTING regular file — deliberately not
        # `not isdir`, which would also swallow every path that does not exist
        # and hand a `True` to any nonexistent child of a linked app folder. One
        # stat, and "cannot tell" keeps reading as "refuse" (CT-12).
        if os.path.isfile(path):
            return True

        # A registered linked app (FUSED_RENDER_LINKED_APPS, the registry at
        # ~/.fused-render/linked_apps.json) is an app wherever it lives — same
        # rule as app/condition.py, so a folder never offers one of the two
        # app modes without the other. Env-membership check only (no I/O).
        if is_linked_app_dir(path):
            return True

        root = workspace_dir()
        try:
            rel = os.path.relpath(os.path.abspath(path), root)
        except ValueError:
            # Windows: different drive letters -> not under the root.
            return False
        if rel == os.curdir or rel.split(os.sep, 1)[0] == os.pardir:
            return False
        # Exactly <tag>/<project>: two segments, no more, no fewer — and
        # neither hidden. The apps API skips dot-prefixed tags and projects
        # when listing Home cards; `tag/.venv` or `.hidden/project` must not
        # sneak the mode in through the gate.
        parts = [p for p in rel.split(os.sep) if p not in ("", ".")]
        return len(parts) == 2 and not any(p.startswith(".") for p in parts)
    except Exception:  # noqa: BLE001 — a broken gate must hide, never raise
        return False
