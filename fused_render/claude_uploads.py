"""Files the user attached to Claude Code conversations, listed as apps.

Claude Code keeps a local copy of everything pasted or attached into a
session::

    <claude-config-dir>/uploads/<sessionId>/<hex>-<original-name>

Those are the user's OWN files — a screenshot they pasted, a CSV they handed
to a session, a listing they saved as text — and once the conversation
scrolls away, this store is often the only place that copy still exists. So
it earns the same treatment as the other discovered sources: one toggleable
listing, read-only, one file = one card.

Same shape rules as `claude_sessions` (whose config-dir resolution and
viewable-suffix policy this module shares): only viewable files earn a card,
`entry_html` only when the file really is a page, recency from the
filesystem. The tag is the constant ``"uploads"`` rather than a per-session
one — an attachment's session id means nothing to a person, and one chip that
gathers all of them is how the hub can filter them in or out at a glance.

The walk is exactly two levels (session dirs → files), skips whatever it
cannot read, and is capped with the same islice-plus-lookahead pattern every
discovered source uses, so the cap stops the work and is never silent.
"""
import itertools
import logging
import os
import re

from fused_render import app_listing, claude_sessions

logger = logging.getLogger("fused_render")

#: The `source` these cards carry. A FILE source like `claude-session`: the
#: card's `path` is the file itself, never a folder to open as a project.
SOURCE = "claude-upload"

#: One tag for the whole store — see the module docstring.
TAG = "uploads"

MAX_UPLOADS = 500

#: Claude Code prefixes each stored attachment with a short hex id. Strip it
#: for the card's name (the user knows "files.txt", not "d87044df-files.txt"),
#: tolerantly — an unprefixed name keeps itself, same posture as
#: `claude_science.artifact_name` toward another app's naming scheme.
_PREFIX_RE = re.compile(r"^[0-9a-f]{6,32}-(?P<name>.+)$", re.IGNORECASE)


def uploads_dir() -> str:
    """Where the attachments live. The prefs page's availability probe."""
    return os.path.join(claude_sessions.config_dir(), "uploads")


def upload_name(filename: str) -> str:
    match = _PREFIX_RE.match(filename)
    return match.group("name") if match else filename


def _iter_apps():
    root = uploads_dir()
    try:
        sessions = sorted(os.listdir(root))
    except OSError:
        return  # no store — the normal state, not an error
    for session in sessions:
        if session.startswith("."):
            continue
        session_dir = os.path.join(root, session)
        try:
            with os.scandir(session_dir) as it:
                entries = sorted(it, key=lambda e: e.name)
        except (OSError, ValueError):
            continue
        for entry in entries:
            try:
                if entry.name.startswith(".") or not entry.is_file():
                    continue
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            path = os.path.abspath(entry.path)
            if not path.lower().endswith(claude_sessions.VIEWABLE_SUFFIXES):
                continue
            is_page = app_listing.is_html(path)
            yield {
                "name": upload_name(entry.name),
                "tag": TAG,
                "path": path,
                "entry": path,
                "entry_html": path if is_page else None,
                "title": app_listing.entry_title(path) if is_page else None,
                "updated_at": mtime,
                "source": SOURCE,
            }


def list_apps() -> list[dict]:
    """Every viewable attachment in the store, as app dicts. Unsorted — the
    caller merges and sorts once. Empty when there is no store."""
    stream = _iter_apps()
    apps = list(itertools.islice(stream, MAX_UPLOADS))
    if next(stream, None) is not None:
        logger.warning("claude-upload: listing capped at %d files; the store "
                       "holds more and the walk stopped there (store: %s)",
                       MAX_UPLOADS, uploads_dir())
    return apps
