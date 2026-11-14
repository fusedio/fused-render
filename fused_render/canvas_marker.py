"""The per-folder canvas marker, `.fused-canvas.json` (D1476).

Every environment's canvas root (`~/Fused/canvas/<env>/`, `canvases.
canvases_root`) holds three scope folders — `user/`, `team/`, `public/` — and
each canvas folder in them may carry this one small JSON file. It is the ONLY
state the `fused canvas mirror` command and fused-render share, and the format
is that command's contract (v1), not ours to extend:

    {"version": 1, "sync": "mirror" | "live", "scope": "user" | "team" | "public",
     "id": "<collection id>", "name": "<canvas name>", "owner": "<username or null>",
     "revision": 12, "last_updated": "<ISO 8601>", "pulled_at": "<ISO 8601 UTC>"}

Who owns a folder is the `sync` field:

* `"mirror"` — a read-only reference copy the CLI wrote. The CLI may rewrite
  or delete it. fused-render never syncs, watches or pushes it.
* `"live"` — a clone fused-render two-way syncs. The CLI never writes into,
  moves or deletes it. fused-render writes ONLY this value, and only under
  `user/`; the CLI never writes it.
* no marker, or one that cannot be read — "unmanaged": the CLI leaves the
  folder alone. For fused-render a `user/<name>` folder with a `canvas.toml`
  and no readable marker is still a live clone (a clone from before the
  marker existed, the external-CLI path, the instant between a pull that
  removed the marker and the rewrite).

The two readers are deliberately not equally strict. `read` is the strict
one: anything that is not a well-formed marker is None, and every destructive
decision (`removable_mirror`) is made on it. `sync_of` leans the other way for
one answer only: a file that parses as a JSON object whose `sync` says
"mirror" reads as "mirror" even when another field is off, because the one
mistake that costs data is treating a reference copy as a clone and pushing
it over the user's canvas.

The marker is local state and is never uploaded, never part of a sync point:
a dot-file, so the watcher's fingerprint, hash, trash and watch walks never
see it; in `canvas_snapshot.IGNORED_BASENAMES`, so a merge never takes one
from a bundle; in the clone's seeded `.fusedignore`, so no push ships it.

Standard library only, and no import of anything else in `fused_render`: the
server imports it, and so does `_canvas_push.py`, which sits on the startup
path of every `fused` command.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
import threading

MARKER_NAME = ".fused-canvas.json"
VERSION = 1
#: The scope folders under an environment's canvas root, most specific first.
SCOPES = ("user", "team", "public")
SYNC_LIVE = "live"
SYNC_MIRROR = "mirror"
#: `sync_of`'s answer for a folder with no readable marker.
SYNC_NONE = "none"

# A marker is a few hundred bytes; anything far past that is not one.
_MAX_BYTES = 64 * 1024


@dataclasses.dataclass(frozen=True)
class Marker:
    """One well-formed `.fused-canvas.json`."""

    version: int
    sync: str
    scope: str
    id: str | None
    name: str | None
    owner: str | None
    revision: int | None
    last_updated: str | None
    pulled_at: str | None


def marker_path(folder: str) -> str:
    return os.path.join(folder, MARKER_NAME)


def _load(folder: str) -> dict | None:
    """The marker file's JSON object, or None (missing, unreadable, too
    large, not JSON, not an object). Never raises."""
    try:
        with open(marker_path(folder), "rb") as f:
            raw = f.read(_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _opt_str(value) -> tuple[bool, str | None]:
    """`(ok, value)` for a field that is a string or null."""
    if value is None:
        return True, None
    return (True, value) if isinstance(value, str) else (False, None)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def read(folder: str) -> Marker | None:
    """The folder's marker, or None when it is missing or malformed — the
    contract's "unmanaged". Strict: a known `sync` and `scope`, an integer
    `version` of at least 1 (a later version that still carries the fields
    below reads as what it says), and every other field of its own type."""
    data = _load(folder)
    if data is None:
        return None
    version, sync, scope = data.get("version"), data.get("sync"), data.get("scope")
    if not _is_int(version) or version < 1:
        return None
    if sync not in (SYNC_LIVE, SYNC_MIRROR) or scope not in SCOPES:
        return None
    revision = data.get("revision")
    if revision is not None and not _is_int(revision):
        return None
    fields = {}
    for key in ("id", "name", "owner", "last_updated", "pulled_at"):
        ok, fields[key] = _opt_str(data.get(key))
        if not ok:
            return None
    return Marker(version=version, sync=sync, scope=scope, revision=revision, **fields)


def sync_of(folder: str) -> str:
    """Who owns `folder`: "live", "mirror" or "none".

    "mirror" is answered for ANY marker object whose `sync` says so, valid in
    its other fields or not (module docstring): a reference copy must never
    read as a clone. "live" needs a well-formed marker. Everything else —
    no file, not JSON, an unknown `sync` — is "none"."""
    data = _load(folder)
    if data is None:
        return SYNC_NONE
    if data.get("sync") == SYNC_MIRROR:
        return SYNC_MIRROR
    marker = read(folder)
    return marker.sync if marker is not None else SYNC_NONE


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_live(folder: str, *, name: str, collection_id: str, scope: str = "user",
               owner: str | None = None, revision: int | None = None,
               last_updated: str | None = None) -> bool:
    """Mark `folder` as fused-render's live clone of collection
    `collection_id`. True when the marker on disk says so afterwards.

    The caller decides WHETHER a folder may become live (only `user/<name>`,
    only fused-render's own clone); this only writes. A marker that already
    says the same thing is left untouched (its `pulled_at` stands), so the
    callers that run after every pull and every sync start cost a read.
    Atomic — a dot-prefixed temp file beside it, then `os.replace` — so a
    reader sees the old marker or the new one, and the temp never shows up in
    a clone walk. Never raises: a marker that could not be written leaves the
    folder unmanaged, which the next call heals."""
    if (not isinstance(collection_id, str) or not collection_id
            or not isinstance(name, str) or not name or scope not in SCOPES):
        return False
    if revision is not None and not _is_int(revision):
        revision = None
    current = read(folder)
    wanted = Marker(version=VERSION, sync=SYNC_LIVE, scope=scope, id=collection_id,
                    name=name, owner=owner, revision=revision,
                    last_updated=last_updated,
                    pulled_at=current.pulled_at if current is not None else None)
    if current is not None and current == wanted and current.pulled_at:
        return True
    record = dataclasses.asdict(dataclasses.replace(wanted, pulled_at=_now_iso()))
    path = marker_path(folder)
    # Unique per writer: a watcher thread and a route can mark one clone at
    # the same moment, and must not share a staging file.
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(record, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    return True


def find_by_id(scope_dir: str, collection_id: str) -> str | None:
    """The canvas folder directly under `scope_dir` whose marker holds
    `collection_id`, or None. A live folder wins over a mirror of the same
    id; within one kind the first by name. Dot-folders (the CLI's half-written
    temp siblings) are never candidates."""
    if not isinstance(collection_id, str) or not collection_id:
        return None
    try:
        names = sorted(os.listdir(scope_dir))
    except OSError:
        return None
    mirror = None
    for name in names:
        if name.startswith("."):
            continue
        folder = os.path.join(scope_dir, name)
        marker = read(folder)
        if marker is None or marker.id != collection_id:
            continue
        if marker.sync == SYNC_LIVE:
            return folder
        if mirror is None:
            mirror = folder
    return mirror


# -- what a folder under `user/` is ----------------------------------------------

#: `folder_state`'s answers.
STATE_MISSING = "missing"      # no such directory
STATE_MIRROR = "mirror"        # the mirror command's reference copy
STATE_ADOPTING = "adopting"    # being turned into a clone, not pulled yet
STATE_LIVE = "live"            # marked live
STATE_UNMARKED = "unmarked"    # no readable marker


def adopting_path(folder: str) -> str:
    """The adoption flag of canvas folder `<root>/<scope>/<name>`:
    `<root>/.sync/<name>.adopting`. It exists from the moment a reference
    copy is taken over until a pull has landed in it (canvases.py
    `_adopt_reference_copy`); it sits outside the folder so that no pull can
    delete it."""
    folder = os.path.abspath(folder)
    root = os.path.dirname(os.path.dirname(folder))
    return os.path.join(root, ".sync", os.path.basename(folder) + ".adopting")


def folder_state(folder: str) -> str:
    """What the canvas folder `folder` is, the ONE reading every caller that
    asks "is this a clone?" goes through.

    A marker that says `mirror` wins (`sync_of`'s lenient reading): the
    folder is the mirror command's whatever else is true. Otherwise a folder
    with an adoption flag is "adopting" — out of the command's hands, but
    still holding a reference copy's possibly stale files, so not a clone
    yet. Otherwise "live" or "unmarked" by its marker."""
    if not os.path.isdir(folder):
        return STATE_MISSING
    sync = sync_of(folder)
    if sync == SYNC_MIRROR:
        return STATE_MIRROR
    if os.path.lexists(adopting_path(folder)):
        return STATE_ADOPTING
    return STATE_LIVE if sync == SYNC_LIVE else STATE_UNMARKED


def is_clone(folder: str) -> bool:
    """Whether `folder` is a clone fused-render may sync: it holds a
    `canvas.toml`, it is not a reference copy, and no adoption of it is
    pending. A missing or unreadable marker counts as a clone, deliberately
    (module docstring)."""
    return (folder_state(folder) in (STATE_LIVE, STATE_UNMARKED)
            and os.path.isfile(os.path.join(folder, "canvas.toml")))


def is_reference(folder: str) -> bool:
    """Whether `folder` holds a reference copy's files and must be treated as
    read-only: the mirror command's own, or one whose adoption has not been
    pulled yet."""
    return folder_state(folder) in (STATE_MIRROR, STATE_ADOPTING)


def remove_mirror(folder: str, scope_dir: str) -> bool:
    """Delete `folder` if it is a `removable_mirror`, the way the mirror
    command does: renamed to a dot-name beside it first, then deleted. True
    when the path is free afterwards.

    A plain `rmtree` that fails halfway can remove the marker and leave
    `canvas.toml` — a folder that then reads as a live clone holding half a
    reference copy. Renamed away first, the path is free at once and what a
    failed delete leaves is a dot-folder no scan looks at."""
    import shutil
    import uuid

    if not removable_mirror(folder, scope_dir):
        return False
    aside = os.path.join(os.path.dirname(os.path.abspath(folder)),
                         f".fused-removing-{uuid.uuid4().hex}")
    try:
        os.rename(folder, aside)
    except OSError:
        return False
    shutil.rmtree(aside, ignore_errors=True)
    return True


def removable_mirror(folder: str, scope_dir: str) -> bool:
    """Whether fused-render may delete or replace `folder`: the contract's
    three conditions, all of them — it resolves to a direct child of
    `scope_dir`, it is a real directory and not a symlink, and it holds a
    well-formed marker saying `sync: "mirror"`. Anything else is someone's
    folder and is left alone."""
    try:
        if os.path.islink(folder) or not os.path.isdir(folder):
            return False
        real = os.path.realpath(folder)
        if os.path.dirname(real) != os.path.realpath(scope_dir):
            return False
    except OSError:
        return False
    marker = read(folder)
    return marker is not None and marker.sync == SYNC_MIRROR
