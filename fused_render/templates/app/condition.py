"""Gate for the `app` template (bound on the universal "/" directory key).

`main(path)` offers the plain app view — the app itself, full-bleed, for USING
it — ONLY for a project folder, i.e. a directory exactly two levels below the
workspace root: <workspace>/<tag>/<project>. Anywhere else (the root itself, a
tag folder, a nested subfolder, an unrelated directory) the mode stays hidden,
and `_listing` remains what an ordinary directory opens on.

Same rule, deliberately identical to `claude_split/condition.py`: the two modes
are two ways of looking at the same thing, so a folder must never offer one
without the other. Unlike `versions` there is no `.git` requirement — using an
app has nothing to do with whether its history is tracked.

CRITICAL: this never lists or walks the directory (`os.listdir`, `os.scandir`,
`glob`, recursion) and never resolves symlinks — the gate runs for every
directory the explorer stats, some on remote mounts, and pure path arithmetic on
the already-known path is the only I/O-free answer. Mount-backed paths are
refused outright: an app is by definition a local folder, and a mount path is
itself shaped <mounts>/<remote>/<dir>, which would otherwise pass the rule.
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
        try:
            rel = os.path.relpath(os.path.abspath(path), root)
        except ValueError:
            # Windows: different drive letters -> not under the root.
            return False
        if rel == os.curdir or rel.split(os.sep, 1)[0] == os.pardir:
            return False
        # Exactly <tag>/<project>: two segments, no more, no fewer — and neither
        # hidden. The apps API skips dot-prefixed tags and projects when listing
        # Home cards; `tag/.venv` or `.hidden/project` must not sneak the mode in
        # through the gate.
        parts = [p for p in rel.split(os.sep) if p not in ("", ".")]
        return len(parts) == 2 and not any(p.startswith(".") for p in parts)
    except Exception:  # noqa: BLE001 — a broken gate must hide, never raise
        return False
