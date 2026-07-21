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
    """The native launcher stub, sibling to the `python\\` directory this
    supervisor runs out of (payload layout: `payload\\<launcher>.exe`,
    `payload\\python\\pythonw.exe`).

    Unlike the Rust version (where `env::current_exe()` *was* the launcher),
    `sys.executable` here is `pythonw.exe`, and the launcher's filename is not
    fixed — the installer names it per product build (e.g. `FusedRender.exe`
    for the shipping product, `FusedRenderPy.exe` for the experiment build so
    it can install side by side without colliding). So discover it rather than
    hardcode a name: there is exactly one `*.exe` in the payload root
    (installer.iss's [Files] places only the launcher there). The Run key
    must always name that launcher, never pythonw.exe directly, or the
    installer's uninstall sweep (which matches on the quoted launcher path)
    silently stops matching and leaves an orphaned Run entry (bugbot #6).

    Raises FileNotFoundError if discovery is ambiguous — no guessed fallback
    name, since any hardcoded name is wrong for at least one product build
    and a wrong guess would silently write a broken Run key instead of
    failing the way callers (tray.py's toggle handler) already expect.
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
