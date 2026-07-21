"""Entry point: `pythonw.exe -I -m fused_render.win_supervisor <args>` — port
of windows/supervisor/src/main.rs (feat/windows-desktop-foundation, PR #162).
"""
from __future__ import annotations

import ctypes
import os
import sys

from fused_render.win_supervisor import protocol, supervisor
from fused_render.win_supervisor.paths import DesktopPaths

_APP_USER_MODEL_ID = "Fused.FusedRender.Desktop"


def main() -> None:
    try:
        os.environ.update(DesktopPaths.discover().self_environment())
    except Exception:  # noqa: BLE001 - e.g. RuntimeError if LOCALAPPDATA is unset
        pass  # fall through with whatever env we were launched with

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
            # until someone dismisses it, and "could not start" is also the
            # wrong message for a teardown that failed on an already-running
            # app.
            MB_OK = 0x0
            MB_ICONERROR = 0x10
            ctypes.windll.user32.MessageBoxW(
                0, f"FusedRender could not start:\n\n{error}", "FusedRender", MB_OK | MB_ICONERROR
            )
        sys.exit(1)


if __name__ == "__main__":
    main()
