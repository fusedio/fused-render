"""Entry point: `pythonw.exe -I -m fused_render.supervisor <args>`.

The desktop env (branch opt-out, state/cache/log dirs) is applied at the very
top of this module body, before the `protocol`/`core` import statement runs —
Python executes a module body strictly top-to-bottom, and an `import` statement
runs the imported module's own top-level code at that point, so sequencing the
env update above the import genuinely runs it first, not just earlier on the
page. fused_render._branch caches the first ref it resolves for the process
lifetime, so nothing from fused_render may load before the inherited
FUSED_RENDER_BRANCH is overridden. `paths` is a stdlib-only leaf module (no
fused_render imports of its own), safe to import before that.
"""
from __future__ import annotations

import os

from fused_render.supervisor.paths import DesktopPaths

try:
    os.environ.update(DesktopPaths.discover().self_environment())
except Exception:  # noqa: BLE001 - e.g. RuntimeError if LOCALAPPDATA is unset
    pass  # fall through with whatever env we were launched with

import sys  # noqa: E402

# `ui` is stdlib-only at import time on every backend (Win32's pywin32 pieces
# load lazily; Linux's dialog tools are exec'd), so it stays importable — and
# `ui.alert` usable — even when the full backend import below fails (e.g. a
# broken pywin32 after a bad upgrade). Import it straight from the OS package,
# not via `_backend`, whose Job/instance imports are the fragile ones.
if sys.platform == "win32":
    from fused_render.supervisor._win32 import ui  # noqa: E402
elif sys.platform.startswith("linux"):
    from fused_render.supervisor._linux import ui  # noqa: E402
else:
    from fused_render.supervisor import _backend  # noqa: E402 - raises with a clear message

    ui = _backend.ui

try:
    from fused_render.supervisor import core, protocol  # noqa: E402
except Exception as import_error:  # noqa: BLE001 - e.g. "DLL load failed" from a
    # broken pywin32 after a bad upgrade. This runs windowless (no console) and
    # before main() exists, so a bare raise here is an invisible exit — report
    # it the same way main()'s fatal path does.
    try:
        DesktopPaths.discover().log(str(import_error))
    except Exception:  # noqa: BLE001 - logging is best-effort, never the point of failure
        pass
    ui.alert(f"FusedRender could not start:\n\n{import_error}")
    sys.exit(1)

_APP_USER_MODEL_ID = "Fused.FusedRender.Desktop"


def main() -> None:
    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_APP_USER_MODEL_ID)
        except OSError:
            pass  # cosmetic (tray/taskbar identity) — never fatal

    command: protocol.Command | None = None
    try:
        command = protocol.parse_args(sys.argv[1:])
        core.run(command)
    except Exception as error:  # noqa: BLE001 - top-level: report, never crash silently
        try:
            DesktopPaths.discover().log(str(error))
        except Exception:  # noqa: BLE001 - logging is best-effort, never the point of failure
            pass
        if not isinstance(command, protocol.ShutdownForUpgrade):
            # The installer execs --shutdown-for-upgrade with
            # ewWaitUntilTerminated and reports failure itself from the exit
            # code — a blocking dialog here would stall a silent upgrade
            # until someone dismisses it.
            if isinstance(error, core.SupervisorStoppedError):
                # The app DID start (server was ready, tray was up) and then
                # broke — "could not start" would misreport the failure mode.
                message = f"FusedRender stopped unexpectedly:\n\n{error}"
            else:
                message = f"FusedRender could not start:\n\n{error}"
            ui.alert(message)
        sys.exit(1)


if __name__ == "__main__":
    main()
