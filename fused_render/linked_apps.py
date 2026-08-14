"""Linked apps: a READ-ONLY registry of app folders living anywhere on disk.

The workspace walk (`app_listing.two_level_apps`) only sees folders under
``<workspace>/<tag>/<name>``. A *linked* app is any folder elsewhere on disk
that the user once marked as an app: an entry in
``~/.fused-render/linked_apps.json`` mapping a name to an absolute folder path.

**Nothing writes this registry any more.** "Add as app" was its only writer and
went with the app concept (D264), taking `link_app`/`unlink_app` and their
routes with it. The module survives on two facts about installs that already
have a registry file, neither of which is served by deleting it:

- those folders still list on the /apps hub under the ``linked`` tag, and
  dropping them would be silent data loss from a feature removal that was about
  BUTTONS, not about the user's folders;
- `export_linked_apps_env` still publishes them, and `templates/history` still
  asks `is_linked_app_dir` before it will `revert` — a linked folder is the
  USER's own repository, so that refusal must keep firing for exactly the
  folders it was written for. An unexported env var would turn a safety rule
  into a no-op quietly.

`write_entries` is kept as the one remaining way in, for tests and for a user
editing the file by hand. The listing merges them in under
the reserved virtual tag ``linked`` — no symlink is created, so nothing else
in the system (git auto-commit, export, delete) can be tricked into treating
the user's real folder as workspace content by a filesystem alias.

A registry was chosen over a ``linked/`` symlink dir deliberately:

- ``app_git.app_dir_for`` scopes auto-commits by path prefix (`abspath`, not
  `realpath`); a symlink under the workspace would pass that check and let
  fused-render ``git add -A`` inside the user's own repository. A registry
  entry never enters the workspace, so the guard holds with no change.
- Symlink creation is privileged on Windows; JSON works everywhere.
- Stale entries are filtered on read (same posture as app_recents) instead of
  leaving dangling links to prune.

Registry shape: ``{"entries": [{"name": str, "path": str}, ...]}`` — names
unique, paths absolute. Reads degrade like every other listing: a missing or
unreadable target folder just drops out of the app list (read-only — the
folder may come back), and a corrupt registry reads as empty.
"""
import os

from fused_render import app_listing
from fused_render.shell import storage

LINKED_TAG = "linked"


def _registry_path() -> str:
    return os.path.join(storage.home_dir(), "linked_apps.json")


def _contains_workspace(folder: str) -> bool:
    """Whether `folder` is the Fused workspace or an ancestor of it. Such an
    entry would make `linked_app_dir_for` claim every workspace path, shadowing
    real apps in the template gates — filtered on read so even a hand-edited or
    pre-fix registry can't poison path resolution."""
    from fused_render.shell.seed import fused_dir

    root = os.path.abspath(fused_dir())
    folder = os.path.abspath(folder)
    return root == folder or root.startswith(folder + os.sep)


def read_entries() -> list[dict]:
    """The registry's valid entries, in stored order. Corrupt/missing file or
    malformed entries read as absent — a registry degrades, never raises."""
    data = storage.read_json(_registry_path())
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    return [
        e
        for e in (entries if isinstance(entries, list) else [])
        if isinstance(e, dict)
        and isinstance(e.get("name"), str)
        and e["name"]
        and isinstance(e.get("path"), str)
        and os.path.isabs(e["path"])
        and not _contains_workspace(e["path"])
    ]


def write_entries(entries: list[dict]) -> None:
    storage.write_json(_registry_path(), {"entries": entries})
    export_linked_apps_env()


def export_linked_apps_env() -> None:
    """Publish the registered folders to the environment for template children
    (SPEC PY-15 / D166) — same contract as `export_ro_mounts_env`.

    Template gates (app/condition.py, claude/condition.py) must decide
    "is this folder an app?" with pure path arithmetic — no file reads — and
    they can't import fused_render. So the DERIVED PATH LIST travels as
    `FUSED_RENDER_LINKED_APPS` (os.pathsep-joined, the platform's own
    list-in-one-var convention, matching FUSED_RENDER_RO_MOUNTS), read back by
    `templates/shared/appenv.py`. Exported on every registry write and once at
    server startup, so a value is always present — possibly empty — rather
    than an unset var a child couldn't tell from "no linked apps".
    """
    os.environ["FUSED_RENDER_LINKED_APPS"] = os.pathsep.join(
        e["path"] for e in read_entries()
    )


def linked_path(name: str) -> str | None:
    """The registered folder for `name`, or None when not registered."""
    for e in read_entries():
        if e["name"] == name:
            return e["path"]
    return None


def linked_apps() -> list[dict]:
    """Registry entries as app listing dicts (tag = ``linked``), shaped by the
    same `app_listing.app_dict` contract as workspace apps. An entry whose
    folder is missing or unreadable is skipped, not deleted — read-only, the
    folder may come back (same posture as app_recents)."""
    apps: list[dict] = []
    for e in read_entries():
        path = e["path"]
        try:
            if not os.path.isdir(path):
                continue
            entry_html = app_listing.app_entry(path)
        except OSError:
            continue  # unreadable: skip, never fail the listing
        apps.append(app_listing.app_dict(path, e["name"], LINKED_TAG, entry_html))
    return apps
