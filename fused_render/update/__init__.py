"""Self-update machinery shared by the desktop packages.

`common` holds the platform-neutral core (signed-manifest fetch/verify and
the hash-verified download) that the in-app updaters here (`mac`, `linux`)
both build on. `manager()`/`start()` below are the platform dispatch: callers
that just want "the running in-app update manager, whichever platform this
is" (the /api/config and /api/update routers) go through these instead of
importing `mac`/`linux` directly, so they don't need their own
sys.platform branch.

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


# Set by `fused-render serve` (cli.py) — NOT by create_app(), so tests and
# embedded servers never reach PyPI. See update/pypi.py.
_cli_check = False


def enable_cli_check() -> None:
    """Opt this process into the PyPI update notice (check-only), used when no
    packaged updater applies."""
    global _cli_check
    _cli_check = True


def manager() -> "UpdateManager | None":
    """The process-wide in-app update manager, whichever platform module's
    singleton is actually set — None when none applies (an unsupported
    platform, or start() was never called) — /api/config omits `update`
    then. See the module docstring for why this checks both modules rather
    than sys.platform. The CLI's PyPI manager is the last resort."""
    from fused_render.update import linux, mac, pypi

    if (found := mac.manager()) is not None:
        return found
    if (found := linux.manager()) is not None:
        return found
    return pypi.manager()


def supported() -> bool:
    """Whether `start()` would create a manager here: the same predicate as the
    per-platform `start()`s, answered without starting (or importing the
    platform modules' singletons). False on Windows and other platforms, on a
    lean install without `cryptography`, and in an unpackaged run (no .app
    bundle / AppImage) unless the check-only dev manager was asked for. Lets a
    caller tell "never started" from "cannot run here"."""
    import os

    from fused_render.update import common
    from fused_render.update._manager import DEV_MANAGER_ENV

    if sys.platform == "darwin":
        from fused_render.update import mac
        packaged = mac.bundle_path() is not None
    elif sys.platform.startswith("linux"):
        from fused_render.supervisor._linux import startup
        packaged = startup.appimage_path() is not None
    else:
        return False
    if not common.CRYPTO_AVAILABLE:
        return False
    return packaged or bool(os.environ.get(DEV_MANAGER_ENV))


def start() -> "UpdateManager | None":
    """Create and start the singleton in-app update manager for this
    platform, if any. See mac.start()/linux.start() for the per-platform
    "nothing to swap" no-op rules. When none applies and the CLI opted in
    (enable_cli_check), fall back to the check-only PyPI manager."""
    started = None
    if sys.platform == "darwin":
        from fused_render.update import mac
        started = mac.start()
    elif sys.platform.startswith("linux"):
        from fused_render.update import linux
        started = linux.start()
    if started is None and _cli_check:
        from fused_render.update import pypi
        return pypi.start()
    return started
