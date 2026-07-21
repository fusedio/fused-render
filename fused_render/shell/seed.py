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

# Seed examples live at the repo root (examples_seed/) and are force-included
# into the wheel at fused_render/examples_seed/ (pyproject
# [tool.hatch.build.targets.wheel.force-include]). Installed wheels find them
# inside the package; editable/dev installs (where force-include does not
# materialize files) fall back to the repo-root copy.
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_IN_PACKAGE = os.path.join(_PKG_DIR, "examples_seed")
_REPO_ROOT = os.path.join(os.path.dirname(_PKG_DIR), "examples_seed")
PACKAGE_SEED_DIR = _IN_PACKAGE if os.path.isdir(_IN_PACKAGE) else _REPO_ROOT

# The example pages the starter bookmarks point at; any of them existing is
# the "examples are present" signal that gates bookmark seeding, and each
# bookmark is written only when its own target exists (a legacy workspace may
# hold an older seed set missing some of them). Showcase is also the
# first-launch landing page (see ensure_fused_dir_and_landing). Sine ships in
# its own subfolder (sine/sine.html + sine/sine.py) so nothing lands loose at
# the workspace root and sine.py's __pycache__ stays inside the subfolder.
_SHOWCASE_HTML = os.path.join("showcase", "index.html")
_TUTORIAL_HTML = os.path.join("tutorial", "index.html")
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
    return any(
        os.path.isfile(os.path.join(fdir, rel))
        for rel in (_TUTORIAL_HTML, _SHOWCASE_HTML, _SINE_HTML, _EXPLAINER_HTML)
    )


def _remove(path: str) -> None:
    """Best-effort delete of a file or directory tree; silent on absence."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def _clear_partials(fdir: str) -> None:
    """Remove any ".<name>.partial" leftovers from a previously interrupted seed.
    These are the temp targets a crash can strand mid-copy; they are hidden so
    they never counted as user content, but they must be cleared before a retry
    so the atomic os.rename below lands on a clean name."""
    try:
        entries = list(os.scandir(fdir))
    except FileNotFoundError:
        return
    for entry in entries:
        if entry.name.endswith(".partial"):
            _remove(entry.path)


def _seed_examples(fdir: str) -> bool:
    """Copy the packaged seed set into fdir iff it is empty. Returns True when it
    copied, False when the dir already had content (never re-seed).

    Each example is materialized atomically: fully copied under a hidden
    ".<name>.partial" sibling inside fdir, then os.rename'd into place. A crash
    mid-copy therefore leaves only a hidden ".*.partial" (cleaned on the next
    run), never a half-written example dir that would make fdir look non-empty
    and wedge seeding off forever."""
    # Clear stale partials FIRST, before the emptiness check, so an interrupted
    # prior run can be retried instead of being skipped as "already seeded".
    _clear_partials(fdir)
    try:
        # Hidden metadata (.DS_Store etc.) is not user content: a dir holding
        # only dot-entries still counts as empty and gets seeded.
        nonempty = any(not entry.name.startswith(".") for entry in os.scandir(fdir))
    except FileNotFoundError:
        nonempty = False
    if nonempty:
        return False
    for entry in os.scandir(PACKAGE_SEED_DIR):
        # Hidden metadata (.DS_Store a dev machine dropped into the package
        # dir) is not seed content — mirror the emptiness check above.
        if entry.name.startswith("."):
            continue
        dest = os.path.join(fdir, entry.name)
        partial = os.path.join(fdir, "." + entry.name + ".partial")
        _remove(partial)  # defensive: no residue from this same run
        if entry.is_dir():
            shutil.copytree(entry.path, partial)
        else:
            shutil.copy2(entry.path, partial)
        os.rename(partial, dest)
    return True


def _seed_bookmarks(fdir: str) -> None:
    """Write the starter bookmarks iff ~/.fused-render/bookmarks.json does
    not exist. An existing file (even a corrupt one) is never overwritten — the
    one-time-write gate the store already relies on (D75)."""
    path = os.path.join(storage.home_dir(), "bookmarks.json")
    if os.path.exists(path):
        return
    now = int(time.time() * 1000)  # ms, matching the frontend's Date.now()
    starters = (
        ("Tutorial", _TUTORIAL_HTML, _view_url),
        ("Showcase", _SHOWCASE_HTML, _view_url),
        # Split view: rendered sine page beside its source (code mode).
        ("Sine demo", _SINE_HTML, _sine_panel_url),
        ("How it works", _EXPLAINER_HTML, _view_url),
    )
    bookmarks = [
        {
            "id": str(uuid.uuid4()),  # same UUIDv4 shape as crypto.randomUUID()
            "name": name,
            "url": url_for(os.path.join(fdir, rel)),
            "created_at": now,
        }
        # Only bookmark pages that actually exist: a legacy workspace (older
        # seed set, bookmarks.json deleted) may be missing some of them, and a
        # bookmark onto a nonexistent file is worse than none.
        for name, rel, url_for in starters
        if os.path.isfile(os.path.join(fdir, rel))
    ]
    if bookmarks:
        storage.write_json(path, bookmarks)


def ensure_fused_dir() -> str:
    """Create ~/Documents/Fused, seed examples into it once (empty dir only), and
    seed starter bookmarks once (absent bookmarks.json + examples present).
    Idempotent, non-destructive on upgrades. Returns the abs Fused dir."""
    return ensure_fused_dir_and_landing()[0]


def ensure_fused_dir_and_landing() -> tuple[str, str | None]:
    """ensure_fused_dir plus the first-launch landing URL.

    Returns (fused_dir, landing): `landing` is the /view/ URL of the seeded
    showcase page iff THIS run performed the one-time example seed — the same
    first-run condition that gates everything else here — so a brand-new
    install's first browser tab opens on the showcase instead of the bare
    workspace listing. Every later run (dir already non-empty) returns None
    and the entry points open the root URL exactly as before."""
    fdir = os.path.abspath(fused_dir())
    os.makedirs(fdir, exist_ok=True)

    seeded = _seed_examples(fdir)
    # Bookmarks ride alongside a fresh example set — or an existing one the user
    # kept — but never onto an unrelated dir the user filled with their own work.
    if seeded or _examples_present(fdir):
        _seed_bookmarks(fdir)

    landing = None
    if seeded:
        showcase = os.path.join(fdir, _SHOWCASE_HTML)
        if os.path.isfile(showcase):
            landing = _view_url(showcase)
    return fdir, landing
