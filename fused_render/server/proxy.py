from fastapi import Request


# Media types a browser will EXECUTE as a document rather than display as data.
_SCRIPTABLE_MEDIA = frozenset((
    "text/html", "application/xhtml+xml", "image/svg+xml"))

# Fetch destinations that make the response a document on THIS origin: a
# top-level navigation, or a frame of one.
_DOCUMENT_DESTS = frozenset(("document", "iframe", "frame", "embed", "object"))


def _is_document_load(request: Request) -> bool:
    """True when this request loads the response as a document (see
    _harden_raw). Shared with /api/fs/raw's 304 path, which must never let a
    document load revalidate a cache entry stored under another destination."""
    dest = request.headers.get("sec-fetch-dest", "").lower()
    mode = request.headers.get("sec-fetch-mode", "").lower()
    return dest in _DOCUMENT_DESTS or mode == "navigate"


def _harden_raw(resp, request: Request):
    """Stop /api/fs/raw from handing a page the app's own origin.

    /api/fs/raw serves any absolute path with a content-type guessed from its
    name, and it is a plain GET with no X-Fused, so it is top-level navigable
    — and navigation is not subject to CORS. A foreign site can therefore point
    the browser at a .html file it arranged to be on disk (a drive-by download
    into ~/Downloads names the file for it) and get that file running as a
    FIRST-PARTY document on http://127.0.0.1:<port>. From there it is inside
    the trust boundary: it can send X-Fused: 1 and POST /api/run.

    D4 concedes that an .html file *you open* runs same-origin. That is about
    the user choosing the file; here the attacker chooses it, so the concession
    does not stretch to cover it.

    Two measures, both applied to every response this route produces:

      * `nosniff` unconditionally — every in-tree consumer reads this endpoint
        as data (.text()/.arrayBuffer()), so none of them can be hurt by it;

      * scriptable types are downgraded to text/plain ONLY when the request is
        a document load. The threat is navigating or framing; an <img src> at
        an SVG cannot execute script, and coercing it would break a working
        case to fix one that does not exist. Sec-Fetch-Dest is the right signal
        and this file already relies on browsers always sending Sec-Fetch-*
        (see api_fs_raw). Sec-Fetch-Mode is checked too, for
        a browser that sends the mode but not the dest.

    Redirects are left alone (a redirect carries no body to harden)."""
    if 300 <= resp.status_code < 400:
        return resp
    resp.headers["x-content-type-options"] = "nosniff"
    if _is_document_load(request):
        media = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if media in _SCRIPTABLE_MEDIA:
            # Keep serving the bytes — a download still saves the real file
            # under its own name — just never as a document on this origin.
            resp.headers["content-type"] = "text/plain; charset=utf-8"
    return resp
