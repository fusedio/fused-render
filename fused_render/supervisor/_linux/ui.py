"""Linux native UI + shell helpers — the counterpart to `_win32/ui.py`.

Dialogs go through the first available of `zenity`, `kdialog`, then a bundled
tkinter fallback (tcl/tk ships in the python-build-standalone runtime, the same
precedent as the Windows file-dialog note). Shell opens (`open_path`,
`open_uri`, `open_url`) shell out to `xdg-open`, the same pattern the server
uses to reveal a path in the file manager (server.py's reveal handler).

Module-level imports are stdlib only, so `alert` stays usable as the fatal-error
reporter in `__main__.py` even if a dialog tool is missing (it degrades to the
tkinter fallback, then to a best-effort no-op).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_TITLE = "FusedRender"
_DIALOG_TIMEOUT_S = 300  # a modal dialog a user never answers must not hang forever
_XDG_OPEN_WAIT_S = 5  # long enough for a failing handler to exit before we call it a success


def _dialog_tool() -> str:
    """The dialog backend to use: the first available of zenity, kdialog, then
    the always-present tkinter fallback. This resolution is the only unit-tested
    part of this module (subprocess-stubbed); the dialogs are gate-tested
    manually."""
    if shutil.which("zenity"):
        return "zenity"
    if shutil.which("kdialog"):
        return "kdialog"
    return "tkinter"


def _run(argv: list[str]) -> "subprocess.CompletedProcess | None":
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=_DIALOG_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None


def alert(message: str, title: str = _TITLE) -> None:
    tool = _dialog_tool()
    if tool == "zenity":
        _run(["zenity", "--error", "--title", title, "--text", message])
    elif tool == "kdialog":
        _run(["kdialog", "--title", title, "--error", message])
    else:
        _tk_message("showerror", title, message)


def confirm_exit() -> bool:
    message = "Stop FusedRender and all running render processes?"
    title = "Exit FusedRender"
    tool = _dialog_tool()
    if tool == "zenity":
        result = _run(["zenity", "--question", "--title", title, "--text", message])
        return result is not None and result.returncode == 0
    if tool == "kdialog":
        result = _run(["kdialog", "--title", title, "--yesno", message])
        return result is not None and result.returncode == 0
    return bool(_tk_message("askyesno", title, message))


def report_open_rejected(path: str) -> None:
    alert(f"FusedRender could not open:\n\n{path}")


def pick_file() -> str | None:
    tool = _dialog_tool()
    if tool == "zenity":
        result = _run(["zenity", "--file-selection", "--title", "Open file"])
    elif tool == "kdialog":
        result = _run(["kdialog", "--title", "Open file", "--getopenfilename"])
    else:
        return _tk_pick_file()
    if result is None or result.returncode != 0:
        return None
    chosen = result.stdout.strip()
    return chosen or None


def open_path(path: Path) -> None:
    _xdg_open(str(path))


def open_uri(uri: str) -> None:
    _xdg_open(uri)


def open_url(url: str) -> None:
    _xdg_open(url)


def open_default_apps() -> None:
    """No cross-desktop Linux equivalent of the Windows ms-settings:defaultapps
    page, so raise: core's _safe_call logs an honest no-op
    (docs/LINUX_DESKTOP_SPEC.md). Kept as a real backend method so core stays
    platform-neutral and never hardcodes a Windows-only URI."""
    raise OSError("default-apps settings has no cross-desktop Linux equivalent")


def _xdg_open(target: str) -> None:
    # Detached like server.py's reveal handler, but the exit code is checked:
    # a failed open must raise OSError so core's _safe_open answers status 1
    # (rejection dialog) instead of silently reporting success — the regression
    # against os.startfile. A handler that stays in the foreground never exits
    # within the wait window, so a timeout is treated as success (the open is
    # underway). Raises OSError if xdg-open is absent (FileNotFoundError), which
    # core's _safe_call / _safe_open already log-and-ignore.
    proc = subprocess.Popen(
        ["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        returncode = proc.wait(timeout=_XDG_OPEN_WAIT_S)
    except subprocess.TimeoutExpired:
        return
    if returncode != 0:
        raise OSError(f"xdg-open exited with status {returncode} for {target}")


def _tk_message(kind: str, title: str, message: str):
    # The whole thing (import AND Tk()) is guarded: on a display-less session
    # Tk() raises TclError, and alert() is the unguarded fatal-error reporter in
    # __main__.py — it must degrade to a best-effort no-op, never crash.
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        try:
            return getattr(messagebox, kind)(title, message)
        finally:
            root.destroy()
    except Exception:  # noqa: BLE001 - tk missing / no display: best-effort no-op
        return None


def _tk_pick_file() -> str | None:
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        try:
            chosen = filedialog.askopenfilename(title="Open file")
        finally:
            root.destroy()
        return chosen or None
    except Exception:  # noqa: BLE001 - tk missing / no display: best-effort no-op
        return None
