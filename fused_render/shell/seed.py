"""First-run onboarding: the ~/Documents/Fused workspace, its seeded examples,
and starter bookmarks (D81).

Called from the real process entry points (cli._run_serve, app._start_server_thread)
— NOT from create_app, so importing the server in tests never touches a user's
real Fused dir. The whole thing is idempotent and non-destructive on upgrades:

  * the dir is created if missing;
  * the bundled examples are copied in ONLY when the dir is empty (an existing,
    non-empty dir is left completely alone — user edits are sacred, we never
    re-seed);
  * starter bookmarks are written ONLY when ~/.fused-render/bookmarks.json is
    absent AND the examples are present — an existing bookmarks.json is never
    touched.

Seeding concerns the Fused dir, independent of the server's --start-dir.
"""
import os
import shutil
import time
import uuid
from urllib.parse import quote

from fused_render.shell import storage

# Seed examples ship inside the wheel at fused_render/examples_seed/, packaged
# by the same mechanism as fused_render/templates/ (pyproject packages =
# ["fused_render"] — every committed file under the package ships).
PACKAGE_SEED_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples_seed"
)

# The two example pages the starter bookmarks point at; both existing is the
# "examples are present" signal that gates bookmark seeding. Sine ships in its
# own subfolder (sine/sine.html + sine/sine.py) so nothing lands loose at the
# workspace root and sine.py's __pycache__ stays inside the subfolder.
_SINE_HTML = os.path.join("sine", "sine.html")
_EXPLAINER_HTML = os.path.join("how_it_works", "explainer.html")


def fused_dir() -> str:
    """The user's Fused workspace: ~/Documents/Fused. FUSED_RENDER_DIR overrides
    it (tests set it so they never touch the real dir). Path only — no I/O.
    Normalized (expanduser + abspath) so a tilde or relative override yields the
    same path everywhere: seeding, bookmark URLs, and /api/config's fused_dir."""
    return os.path.abspath(
        os.path.expanduser(os.environ.get("FUSED_RENDER_DIR") or "~/Documents/Fused")
    )


def _view_url(abs_path: str) -> str:
    """Build a /view/ URL for an absolute fs path, encoding each segment exactly
    as the frontend's urlForFsPath (lib/router.ts): drop the leading slash(es),
    split on '/', URL-encode each non-empty segment (encodeURIComponent parity —
    quote already keeps A-Za-z0-9_.-~, so add !*'() to match), join with '/'."""
    rest = abs_path.lstrip("/")
    segs = [quote(seg, safe="!*'()") for seg in rest.split("/") if seg]
    return "/view/" + "/".join(segs)


# --- Panel `_layout` codec (mirror of frontend/src/lib/layout-codec.ts) -------
# The Sine demo bookmark opens panel mode split into two panes of the SAME page
# — left in the default render mode, right in `code` mode — exactly the URL the
# app itself produces when you open sine/sine.html, click Split right, then set
# the right pane to code mode. The panel URL is NOT the /view/ codec above: the
# layout codec keeps `/ ? & =` literal and escapes only its own delimiters, so
# it must be replicated here rather than reusing _view_url.


def _enc_path(s: str) -> str:
    """encPath(): escape `%` first, then the codec delimiters `, ; ( )` and `?`
    (so a path can never contain the path/query separator)."""
    return (
        s.replace("%", "%25")
        .replace(",", "%2C")
        .replace(";", "%3B")
        .replace("(", "%28")
        .replace(")", "%29")
        .replace("?", "%3F")
    )


def _enc_query(s: str) -> str:
    """encQuery(): same as _enc_path but keep `?` literal (the leading `?` is the
    path/query separator inside a segment)."""
    return (
        s.replace("%", "%25")
        .replace(",", "%2C")
        .replace(";", "%3B")
        .replace("(", "%28")
        .replace(")", "%29")
    )


def _encode_pane_segment(path: str, query: str = "") -> str:
    """encodePaneSegment(): one pane = escaped fs path + escaped query (query
    includes its leading `?`)."""
    return _enc_path(path) + _enc_query(query)


def _url_safe_layout(s: str) -> str:
    """urlSafeLayout(): escape only `%`, `#` and space when placing the codec
    string inside the `_layout=(...)` parens (splitShellSearch reverses it with
    one decodeURIComponent pass)."""
    return s.replace("%", "%25").replace("#", "%23").replace(" ", "%20")


def _sine_panel_url(abs_path: str) -> str:
    """The two-pane split-view URL for the Sine demo: `/view/_panel?_layout=(L,R)`
    where L = the page in default render mode and R = the same page with
    `_mode=code`. `,` is the row (side-by-side) separator; the whole layout is
    parenthesized and emitted last, per the D51 grammar in buildSentinelUrl()."""
    left = _encode_pane_segment(abs_path, "")
    right = _encode_pane_segment(abs_path, "?_mode=code")
    codec = left + "," + right
    return "/view/_panel?_layout=(" + _url_safe_layout(codec) + ")"


def _examples_present(fdir: str) -> bool:
    return os.path.isfile(os.path.join(fdir, _SINE_HTML)) and os.path.isfile(
        os.path.join(fdir, _EXPLAINER_HTML)
    )


def _seed_examples(fdir: str) -> bool:
    """Copy the packaged seed set into fdir iff it is empty. Returns True when it
    copied, False when the dir already had content (never re-seed)."""
    try:
        # Hidden metadata (.DS_Store etc.) is not user content: a dir holding
        # only dot-entries still counts as empty and gets seeded.
        nonempty = any(not entry.name.startswith(".") for entry in os.scandir(fdir))
    except FileNotFoundError:
        nonempty = False
    if nonempty:
        return False
    # dirs_exist_ok: fdir was just makedirs'd (empty), so it exists.
    shutil.copytree(PACKAGE_SEED_DIR, fdir, dirs_exist_ok=True)
    return True


def _seed_bookmarks(fdir: str) -> None:
    """Write the two starter bookmarks iff ~/.fused-render/bookmarks.json does
    not exist. An existing file (even a corrupt one) is never overwritten — the
    one-time-write gate the store already relies on (D75)."""
    path = os.path.join(storage.home_dir(), "bookmarks.json")
    if os.path.exists(path):
        return
    now = int(time.time() * 1000)  # ms, matching the frontend's Date.now()
    bookmarks = [
        {
            "id": str(uuid.uuid4()),  # same UUIDv4 shape as crypto.randomUUID()
            "name": "Sine demo",
            # Split view: rendered sine page beside its source (code mode).
            "url": _sine_panel_url(os.path.join(fdir, _SINE_HTML)),
            "created_at": now,
        },
        {
            "id": str(uuid.uuid4()),
            "name": "How it works",
            "url": _view_url(os.path.join(fdir, _EXPLAINER_HTML)),
            "created_at": now,
        },
    ]
    storage.write_json(path, bookmarks)


def ensure_fused_dir() -> str:
    """Create ~/Documents/Fused, seed examples into it once (empty dir only), and
    seed starter bookmarks once (absent bookmarks.json + examples present).
    Idempotent, non-destructive on upgrades. Returns the abs Fused dir."""
    fdir = os.path.abspath(fused_dir())
    os.makedirs(fdir, exist_ok=True)

    seeded = _seed_examples(fdir)
    # Bookmarks ride alongside a fresh example set — or an existing one the user
    # kept — but never onto an unrelated dir the user filled with their own work.
    if seeded or _examples_present(fdir):
        _seed_bookmarks(fdir)
    return fdir
