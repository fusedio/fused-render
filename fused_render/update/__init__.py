"""Self-update machinery shared by the desktop packages.

`common` holds the platform-neutral core (signed-manifest fetch/verify and
the hash-verified download) that the Windows supervisor updater
(supervisor/_win32/update.py) and the in-app updaters here (`mac`, `linux`)
all build on. `manager()`/`start()` below are the platform dispatch: callers
that just want "the running in-app update manager, whichever platform this
is" (the /api/config and /api/update routers) go through these instead of
importing `mac`/`linux` directly, so they don't need their own
sys.platform branch. Windows has no entry here — its updater lives entirely
in the supervisor (supervisor/_win32/update.py), not as an in-app manager.

`manager()` itself is not gated on `sys.platform`: it just reads back
whichever singleton `start()` already created, and `start()` — the only one
of the two that actually touches the OS (spawns the background check
thread, resolves a bundle/AppImage path) — IS strictly platform-gated, so in
any real single-platform process at most one of `mac`/`linux` ever has a
non-None singleton to find. `manager()` looks at both purely so it keeps
answering correctly for a caller that reached into `mac._manager` /
`linux._manager` directly (tests do this to install a fake manager without
going through the real `start()`), which a hard `sys.platform` branch here
would silently ignore whenever it disagreed with the real host platform.
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fused_render.update._manager import UpdateManager


def manager() -> "UpdateManager | None":
    """The process-wide in-app update manager, whichever platform module's
    singleton is actually set — None when none applies (Windows, or start()
    was never called) — /api/config omits `update` then. See the module
    docstring for why this checks both modules rather than sys.platform."""
    from fused_render.update import linux, mac

    if (found := mac.manager()) is not None:
        return found
    return linux.manager()


def start() -> "UpdateManager | None":
    """Create and start the singleton in-app update manager for this
    platform, if any. See mac.start()/linux.start() for the per-platform
    "nothing to swap" no-op rules."""
    if sys.platform == "darwin":
        from fused_render.update import mac
        return mac.start()
    if sys.platform.startswith("linux"):
        from fused_render.update import linux
        return linux.start()
    return None
