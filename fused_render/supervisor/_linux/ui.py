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


def _xdg_open(target: str) -> None:
    # Detached like server.py's reveal handler; raises OSError if xdg-open is
    # absent, which core's _safe_call / _safe_open already log-and-ignore.
    subprocess.Popen(
        ["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _tk_message(kind: str, title: str, message: str):
    try:
        import tkinter
        from tkinter import messagebox
    except Exception:  # noqa: BLE001 - tkinter genuinely absent: best-effort no-op
        return None
    root = tkinter.Tk()
    root.withdraw()
    try:
        return getattr(messagebox, kind)(title, message)
    finally:
        root.destroy()


def _tk_pick_file() -> str | None:
    try:
        import tkinter
        from tkinter import filedialog
    except Exception:  # noqa: BLE001
        return None
    root = tkinter.Tk()
    root.withdraw()
    try:
        chosen = filedialog.askopenfilename(title="Open file")
    finally:
        root.destroy()
    return chosen or None
