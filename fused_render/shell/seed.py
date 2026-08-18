"""First-run onboarding: the ~/Fused workspace (D81, relocated by D337).

Called from the real process entry points (cli._run_serve, app._start_server_thread)
— NOT from create_app, so importing the server in tests never touches a user's
real Fused dir. Idempotent: the dir is created if missing, existing content is
left completely alone.
"""
import os


def fused_dir() -> str:
    """The user's Fused workspace: ~/Fused. FUSED_RENDER_DIR overrides
    it (tests set it so they never touch the real dir). Path only — no I/O.
    Normalized (expanduser + abspath) so a tilde or relative override yields the
    same path everywhere: onboarding and /api/config's fused_dir."""
    return os.path.abspath(
        os.path.expanduser(os.environ.get("FUSED_RENDER_DIR") or "~/Fused")
    )


def ensure_fused_dir() -> str:
    """Create ~/Fused if missing. Idempotent, non-destructive on
    upgrades. Returns the abs Fused dir."""
    fdir = os.path.abspath(fused_dir())
    os.makedirs(fdir, exist_ok=True)
    return fdir
