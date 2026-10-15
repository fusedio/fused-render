"""Deep links (SPEC §26). Three `open` payloads, all ending on a `.fused`
file's INSTALL PAGE (AF-14) — the one gate before anything runs:

  fused-render://open?file=<absolute .fused path>       (DL-7)
  fused-render://open?url=<http(s) link to a .fused>    (DL-8)
  fused-render://open?git=https://github.com/<owner>/<repo>  (DL-9, one app)

plus the payload-free `launch` / `relaunch` actions (D128) the server banner,
the update dialog and the FDA relaunch button use.

Every OS-delivered link (macOS CFBundleURLTypes, the Linux desktop entry's
MimeType) lands the browser on `GET /clone?src=<raw link>`, which never
writes (D3) and has no page of its own:

- `file=` (Render App's title-bar Edit button): the path is percent-encoded
  once by the sender and unquoted once here; a 303 to the file's install page,
  where the user installs or updates behind the X-Fused
  `/api/appfile/install`. A malformed payload goes to Home under
  `?_edit_appfile=` for the shell to report.
- `url=` (a web page's "Open in fused-render" link, Render App's
  `render-app://open?url=` ported): a 303 to Home with the link as
  `?_fetch_appfile=`.
- `git=` (a public GitHub repo holding ONE app): a 303 to Home with the repo
  link as `?_fetch_appgit=`.

The shell's `FetchAppFileBoot` reads either hand-off once, downloads through
the X-Fused `POST /api/appfile/fetch` (appfetch.py — `{url}` downloads the
`.fused`, `{git}` builds one from the repo's archive; both into
`~/.fused-render/downloads/<app_id>.fused`) and hard-loads the file's
chrome-free embed URL, the install page. In a native window that window
becomes the app's own (the URL observer re-keys it; title-bar Edit leads to
the explorer); in a browser tab the EmbedStrip's "Open in explorer" does. No
confirm before the download (owner call, mirrors lite PR #30): the link
click is the gesture, the install page the confirm. The payloads are ferried
verbatim; appfetch validates them and the shell reports its error.
"""
import os
from urllib.parse import quote, unquote

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from fused_render import _flavor
from fused_render._view_url_codec import view_url_path as _view_url_path

router = APIRouter()

# The action is host-position ("open"), payloads are query params (owner
# call, D110). The scheme is the flavor's (`_flavor.scheme()`: `fused-render`
# for Fused Render, `fused-bot` for Fused Bot — each bundle registers only its
# own in CFBundleURLTypes). Every accepted form below is built from it, so the
# two apps never claim each other's links.
_SCHEME = _flavor.scheme()
_OPEN_GIT_PREFIXES = (f"{_SCHEME}://open?git=", f"{_SCHEME}://open/?git=")
_OPEN_FILE_PREFIXES = (f"{_SCHEME}://open?file=", f"{_SCHEME}://open/?file=")
_OPEN_URL_PREFIXES = (f"{_SCHEME}://open?url=", f"{_SCHEME}://open/?url=")

# The launch action (D128) is payload-free by definition: any query or extra
# path makes the link NOT a launch link (strictness keeps the grammar clean —
# a future launch payload would be a query param on this same action, and
# silently ignoring one today would freeze "ignored" into the contract).
# `fused-render:launch` (no slashes) is included because some carriers strip
# the empty authority from an opaque scheme URL.
_LAUNCH_FORMS = frozenset(
    {f"{_SCHEME}://launch", f"{_SCHEME}://launch/", f"{_SCHEME}:launch"}
)

# The relaunch action: quit the running app and respawn it from the bundle on
# disk, so a newer installed version takes over (the update dialog's
# button, server-status.ts). Payload-free under the same strictness rules as
# launch above.
_RELAUNCH_FORMS = frozenset(
    {f"{_SCHEME}://relaunch", f"{_SCHEME}://relaunch/", f"{_SCHEME}:relaunch"}
)

# The relaunch action's one payload (D110: payloads are query params on the
# same action): `reason=fda`. A Full Disk Access grant applies to the NEXT
# process — macOS caches the running one's verdict — so the shell's "Relaunch
# to apply" button (FdaStrip / FdaStep) needs a relaunch that respawns the SAME
# version, which the bare relaunch refuses to do (it only acts when the disk
# version differs). Exact forms, not a parser: the payload has one legal value.
_RELAUNCH_FDA_FORMS = frozenset(
    {
        f"{_SCHEME}://relaunch?reason=fda",
        f"{_SCHEME}://relaunch/?reason=fda",
        f"{_SCHEME}:relaunch?reason=fda",
    }
)


class DeeplinkError(Exception):
    """User-reportable failure in a link's payload."""


def is_launch_url(src: str) -> bool:
    """True for a `fused-render://launch` deep link (D128): the action-only
    "make sure the app/server is running" form. Case-insensitive, optional
    trailing slash tolerated; a query string or any payload disqualifies it."""
    return (src or "").strip().lower() in _LAUNCH_FORMS


def is_relaunch_url(src: str) -> bool:
    """True for a `fused-render://relaunch` deep link: quit-and-respawn so the
    version installed on disk takes over. Same strictness as is_launch_url."""
    return (src or "").strip().lower() in _RELAUNCH_FORMS


def is_fda_relaunch_url(src: str) -> bool:
    """True for `fused-render://relaunch?reason=fda`: quit-and-respawn the SAME
    bundle so a freshly granted Full Disk Access takes effect."""
    return (src or "").strip().lower() in _RELAUNCH_FDA_FORMS


def _payload(src: str, prefixes: tuple[str, ...]) -> str | None:
    """The still-encoded value after one of ``prefixes`` (case-insensitive),
    taken verbatim to end-of-string — not parse_qsl'd, so `+`/`&` inside an
    embedded URL survive — or None when ``src`` is not that kind of link."""
    src = (src or "").strip()
    low = src.lower()
    for prefix in prefixes:
        if low.startswith(prefix):
            return src[len(prefix):]
    return None


def file_payload_from(src: str) -> str | None:
    """The still-encoded ``file=`` value of a file link, or None when ``src``
    is not one. Shared by the validating parse below and the route that must
    ferry even a malformed payload to the shell verbatim."""
    return _payload(src, _OPEN_FILE_PREFIXES)


def url_payload_from(src: str) -> str | None:
    """The still-encoded ``url=`` value of a url link, or None."""
    return _payload(src, _OPEN_URL_PREFIXES)


def git_payload_from(src: str) -> str | None:
    """The ``git=`` value of a git link, percent-decoded once when it arrives
    encoded (some carriers encode the embedded URL, most do not), or None."""
    raw = _payload(src, _OPEN_GIT_PREFIXES)
    if raw is None:
        return None
    if not raw.lower().startswith(("https://", "http://")) and "%" in raw:
        raw = unquote(raw)
    return raw.strip()


def app_file_path_from(src: str) -> str | None:
    """The absolute ``.fused`` path a ``fused-render://open?file=`` link
    carries, or None when ``src`` is not a file link at all.

    Percent-decoded exactly once — the sender's side of the contract is one
    ``quote(path, safe="")`` (Render App's ``editlink.py``), so ``&``, ``#``,
    spaces and a literal ``%`` in a filename all round-trip. A file link that
    decodes to something other than an absolute ``.fused`` path is an error,
    not a fall-through: the link named a kind and got its payload wrong.
    """
    raw = file_payload_from(src)
    if raw is None:
        return None
    path = unquote(raw)
    if not path or not os.path.isabs(path):
        raise DeeplinkError(f"file link needs an absolute path: {path or '(empty)'}")
    if not path.lower().endswith(".fused"):
        raise DeeplinkError(f"file link must name a .fused app file: {path}")
    return os.path.normpath(path)


#: The query param a MALFORMED file link is handed to the shell under (DL-7),
#: so the shell can report it in-app; a well-formed one goes straight to the
#: file's install page. Underscore like `_preview`: shell-internal, stripped
#: by the shell on first read. Mirrored in frontend edit-appfile-lib.ts.
EDIT_APPFILE_PARAM = "_edit_appfile"
#: A url link's http(s) link, for the shell to download (DL-8).
FETCH_APPFILE_PARAM = "_fetch_appfile"
#: A git link's GitHub repo link, for the shell to build + download (DL-9).
FETCH_APPGIT_PARAM = "_fetch_appgit"


def edit_appfile_redirect(path: str) -> str:
    """Where the OS-delivered ``file=`` link sends the browser: the file's
    own INSTALL PAGE (AF-14) — the ``fusedapp`` template at the `.fused`'s
    embed URL, the same place a double-click lands. A missing or unreadable
    file lands on the same page, whose own read reports it — one error
    surface."""
    return _view_url_path(os.path.abspath(path))


def _home_with(param: str, value: str) -> RedirectResponse:
    return RedirectResponse(f"/?{param}=" + quote(value, safe=""), status_code=303)


# ---- Routes (included by server.create_app) ---------------------------------


@router.get("/clone")
def open_link(src: str = ""):
    """Every OS-delivered fused-render: link lands here (the path predates
    the clone flow's removal; every platform's launcher builds it, see
    `_view_url_codec.open_target_path`). Nothing is written on this GET (D3):
    a file link redirects to its install page, a url/git link to Home for the
    shell's guarded download."""
    raw = file_payload_from(src)
    if raw is not None:
        try:
            return RedirectResponse(edit_appfile_redirect(app_file_path_from(src)),
                                    status_code=303)
        except DeeplinkError:
            return _home_with(EDIT_APPFILE_PARAM, unquote(raw))
    raw = url_payload_from(src)
    if raw is not None:
        return _home_with(FETCH_APPFILE_PARAM, unquote(raw).strip())
    git = git_payload_from(src)
    if git is not None:
        return _home_with(FETCH_APPGIT_PARAM, git)
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Unsupported link</title>"
        "<p>Unsupported link. fused-render opens <code>open?file=</code>, "
        "<code>open?url=</code> and <code>open?git=</code> links.</p>",
        status_code=400,
    )
