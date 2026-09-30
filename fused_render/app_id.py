"""An app's stable identity: `<meta name="fused-app-id">` in its entry page.

Two `.fused` files, or a `.fused` and a folder, can be the SAME app at two
points in time (an update) or two different apps that happen to share a
name — and nothing in the folder's name, path or content settles which. So
an app carries one identifier that never changes over its life, stamped
beside the other markers in its entry page:

    <meta name="fused-app" />
    <meta name="fused-api-version" content="1" />
    <meta name="fused-app-id" content="my-app-test-82de2580" />

The value is the app's name in kebab case plus eight random hex digits:
readable in a listing, unique enough that two apps named alike never
collide, and safe to compare as an opaque string. It is MINTED ONCE — at app
creation (`routers/apps` stamps the fresh starter copy before the
boilerplate commit, so the tag is in history from the first commit), or on
the first export (`appfile.export_app_file`) for an app created before that
existed — and then only ever read. Renaming or moving the folder, editing
every other byte, cloning the `.fused` back into a workspace: none of them
touch it, which is the whole point.

Why the entry page and not a sidecar: the tag lives in the one file every
app already must have, it is committed with the app (the `.fused/` state dir
is gitignored and rebuilt at will, so a stamp there would not survive), it
ships inside every `.fused` and it reads through the same 4 KiB head budget
as the other two markers (`app_listing.has_fused_meta`,
`fused_api_version.version_from_text`). The `.fused` container copies the id
into its index too, so a file's identity is one bounded manifest read away
with nothing extracted (`appfile.read_manifest`).

The id is never a path: `appfile._slug` remains the only thing that turns a
manifest string into a filesystem segment, and the id is matched, not
joined. A value read out of a file that fails `ID_RE` is treated as absent.
"""
from __future__ import annotations

import os
import re
import secrets

META_NAME = "fused-app-id"

# Same head budget as `app_listing.has_fused_meta`.
_META_SCAN_BYTES = 4096

# `<kebab name>-<8 hex>`. The name part is 1..48 chars so the whole id stays
# under 64 — long enough for any real app name, short enough for a listing
# column and for a cache key.
_HEX_LEN = 8
_NAME_MAX = 48
ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,%d})-[0-9a-f]{%d}$" % (_NAME_MAX - 1, _HEX_LEN))

# Two steps, like `fused_api_version`: find the tag that names us, then read
# `content` out of THAT tag, so attribute order does not matter.
_TAG_RE = re.compile(
    rb"<meta\s[^>]*name\s*=\s*[\"']?fused-app-id[\"']?[^>]*>", re.IGNORECASE)
_CONTENT_RE = re.compile(rb"content\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)

# Insertion anchors, most specific first: right after the api-version tag,
# else after the app marker, else after <meta charset>, else after <head>.
_ANCHORS = (
    re.compile(r"<meta\s[^>]*name\s*=\s*[\"']?fused-api-version[\"']?[^>]*>", re.IGNORECASE),
    re.compile(r"<meta\s[^>]*name\s*=\s*[\"']?fused-app[\"']?[^>]*>", re.IGNORECASE),
    re.compile(r"<meta\s[^>]*charset[^>]*>", re.IGNORECASE),
    re.compile(r"<head[^>]*>", re.IGNORECASE),
)


def is_valid(value: object) -> bool:
    return isinstance(value, str) and ID_RE.match(value) is not None


def id_from_text(head: bytes | str) -> str | None:
    """The declared app id in a page's head bytes; None when the tag is
    absent or its content is not a well-formed id."""
    # Encode BEFORE slicing: the budget is bytes on disk, and a character
    # slice of a multi-byte-heavy head would see further than any reader
    # of the file does.
    if isinstance(head, str):
        head = head.encode("utf-8", "ignore")
    head = head[:_META_SCAN_BYTES]
    tag = _TAG_RE.search(head)
    if not tag:
        return None
    m = _CONTENT_RE.search(tag.group(0))
    if not m:
        return None
    value = m.group(1).decode("utf-8", "ignore").strip()
    return value if is_valid(value) else None


def app_id(html_path: str) -> str | None:
    """The app id an entry page declares, None when undeclared. Never
    raises — an unreadable page is an unidentified one."""
    try:
        with open(html_path, "rb") as fh:
            head = fh.read(_META_SCAN_BYTES)
    except OSError:
        return None
    return id_from_text(head)


def kebab(name: str, fallback: str = "app") -> str:
    """``name`` as one lowercase hyphenated word, capped for the id."""
    word = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    word = word[:_NAME_MAX].strip("-")
    return word or fallback


def mint(name: str) -> str:
    """A fresh id for an app called ``name``: ``my-app-test-82de2580``."""
    value = f"{kebab(name)}-{secrets.token_hex(_HEX_LEN // 2)}"
    assert is_valid(value), value
    return value


def _insert_tag(text: str, value: str) -> str | None:
    """``text`` with the id tag inserted after the best anchor, or None when
    no anchor exists (a page with no <head> at all)."""
    for anchor in _ANCHORS:
        m = anchor.search(text[:_META_SCAN_BYTES])
        if m:
            at = m.end()
            return text[:at] + f'\n<meta name="{META_NAME}" content="{value}" />' + text[at:]
    return None


def stamp_entry(entry_html: str, value: str) -> bool:
    """Write ``value`` into ``entry_html`` as the id tag. True when the file
    now carries it (already there with this value, or just written); False
    when it could not be stamped — read-only file, not UTF-8, no anchor, or
    already carrying a DIFFERENT id (never overwritten: an id is for life).
    Never raises."""
    if not is_valid(value):
        return False
    try:
        existing = app_id(entry_html)
        if existing is not None:
            return existing == value
        with open(entry_html, "r", encoding="utf-8") as fh:
            text = fh.read()
        updated = _insert_tag(text, value)
        if updated is None:
            return False
        # The anchor was found in a CHARACTER slice, but readers scan a BYTE
        # budget: a multi-byte-heavy head can push the new tag past the 4 KiB
        # every reader (this module, `has_fused_meta`) actually looks at. An
        # id nobody can read back is not an id — the next export would mint
        # another — so the write only stands if it reads back.
        if id_from_text(updated) != value:
            return False
        with open(entry_html, "w", encoding="utf-8") as fh:
            fh.write(updated)
        if app_id(entry_html) == value:
            return True
        # Belt and braces: the on-disk read disagrees with the in-memory one
        # (a filesystem transcoding, a concurrent write). Put the page back.
        with open(entry_html, "w", encoding="utf-8") as fh:
            fh.write(text)
        return False
    except (OSError, UnicodeError):
        return False


def ensure(entry_html: str, name: str) -> str | None:
    """The entry page's id, minting and stamping one when it has none.
    None when the page has no id and cannot take one — the caller then
    proceeds without an identity rather than inventing one it cannot
    persist (an id that is not in the page is not an id: the next export
    would mint another)."""
    existing = app_id(entry_html)
    if existing is not None:
        return existing
    value = mint(name or os.path.basename(os.path.dirname(entry_html)))
    return value if stamp_entry(entry_html, value) else None
