"""Entry point: `pythonw.exe -I -m fused_render.win_supervisor <args>` — port
of windows/supervisor/src/main.rs (feat/windows-desktop-foundation, PR #162).

The desktop env (branch opt-out, state/cache/log dirs) is applied at the very
top of this module body, before the `protocol`/`supervisor` import statement
runs — Python executes a module body strictly top-to-bottom, and an `import`
statement runs the imported module's own top-level code at that point, so
sequencing the env update above the import genuinely runs it first, not just
earlier on the page. fused_render._branch caches the first ref it resolves
for the process lifetime, so nothing from fused_render may load before the
inherited FUSED_RENDER_BRANCH is overridden. `paths` is a stdlib-only leaf
module (no fused_render imports of its own), safe to import before that.
"""
from __future__ import annotations

import os

from fused_render.win_supervisor.paths import DesktopPaths

try:
    os.environ.update(DesktopPaths.discover().self_environment())
except Exception:  # noqa: BLE001 - e.g. RuntimeError if LOCALAPPDATA is unset
    pass  # fall through with whatever env we were launched with

import ctypes  # noqa: E402
import sys  # noqa: E402

try:
    from fused_render.win_supervisor import protocol, supervisor  # noqa: E402
except Exception as import_error:  # noqa: BLE001 - e.g. "DLL load failed" from a
    # broken pywin32 after a bad upgrade. This runs under pythonw (no console)
    # and before main() exists, so a bare raise here is an invisible exit —
    # report it the same way main()'s fatal path does.
    try:
        DesktopPaths.discover().log(str(import_error))
    except Exception:  # noqa: BLE001 - logging is best-effort, never the point of failure
        pass
    MB_OK = 0x0
    MB_ICONERROR = 0x10
    ctypes.windll.user32.MessageBoxW(
        0,
        f"FusedRender could not start:\n\n{import_error}",
        "FusedRender",
        MB_OK | MB_ICONERROR,
    )
    sys.exit(1)

_APP_USER_MODEL_ID = "Fused.FusedRender.Desktop"


def main() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_APP_USER_MODEL_ID)
    except OSError:
        pass  # cosmetic (tray/taskbar identity) — never fatal

    command: protocol.Command | None = None
    try:
        command = protocol.parse_args(sys.argv[1:])
        supervisor.run(command)
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
            if isinstance(error, supervisor.SupervisorStoppedError):
                # The app DID start (server was ready, tray was up) and then
                # broke — "could not start" would misreport the failure mode.
                message = f"FusedRender stopped unexpectedly:\n\n{error}"
            else:
                message = f"FusedRender could not start:\n\n{error}"
            MB_OK = 0x0
            MB_ICONERROR = 0x10
            ctypes.windll.user32.MessageBoxW(0, message, "FusedRender", MB_OK | MB_ICONERROR)
        sys.exit(1)


if __name__ == "__main__":
    main()
