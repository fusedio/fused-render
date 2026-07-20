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

# The two example pages the starter bookmarks point at; any of them existing is
# the "examples are present" signal that gates bookmark seeding, and each
# bookmark is written only when its own target exists (a legacy workspace may
# hold an older seed set missing some of them). Showcase is also the
# first-launch landing page (see ensure_fused_dir_and_landing).
_SHOWCASE_HTML = os.path.join("showcase", "index.html")
_TUTORIAL_HTML = os.path.join("tutorial", "index.html")


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


def _examples_present(fdir: str) -> bool:
    return os.path.isfile(os.path.join(fdir, _SHOWCASE_HTML)) or os.path.isfile(
        os.path.join(fdir, _TUTORIAL_HTML)
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
            "name": name,
            "url": _view_url(os.path.join(fdir, rel)),
            "created_at": now,
        }
        # Only bookmark pages that actually exist: a legacy workspace (older
        # seed set, bookmarks.json deleted) may be missing some of them, and a
        # bookmark onto a nonexistent file is worse than none.
        for name, rel in (("Showcase", _SHOWCASE_HTML), ("Tutorial", _TUTORIAL_HTML))
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
