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


def confirm_uninstall() -> bool:
    """Yes/No confirmation for the tray Uninstall action. True iff the user
    confirmed. Part of the uniform `ui` surface core.py depends on (like
    open_default_apps); the Windows tray builds no Uninstall item — Windows
    uninstalls through the installer — so this is never reached at runtime."""
    result = ctypes.windll.user32.MessageBoxW(
        0,
        "Remove FusedRender's desktop integration and quit?\n\n"
        "This does not delete your data or the app file.",
        "Uninstall FusedRender",
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


# HRESULT_FROM_WIN32(ERROR_CANCELLED) — what IFileDialog::Show returns when the
# user closes the dialog. The one failure that is not a failure.
_ERROR_CANCELLED = -2147023673  # 0x800704C7 as a signed 32-bit HRESULT


def pick_directory(title: str = "Choose a folder", start: str | None = None) -> str | None:
    """Show the folder chooser; return the chosen path, or None if the user
    cancelled. Raises OSError when the dialog itself broke.

    `IFileDialog` with `FOS_PICKFOLDERS`, not the `GetOpenFileNameW` that
    `pick_file` above uses: the old common dialog has no folder mode at all, and
    `SHBrowseForFolder` (the other option) is the cramped tree-only dialog with
    no address bar, no favourites and no search. This is the same chooser
    Explorer shows.

    Same STA discipline as `pick_file`: the dialog pumps its own message loop and
    shell extensions need a single-threaded apartment, so CoInitialize brackets
    the call and the caller owns the dedicated thread and the single-dialog lock
    (`server/dirpicker._pick_win32`, mirroring `supervisor/core.py`).
    """
    import pythoncom
    import pywintypes
    from win32com.shell import shell, shellcon

    pythoncom.CoInitialize()
    try:
        dialog = pythoncom.CoCreateInstance(
            shell.CLSID_FileOpenDialog, None,
            pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IFileOpenDialog)
        # FORCEFILESYSTEM as well as PICKFOLDERS: without it the dialog will
        # happily return a virtual shell folder (a library, "This PC") that has
        # no filesystem path, and GetDisplayName then fails instead of the
        # dialog refusing the choice up front.
        dialog.SetOptions(dialog.GetOptions()
                          | shellcon.FOS_PICKFOLDERS
                          | shellcon.FOS_FORCEFILESYSTEM
                          | shellcon.FOS_PATHMUSTEXIST)
        dialog.SetTitle(title)
        if start:
            # A starting folder is a nicety, never a reason to fail: an
            # unreadable or deleted path just leaves the dialog where it was.
            try:
                dialog.SetFolder(shell.SHCreateItemFromParsingName(
                    start, None, shell.IID_IShellItem))
            except pywintypes.error:
                pass
        try:
            dialog.Show(0)
        except pywintypes.error as exc:
            if exc.args and exc.args[0] == _ERROR_CANCELLED:
                return None
            raise OSError(f"the folder chooser failed: {exc}") from exc
        item = dialog.GetResult()
        return item.GetDisplayName(shellcon.SIGDN_FILESYSPATH) or None
    except OSError:
        raise
    except pywintypes.error as exc:
        raise OSError(f"the folder chooser failed: {exc}") from exc
    finally:
        pythoncom.CoUninitialize()


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
