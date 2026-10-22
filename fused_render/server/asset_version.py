"""Content-versioned URLs for the static files every rendered page loads.

`/static/runtime.js`, `/template-assets/*` and `/template-shared/*` used to be
`no-cache`, so each page open revalidated every script. A URL carrying
`?v=<token>` is now served immutable — but only when the token equals the
file's CURRENT token, so an old versioned URL can never serve stale bytes.

The token is `<mtime_ns hex>-<size hex>` of the exact file, from one `os.stat`
per use (no startup hash, no cache). That is right in both modes by
construction: a re-staged core template gets a new mtime, and a live
`FUSED_RENDER_CORE_TEMPLATES` file changes mtime on every edit, so an edit
produces a new URL. Nothing here imports `server.common` at module level
(common imports this module).
"""
import os
import re
import stat


def _token(full: str) -> str | None:
    try:
        st = os.stat(full)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return f"{st.st_mtime_ns:x}-{st.st_size:x}"


def _mount_file(base: str, rel: str) -> str | None:
    if not rel or "\\" in rel or "\x00" in rel:
        return None
    if any(seg in ("", ".", "..") for seg in rel.split("/")):
        return None
    full = os.path.normpath(os.path.join(base, rel))
    try:
        if os.path.commonpath([base, full]) != base:
            return None
    except ValueError:
        return None
    return full


def asset_version(url_path: str) -> str | None:
    """The version token for `url_path`, or None when it is not one of the
    three versioned families or does not name a regular file."""
    if url_path == "/static/runtime.js":
        from fused_render.server.common import STATIC_DIR

        return _token(os.path.join(STATIC_DIR, "runtime.js"))
    # Read at CALL time: tests (and the staging step) repoint TEMPLATES_DIR.
    from fused_render.server import templates as _t

    for prefix, sub in (("/template-assets/", "vendor"), ("/template-shared/", "shared")):
        if url_path.startswith(prefix):
            base = os.path.join(_t.TEMPLATES_DIR, sub)
            full = _mount_file(base, url_path[len(prefix):])
            return _token(full) if full else None
    return None


def versioned(url_path: str) -> str:
    """`url_path?v=<token>` when the file exists, else `url_path` unchanged."""
    v = asset_version(url_path)
    return f"{url_path}?v={v}" if v is not None else url_path


_ASSET_REF = re.compile(
    r'(?<![\w/.\-])(/template-(?:assets|shared)/[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*)(?![\w?#/])')


def rewrite_asset_refs(html: str) -> str:
    """Version every bare `/template-assets/<file>` and `/template-shared/<file>`
    reference in a page (script src, dynamic import, worker URL). A reference
    to a missing file, or one that already carries a query, is left as written."""
    return _ASSET_REF.sub(lambda m: versioned(m.group(1)), html)
