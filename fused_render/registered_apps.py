"""Registered apps: app folders living OUTSIDE the workspace, listed on /apps.

The workspace walk (`app_listing.workspace_apps`) only sees folders inside the
workspace, at most three levels down. A *registered* app is any folder
elsewhere on disk the user opened through the explorer's "Open app" button: the
click records `{"path": <abs folder>, "openedAt": <iso>}` in
``~/.fused-render/registered_apps.json``, and the /apps hub merges those
folders in under the reserved virtual tag ``linked``.

This is a deliberate PARTIAL revival of the linked-apps registry D264 removed
(and #525 deleted outright), on the owner's ask. What comes back is the
registry and its /apps listing; what stays dead is everything D264 killed the
registry FOR — the `app` template mode, `_app` sentinel, link/unlink/status
routes, and the `FUSED_RENDER_LINKED_APPS` env export the template gates read.
Nothing outside the /apps listing consumes this store. The old inert
``linked_apps.json`` is left untouched — no migration, same as #525's posture.

Registration is PASSIVE — opening IS registering — so the store doubles as the
external apps' recents: `openedAt` here is what `opened_at` reports for these
entries, where a workspace app's comes from ``app_recents.json``. One folder,
one entry: a re-open updates `openedAt` in place.

A registry over a symlink into the workspace, for the same reasons as before:
`app_git.app_dir_for` scopes auto-commits by path prefix and a symlink would
let fused-render commit inside the user's own repository; symlink creation is
privileged on Windows; and stale entries just drop out on read instead of
dangling.

Reads degrade like every other listing: a missing or unreadable target folder
drops out of the app list (read-only — the folder may come back), and a corrupt
registry reads as empty. Entries inside the workspace are filtered on read —
the walk already lists those, and a workspace path here would double-list.
"""
import os
from datetime import datetime, timezone

from fused_render import app_listing
from fused_render.index.ignore import MountGuard
from fused_render.shell import storage

# The reserved virtual tag external apps file under on the /apps hub — the
# "Repo" facet chip they share. Reused from the old registry deliberately;
# DECISIONS already calls it the reserved tag. A workspace folder literally
# named `linked/` merges chips with it, and that is accepted.
REGISTERED_TAG = "linked"

# The store is user-writable and otherwise unbounded — same cap posture as
# apps.py's APP_RECENTS_CAP, and far above any real number of external apps.
REGISTERED_APPS_CAP = 200


def _registry_path() -> str:
    return os.path.join(storage.home_dir(), "registered_apps.json")


def _in_or_over_workspace(folder: str) -> bool:
    """Whether `folder` is the workspace, an ancestor of it, or inside it.
    Inside-workspace folders are the walk's to list (an entry here would
    double-list them); an ancestor entry would be a card for the user's whole
    disk. Filtered on read AND refused on write, so a hand-edited registry
    can't poison the listing either way.

    PURE STRING WORK on abspaths, deliberately — `read_entries` runs this per
    entry with no MountGuard in front, and a resolve is a syscall a wedged
    mount can block on. The symlink-alias hole that leaves (a link whose
    TARGET is the workspace) is closed by `_resolves_into_workspace`, which
    the two callers that have already passed the guard run as well."""
    from fused_render.shell.seed import fused_dir

    root = os.path.abspath(fused_dir())
    folder = os.path.abspath(folder)
    return (
        root == folder
        or root.startswith(folder + os.sep)
        or folder.startswith(root + os.sep)
    )


def _resolves_into_workspace(folder: str) -> bool:
    """The realpath half of the workspace refusal: an external SYMLINK whose
    target is the workspace (or sits over/under it) passes the string check
    above, and would double-list the walk's own apps under `linked`. Costs a
    resolve — a syscall — so callers run it only AFTER MountGuard has passed
    on the path.

    A resolve that RAISES (realpath can, e.g. a symlink loop) answers True:
    both callers treat True as refuse/skip, and "cannot tell" must keep
    reading as "refuse" — a path we can't resolve is not one to register or
    list, and one bad registry entry must never 500 the endpoint or fail the
    listing."""
    from fused_render.shell.seed import fused_dir

    try:
        root = os.path.realpath(fused_dir())
        folder = os.path.realpath(folder)
    except OSError:
        return True
    return (
        root == folder
        or root.startswith(folder + os.sep)
        or folder.startswith(root + os.sep)
    )


def read_entries() -> list[dict]:
    """The registry's valid entries, in stored order (newest-open first).
    Corrupt/missing file or malformed entries read as absent — a registry
    degrades, never raises."""
    data = storage.read_json(_registry_path())
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    return [
        e
        for e in (entries if isinstance(entries, list) else [])
        if isinstance(e, dict)
        and isinstance(e.get("path"), str)
        and os.path.isabs(e["path"])
        and not _in_or_over_workspace(e["path"])
    ]


def write_entries(entries: list[dict]) -> None:
    storage.write_json(_registry_path(), {"entries": entries})


def record_open(path: str) -> bool:
    """Register `path` as an external app (or refresh its `openedAt` if it
    already is one). False when the path isn't a registrable app folder —
    relative, inside the workspace, behind a wedged mount, unreadable, gone, or
    page-less — the same benign no-op posture as the recents endpoint.

    Server-authoritative on purpose: the button's client-side gate ("this
    folder resolved an entry") is not enough, because everything stored here
    feeds syscalls in GET /api/apps."""
    if not isinstance(path, str) or not os.path.isabs(path):
        return False
    path = os.path.abspath(path)
    if _in_or_over_workspace(path):
        return False
    # BEFORE any syscall on the candidate, same ordering as the walk: a stat
    # under a wedged rclone mount blocks the serving thread, and the guard
    # answers from mount records with pure string work.
    if MountGuard().blocks(path):
        return False
    if _resolves_into_workspace(path):
        return False
    try:
        if not os.path.isdir(path) or app_listing.app_entry(path) is None:
            return False
    except OSError:
        return False
    kept = [e for e in read_entries() if os.path.abspath(e["path"]) != path]
    entry = {"path": path, "openedAt": datetime.now(timezone.utc).isoformat()}
    write_entries([entry, *kept][:REGISTERED_APPS_CAP])
    return True


def registered_apps() -> list[dict]:
    """Registry entries as app listing dicts (tag = ``linked``), shaped by the
    same `app_listing.app_dict` contract as workspace apps, each carrying its
    own `opened_at` (epoch seconds, from the entry's `openedAt`). An entry
    whose folder is missing, unreadable, page-less, or behind a wedged mount is
    skipped, not deleted — read-only, the folder may come back."""
    apps: list[dict] = []
    guard = MountGuard()
    for e in read_entries():
        path = e["path"]
        if guard.blocks(path):
            continue
        if _resolves_into_workspace(path):
            continue  # a symlink alias of the workspace: the walk's to list
        try:
            if not os.path.isdir(path):
                continue
            entry_html = app_listing.app_entry(path)
        except OSError:
            continue  # unreadable: skip, never fail the listing
        if entry_html is None:
            continue  # the page went away: a card must open a page
        app = app_listing.app_dict(path, os.path.basename(path),
                                   REGISTERED_TAG, entry_html)
        app["opened_at"] = _opened_epoch(e.get("openedAt"))
        apps.append(app)
    return apps


def is_registered_app_entry(fs_path: str) -> bool:
    """Whether `fs_path` is the ENTRY page of a registered app — i.e. whether
    `registered_apps()` would report it as some entry's `entry`. The file
    recents' question (shell/recents.py), asked beside its workspace twin
    `app_listing.is_workspace_app_entry`: opening a registered app is already
    the registry's record (`record_open`), so the same open must not land in
    the file recents too.

    Targeted like the workspace check rather than a full `registered_apps()`
    membership test — that pays `entry_title`/`preview_image`/`metadata.json`
    reads per entry, and this runs per recents record and per GET row. Cost
    here: one registry read, and at most one guarded listdir when the file's
    parent IS a registered folder.

    Same error asymmetry as the workspace check: True hides the file from the
    file recents, so anything indeterminate (blocked mount, OSError, page
    gone) answers False — the row records/stays."""
    path = os.path.abspath(fs_path)
    parent = os.path.dirname(path)
    if not any(os.path.normcase(os.path.abspath(e["path"])) == os.path.normcase(parent)
               for e in read_entries()):
        return False
    # Guard BEFORE the listdir, same ordering as registered_apps(): the
    # registry is user-writable and everything in it feeds syscalls.
    if MountGuard().blocks(parent):
        return False
    # Same post-guard resolve as registered_apps(): a registry folder that is
    # a symlink alias of the workspace is skipped by the listing, so its page
    # is not a registered entry either — treating it as one would hide the
    # open from the file recents with nothing recording it.
    if _resolves_into_workspace(parent):
        return False
    try:
        entry = app_listing.app_entry(parent)
    except OSError:
        return False
    return entry is not None and os.path.normcase(entry) == os.path.normcase(path)


def _opened_epoch(ts) -> float | None:
    """`openedAt` as epoch seconds (opened_at's unit), or None — the file is
    user-writable, so a malformed timestamp drops to "never opened" rather
    than failing the listing."""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None
