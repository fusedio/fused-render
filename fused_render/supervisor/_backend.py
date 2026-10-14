"""Per-OS backend seam for the desktop supervisor.

`core.py` and `__main__.py` reach the genuinely platform-specific pieces —
single-instance election + IPC, the supervised process tree (tree-kill on
exit), the autostart toggle, and the native UI/shell helpers — only through
this module. Exactly one backend is ever live per process, so this is a
*module namespace*, not an ABC: dispatch on `sys.platform`, import the matching
backend package, and re-export its names. A platform with no backend raises at
import with a clear message.

Adding an OS is a new `supervisor/<os>/` package plus one more branch here —
no changes to `core.py`.

Re-exported surface (each backend must provide all of it):
  Job            — supervised process tree; `.spawn(...)`, `.close()` (tree-kill)
  instance       — single-instance election + IPC: InstanceNames, acquire(),
                   PrimaryInstance/SecondaryInstance, Request, CommandRejected
  startup        — autostart toggle: enabled(), set_enabled(bool)
  ui             — native dialogs/shell: alert, confirm_exit,
                   report_open_rejected, pick_file, open_path, open_uri,
                   open_url
  SPAWN_ERRORS   — extra exception types `Job.spawn` may raise, beyond the
                   stdlib OSError/RuntimeError/TimeoutError the run loop handles

Optional hooks (a backend may omit them; core probes with getattr):
  integrate      — best-effort user-level desktop self-integration at startup
                   (Linux only: .desktop / MIME / icon + deep-link handler)
  deintegrate    — reverse of integrate(): remove that desktop integration
                   (Linux only, driven by the tray "Uninstall" item;
                   integration-only, never app data or the binary)
  windows        — native app windows (Linux only: a WebKitGTK window host
                   process; `WindowHost.start()/open(url)/stop()`; every
                   failure degrades to the browser, see _linux/windows.py)
"""
from __future__ import annotations

import sys

if sys.platform.startswith("linux"):
    from fused_render.supervisor._linux import instance, startup, ui, windows
    from fused_render.supervisor._linux.integration import deintegrate, integrate
    from fused_render.supervisor._linux.tree import Job

    # The Linux keeper spawns via stdlib primitives (subprocess / os), which
    # raise only OSError/RuntimeError/TimeoutError — all already handled by the
    # run loop — so there are no extra spawn exception types to declare here.
    SPAWN_ERRORS: tuple[type[BaseException], ...] = ()
else:
    raise RuntimeError(
        f"no desktop supervisor backend for {sys.platform!r} "
        "(supported: linux)"
    )

__all__ = ["Job", "SPAWN_ERRORS", "instance", "startup", "ui"]
