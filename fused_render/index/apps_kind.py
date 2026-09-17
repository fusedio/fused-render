"""The built-in "apps" index kind: an app-name index over the workspace,
and the reference proof of the plugin contract in `kinds.py`.

Whether a folder IS an app cannot be answered from anything the plain
`files`/`dirs` schema already carries (path, name, ext, size, mtime): it
depends on reading the CONTENT of a direct-child `.html` page for a
`<meta name="fused-app">` marker (`app_listing.app_entry`, the same rule
`GET /api/apps` uses). That is exactly the gap an `IndexKind.extract` is
for — one file, one stat, a row or nothing — and exactly why this can't be
answered by existing file metadata alone.

Mirrors `git_repos.py`'s "index a FACT about the filesystem, not the
filesystem itself" posture: a row here says "this folder is an app,
diagnosed at scan time", not "let me go check right now". And it mirrors
`exported_apps.py`'s degrade-safe posture: `extract` never raises — an
unreadable folder, a race with a delete, a malformed page all answer None,
the same as "not an app", never an exception that would take the whole
scan down (see `IndexKind.extract`'s own contract in kinds.py).

`app_listing.app_dict` already builds the exact row shape `GET /api/apps`
serves; this module reuses it verbatim rather than re-deriving a second
shape, and adds the one field that shape lacks — `app_id.app_id`, the
stable identity a search result can carry across a rename. The one field
`app_dict` produces that THIS kind does not carry is `tag` (the Apps hub's
"Folders" facet, first path segment under its OWN workspace-scoped walk,
D-whatever) — a global name index has no single workspace root to derive
it against, and nothing here needs it: `Apps.tsx`'s client-side walk is the
one place `tag` is read, and it stays untouched (SPEC-index-plugins.md
decision: the hub's duplication with this index is accepted, not fixed).
"""
from __future__ import annotations

import os

from fused_render import app_id as app_id_mod
from fused_render import app_listing
from fused_render.index.kinds import Column, IndexKind, register

NAME = "apps"

# Mirrors app_listing.app_dict's shape minus "tag" (see module docstring),
# plus "id" — the stable identity `app_dict` itself does not carry.
COLUMNS = (
    Column("name", "string"),
    Column("path", "string"),
    Column("entry", "string"),
    Column("entry_html", "string"),
    Column("id", "string"),
    Column("preview_image", "string"),
    Column("category", "string"),
    Column("icon", "string"),
    Column("icon_mtime", "float64"),
    Column("title", "string"),
    Column("updated_at", "float64"),
)


def extract(path: str, st) -> dict | None:
    """One row when `path` is the canonical app entry of its folder, else
    None. Never raises.

    `st` (the host walker's `os.stat_result`) is accepted per the plugin
    contract but unused here: app-ness is a CONTENT question, not a
    metadata one, so nothing in `st` could shortcut the marker read. A
    future kind that used `st.st_size`/`st.st_mtime` to skip work would be
    the more typical shape; this one simply has no such shortcut.
    """
    if not path.lower().endswith(".html"):
        return None
    folder = os.path.dirname(path)
    try:
        entry = app_listing.app_entry(folder)
    except OSError:
        # Unreadable/racing folder: `app_entry` documents this as the
        # caller's to decide, and here "skip it" is the only sane answer —
        # an index kind has no UI to distinguish "unreadable" from "not an
        # app" for, unlike `GET /api/apps`'s folder-vs-card distinction.
        return None
    if entry is None or os.path.abspath(entry) != os.path.abspath(path):
        # Either the folder has no declared app at all, or `path` is some
        # OTHER file in an app's folder (a multi-page app's non-entry
        # page). Only the entry itself gets a row — one row per app, not
        # one per html file inside it.
        return None
    row = app_listing.app_dict(
        folder, os.path.basename(folder), "", entry, include_updated_at=True
    )
    row.pop("tag", None)
    row["id"] = app_id_mod.app_id(entry)
    return row


KIND = IndexKind(name=NAME, columns=COLUMNS, extract=extract, text_column="name")


def register_builtin(*, replace: bool = False) -> None:
    """Register the built-in "apps" kind. Called once at server startup
    (alongside wherever "files" is wired up); `replace=True` is only for
    tests re-registering across runs — see `kinds.register`."""
    register(KIND, replace=replace)
