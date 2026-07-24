"""Win32 native UI + shell helpers for the desktop supervisor backend.

Everything that pops a Win32 dialog (MessageBox, the Open-file common dialog)
or hands a path/URI/URL to the shell (`os.startfile`) lives here, isolated
behind the `_backend` seam so `core.py` stays platform-neutral.

Module-level imports are stdlib only (`ctypes`, `os`); the pywin32 pieces the
file dialog needs are imported lazily inside `pick_file`. That keeps `alert`
usable as the fatal-error reporter in `__main__.py` even when the reason for
the failure is a broken pywin32 install — the very thing that would make an
eager pywin32 import here fail too.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path

_MB_OK = 0x0
_MB_YESNO = 0x4
_MB_ICONERROR = 0x10
_MB_ICONQUESTION = 0x20
_MB_ICONWARNING = 0x30
_IDYES = 6


def alert(message: str, title: str = "FusedRender") -> None:
    """Modal error dialog. Used for fatal reporting where there is no console
    to print to (the supervisor runs under pythonw)."""
    ctypes.windll.user32.MessageBoxW(0, message, title, _MB_OK | _MB_ICONERROR)


def confirm_exit() -> bool:
    """Yes/No confirmation for the tray Exit action. True iff the user
    confirmed."""
    result = ctypes.windll.user32.MessageBoxW(
        0,
        "Stop FusedRender and all running render processes?",
        "Exit FusedRender",
        _MB_YESNO | _MB_ICONQUESTION,
    )
    return result == _IDYES


def report_open_rejected(path: str) -> None:
    """Warn that a forwarded open failed. The primary already logged the
    underlying reason; this is just accurate user-facing feedback, not a
    launch failure."""
    ctypes.windll.user32.MessageBoxW(
        0, f"FusedRender could not open:\n\n{path}", "FusedRender", _MB_OK | _MB_ICONWARNING
    )


def pick_file() -> str | None:
    """Show the Open-file common dialog; return the chosen path or None if the
    user cancelled. `GetOpenFileNameW` pumps its own message loop and needs an
    STA COM apartment for shell extensions — the caller
    (`core._spawn_file_dialog`) owns the dedicated thread and single-dialog
    lock this runs under, and CoInitialize/CoUninitialize bracket the call
    here."""
    import pythoncom
    import pywintypes
    import win32con
    import win32gui

    pythoncom.CoInitialize()
    try:
        path, _filter_index, _flags = win32gui.GetOpenFileNameW(
            Filter="All files\0*.*\0\0",
            Flags=win32con.OFN_FILEMUSTEXIST | win32con.OFN_PATHMUSTEXIST,
        )
    except pywintypes.error:
        return None
    finally:
        pythoncom.CoUninitialize()
    return path or None


def open_path(path: Path) -> None:
    os.startfile(str(path))  # noqa: S606 - local admin-installed path, not user input


def open_uri(uri: str) -> None:
    os.startfile(uri)


def open_url(url: str) -> None:
    os.startfile(url)


def open_default_apps() -> None:
    """Open the OS 'default apps' settings — the ms-settings page on Windows.
    Owned by the backend (not hardcoded in platform-neutral core) so each OS
    supplies its own honest behavior; Linux has no cross-desktop equivalent."""
    os.startfile("ms-settings:defaultapps")
