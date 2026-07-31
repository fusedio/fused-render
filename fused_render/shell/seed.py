"""First-run onboarding: the ~/Documents/Fused workspace and its seeded
examples (D81).

Called from the real process entry points (cli._run_serve, app._start_server_thread)
— NOT from create_app, so importing the server in tests never touches a user's
real Fused dir. The whole thing is idempotent and non-destructive on upgrades:

  * the dir is created if missing;
  * the bundled examples are copied in ONLY when the dir is empty (an existing,
    non-empty dir is left completely alone — user edits are sacred, we never
    re-seed).

Seeding concerns the Fused dir, independent of the server's --start-dir.
"""
import os
import shutil

from fused_render._view_url_codec import view_url_path

# Seed examples live at the repo root (examples_seed/) and are force-included
# into the wheel at fused_render/examples_seed/ (pyproject
# [tool.hatch.build.targets.wheel.force-include]). Installed wheels find them
# inside the package; editable/dev installs (where force-include does not
# materialize files) fall back to the repo-root copy. Probe a real seed file,
# not just the dir: git can leave an empty fused_render/examples_seed/ behind
# after the move, and an empty dir must not shadow the repo-root copy.
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_IN_PACKAGE = os.path.join(_PKG_DIR, "examples_seed")
_REPO_ROOT = os.path.join(os.path.dirname(_PKG_DIR), "examples_seed")
PACKAGE_SEED_DIR = (
    _IN_PACKAGE
    if os.path.isfile(os.path.join(_IN_PACKAGE, "sine", "sine.html"))
    else _REPO_ROOT
)

# Showcase is the first-launch landing page (see ensure_fused_dir_and_landing).
#
# Seeded under "examples/" (not the workspace root) so it carries the
# "examples" tag in the Home apps grid (root/<tag>/<project> — apps.py scans
# any top-level dir as a tag, any dir directly inside it as a project).
_EXAMPLES_SUBDIR = "examples"
_SHOWCASE_HTML = os.path.join(_EXAMPLES_SUBDIR, "showcase", "index.html")


def fused_dir() -> str:
    """The user's Fused workspace: ~/Documents/Fused. FUSED_RENDER_DIR overrides
    it (tests set it so they never touch the real dir). Path only — no I/O.
    Normalized (expanduser + abspath) so a tilde or relative override yields the
    same path everywhere: seeding and /api/config's fused_dir."""
    return os.path.abspath(
        os.path.expanduser(os.environ.get("FUSED_RENDER_DIR") or "~/Documents/Fused")
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
    """Copy the packaged seed set into fdir/examples/ iff fdir is empty. Returns
    True when it copied, False when the dir already had content (never
    re-seed).

    The whole examples/ tree is materialized atomically: every packaged entry
    is copied into a single hidden ".examples.partial" staging dir directly
    under fdir, then one os.rename publishes it as "examples". A crash
    mid-copy therefore leaves only the hidden ".examples.partial" (cleared on
    the next run's retry, which redoes the whole copy), never a half-written
    "examples" dir that would make fdir look non-empty and wedge seeding off
    forever."""
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
    partial = os.path.join(fdir, "." + _EXAMPLES_SUBDIR + ".partial")
    _remove(partial)  # defensive: no residue from this same run
    os.makedirs(partial)
    for entry in os.scandir(PACKAGE_SEED_DIR):
        # Hidden metadata (.DS_Store a dev machine dropped into the package
        # dir) is not seed content — mirror the emptiness check above.
        if entry.name.startswith("."):
            continue
        dest = os.path.join(partial, entry.name)
        if entry.is_dir():
            shutil.copytree(entry.path, dest)
        else:
            shutil.copy2(entry.path, dest)
    os.rename(partial, os.path.join(fdir, _EXAMPLES_SUBDIR))
    return True


def ensure_fused_dir() -> str:
    """Create ~/Documents/Fused and seed examples into it once (empty dir only).
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

    landing = None
    if seeded:
        showcase = os.path.join(fdir, _SHOWCASE_HTML)
        if os.path.isfile(showcase):
            landing = view_url_path(showcase)
    return fdir, landing
