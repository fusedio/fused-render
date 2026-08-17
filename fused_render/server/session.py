import os
import time
from urllib.parse import parse_qsl, urlsplit
from fastapi import APIRouter, Body, Header
from fused_render.shell import storage

from fused_render.server.common import _error, _require_fused

router = APIRouter()




# Per-file sidecar, now homed under home_dir()/sidecar/ (D83-reversal) rather
# than beside the target — see storage.sidecar_path. Shared with the claude
# chat template, which owns "claudeSessions", and bookmarks, which own
# "bookmarkHistory" (see templates/claude/agent.py and shell/bookmarks.py).
# Read/merge/write preserves every other key so the writers never clobber
# each other (single local user, last-write-wins on a true interleave — D3).
def _sidecar_path(file: str) -> str:
    return storage.sidecar_path(file)


def _read_sidecar(file: str) -> dict:
    # read_json returns None on missing/corrupt; a non-dict (a stray JSON list)
    # is treated as empty so a merge can't crash.
    data = storage.read_json(_sidecar_path(file))
    return data if isinstance(data, dict) else {}


# PARAMS A SIDECAR MAY NOT HOLD (LSN-12, D326). One name so far: `_side`, the
# file preview's companion sidebar — which of the companions is showing, or that
# the user shut it (frontend apps/explorer/lib/preview-side.ts).
#
# It is session-only BY POLICY: the sidebar opens at its default on every page
# load, and a refresh is the way back from any change to it. A sidecar that
# recorded `_side` broke exactly that, because a refresh is WHEN the sidecar is
# replayed — so opening the sidebar once on a file made that file open with a
# sidebar forever, while its neighbour never did, with no way back that a user
# could find. The owner's report was that plainly: "we don't want any persisted
# preference … any other changes being made (open/width) must be persisted only
# for the session."
#
# STRIPPED ON WRITE AND IGNORED ON READ, and the pair is deliberate: stripping
# alone would leave every sidecar already on disk replaying its stale `_side`
# until the file's next qualifying param change, and ignoring alone would keep
# writing a key we then have to keep ignoring. Together, an old file is inert on
# the next read and clean on the next write — it self-heals rather than needing a
# migration pass over the sidecar directory. REFUSING the write instead (a 400, or
# a skip) was the other option and is worse on both counts: `_side` arrives
# alongside perfectly good params, so refusing would throw away the caller's real
# session update to punish one key it did not ask to send.
#
# The frontend strips it too, before the PUT (lib/session-params). That half is not
# redundant: it is what makes a `_side`-only URL read as a BARE url there, so no
# round trip fires at all. This half is the authority.
_OMIT_PARAMS = ("_side",)


def _strip_side(search: str) -> str:
    """Drop the never-persisted params from a query string, TEXTUALLY.

    Not via parse_qsl + urlencode: LSN-2 says the stored `search` is the shell's
    query string verbatim, and a round trip would rewrite what it keeps
    ("q=a+b%2Cc", "stretch=2,1471") on every save of every file."""
    if not search:
        return ""
    kept = [
        p for p in search.split("&")
        if p and p.split("=", 1)[0] not in _OMIT_PARAMS
    ]
    return "&".join(kept)


def _has_non_mode_param(search: str) -> bool:
    # A "qualifying" query has at least one key other than _mode (mirrors the
    # frontend hasQualifyingParam). keep_blank_values so "?city=" still counts.
    # `_side` never reaches here — the caller strips it first (see _strip_side),
    # which is what stops opening a sidebar from STARTING a file's session.
    return any(k != "_mode" for k, _ in parse_qsl(search, keep_blank_values=True))


def _is_file_mount_safe(path: str) -> bool:
    """os.path.isfile, but NEVER a kernel stat on a mount-backed path — a cold
    os.path.isfile there is the GETATTR that lists the whole parent prefix and
    wedges the mount (the /api/session + /api/recents open-flow wedge). Mount
    paths answered via rc_kind_for; only a confirmed "file" passes (a "dir" is
    not a file, matching os.path.isfile), while an "indeterminate" rc probe
    fails OPEN so a transient rcd hiccup never 404s a file the user just
    opened."""
    from fused_render.shell import pathops
    return pathops.is_file(path)


def _stored_session(data: dict):
    """The sidecar's lastSession AS THE APP IS ALLOWED TO SEE IT, or None.

    One reader for both endpoints, because "is there a session" has to mean the
    same thing to the GET that replays one and to the LSN-3 gate that asks whether
    one already exists. The never-persisted params are dropped here (LSN-12), and a
    stored query that was NOTHING BUT them is no session at all: reporting it as
    {"search": ""} would still be a lastSession dict, which the gate reads as "a
    session exists" — promoting a sidebar nobody chose to save into a real session.
    """
    last = data.get("lastSession")
    if not isinstance(last, dict):
        return None
    search = last.get("search")
    if not isinstance(search, str):
        return None  # corrupt/foreign shape — nothing replayable
    kept = _strip_side(search)
    return None if kept == "" else {**last, "search": kept}


def _session_get(path: str):
    if not _is_file_mount_safe(path):
        return _error(f"no such file: {path}", status=404)
    return {"lastSession": _stored_session(_read_sidecar(path))}


def _session_put(body: dict, x_fused: str | None):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path = body.get("path")
    search = body.get("search")
    if not path or not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    if not _is_file_mount_safe(path):
        return _error(f"no such file: {path}", status=404)
    if not isinstance(search, str):
        return _error("'search' must be a string")
    # LSN-12: the never-persisted params go BEFORE the gate below, not after, so a
    # `_side`-only query is an EMPTY one here — it neither starts a session nor
    # clobbers an existing one down to "".
    search = _strip_side(search)
    # No read-only-mount gate here anymore (D83-reversal): the sidecar lives
    # under home_dir()/sidecar/ now, never on the source's mount, so the old
    # sidecar-write incident (CacheMode=full 403-looping a doomed PutObject)
    # structurally can't happen — the read below is local, not a network stat.
    # Read-merge-write the whole dict so claudeSessions / bookmarkHistory
    # survive alongside lastSession (see _read_sidecar comment).
    data = _read_sidecar(path)
    # LSN-3 gate (authoritative, server-side): a _mode-only or empty query must
    # not START a session, but once one exists we DO record _mode-only updates
    # so the file's last _mode is remembered. Save when the query carries a
    # non-_mode param, OR (query is non-empty AND a lastSession already exists).
    # Empty query never clobbers an existing session down to "".
    # Through the same reader the GET uses, so the two cannot disagree about
    # whether this file has a session (see _stored_session).
    has_session = _stored_session(data) is not None
    if not (_has_non_mode_param(search) or (search != "" and has_session)):
        return {"ok": True, "skipped": True}
    data["lastSession"] = {"search": search, "updated_at": time.time()}
    try:
        storage.write_json(_sidecar_path(path), data)
    except OSError as e:
        return _error(f"cannot write sidecar for {path}: {e}", status=400)
    return {"ok": True}


# Per-file session restore (LSN-*): a viewed file remembers its last URL
# query in the "lastSession" key of its <file>.json sidecar. GET is a read
# endpoint (no X-Fused guard); PUT mutates so it carries the D36 guard.
@router.get("/api/session")
def api_session_get(path: str):
    return _session_get(path)

@router.put("/api/session")
def api_session_put(
    body: dict = Body(...), x_fused: str | None = Header(default=None)
):
    return _session_put(body, x_fused)
