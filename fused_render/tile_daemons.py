"""Quit-time shutdown of the local tile-server daemons.

The geotiff / gridv2 / notebook daemons outlive the server process that spawned
them (they are shared across branches and sessions), so app quit asks each one
to exit through its token-gated `/quit` endpoint. The step is wired into `app.quit_teardown` as its own stage.

Everything is best-effort: an absent or corrupt state file and a dead port are
skipped silently, and the whole step is bounded by a join budget so a daemon
that never answers cannot stall the quit.
"""
import logging
import os
import threading
import urllib.request

from fused_render.shell import storage

logger = logging.getLogger(__name__)

DAEMON_STATE_FILES = (
    os.path.expanduser("~/.cache/fused-render-geotiff-v2/daemon.json"),
    os.path.expanduser("~/.cache/fused-render-gridv2/daemon.json"),
    # the notebook daemon nests under the app home (kernel.py _cache_dir)
    os.path.join(storage.home_dir(), "cache", "notebook-daemon", "daemon.json"),
)

# The step is sequential over DAEMON_STATE_FILES and each /quit carries a 3s
# timeout, so two wedged daemons cost 6s. Bounded as a whole rather than per
# daemon, so adding a daemon to DAEMON_STATE_FILES cannot silently stretch the
# quit deadline app.py derives from this number.
QUIT_TILE_DAEMONS_BUDGET_S = 2.0

# The relaunch-initiated quit (app.py's QUIT_FAST_*) gets a tighter bound.
QUIT_FAST_TILE_DAEMONS_BUDGET_S = 1.0


def quit_tile_daemons() -> None:
    """Best-effort /quit to every live tile-server daemon."""
    for state_file in DAEMON_STATE_FILES:
        state = storage.read_json(state_file)
        if not isinstance(state, dict) or not state.get("port"):
            continue
        # /quit is token-gated (D122); the state file carries the daemon's
        # token. Token-less state = a daemon predating the token, which
        # accepts a plain /quit.
        tok = state.get("token")
        path = f"/quit?t={tok}" if tok else "/quit"
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{state['port']}{path}", timeout=3).read()
        except OSError:
            continue


def quit_tile_daemons_bounded(budget_s: float = QUIT_TILE_DAEMONS_BUDGET_S) -> None:
    """Run `quit_tile_daemons` on its own daemon thread, joined against
    `budget_s`. Never raises."""
    def _run() -> None:
        try:
            quit_tile_daemons()
        except Exception:
            logger.warning("quit: quiescing the tile daemons failed",
                           exc_info=True)

    t = threading.Thread(target=_run, daemon=True, name="quit-tile-daemons")
    t.start()
    t.join(budget_s)
    if t.is_alive():
        logger.warning("quit: tile daemons did not all answer within %.1fs; "
                       "continuing", budget_s)
