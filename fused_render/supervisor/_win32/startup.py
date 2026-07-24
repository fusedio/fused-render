"""Start-at-sign-in Run key toggle — port of windows/supervisor/src/startup.rs
(feat/windows-desktop-foundation, PR #162).
"""
from __future__ import annotations

import sys
import winreg
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "FusedRenderDesktop"


def launcher_exe() -> Path:
    """The native launcher stub, sibling to the `python\\` dir this supervisor
    runs from (payload layout: `payload\\<launcher>.exe`, `payload\\python\\
    pythonw.exe`). `sys.executable` is pythonw.exe and the launcher's name
    varies per product build, so discover it — there is exactly one `*.exe` in
    the payload root. The Run key must name that launcher, not pythonw.exe, or
    the uninstaller's quoted-path sweep stops matching and orphans the entry.

    Raises FileNotFoundError if discovery is ambiguous rather than guessing a
    name, since a wrong guess would silently write a broken Run key.
    """
    payload_dir = Path(sys.executable).resolve().parent.parent
    candidates = list(payload_dir.glob("*.exe"))
    if len(candidates) != 1:
        raise FileNotFoundError(
            f"could not identify the launcher exe in {payload_dir} "
            f"(found {[c.name for c in candidates]})"
        )
    return candidates[0]


def enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _VALUE_NAME)
        return True
    except FileNotFoundError:
        return False


def set_enabled(value: bool) -> None:
    if not value:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, _VALUE_NAME)
        except FileNotFoundError:
            pass
        return

    exe = launcher_exe()
    if not exe.is_file():
        raise FileNotFoundError(f"launcher not found: {exe}")
    value_data = f'"{exe}" --startup'
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, value_data)
