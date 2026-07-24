"""Start-at-login toggle — the Linux counterpart to `_win32/startup.py`.

Writes/removes a freedesktop autostart entry at
`$XDG_CONFIG_HOME/autostart/fused-render.desktop` (default `~/.config`). The
`Exec=` line points at the running AppImage — `$APPIMAGE` when packaged (the
AppImage runtime exports it for the launched process and its children),
otherwise a resolved `sys.argv[0]`.

Mirrors the Windows launcher-discovery caution: fail loudly (raise) rather than
write a broken Exec line. The tray toggle handler already treats an OSError as
"revert the checkbox and log" (see tray.py's `_on_toggle_login`), so a raise
here degrades cleanly to "the setting didn't change", never a corrupt entry.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_DESKTOP_FILE_NAME = "fused-render.desktop"

# freedesktop Desktop Entry Spec reserved characters for the Exec key. An
# argument containing any of these must be enclosed in DOUBLE quotes — the spec
# does NOT recognize shell-style single-quote (shlex) quoting, so `shlex.quote`
# produces Exec lines many launchers reject.
_EXEC_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def _exec_quote(path: str) -> str:
    """Quote a path for a `.desktop` `Exec=` field per the freedesktop Desktop
    Entry Spec (NOT shell/`shlex` quoting).

    Returns the path unquoted when it holds no reserved character; otherwise
    double-quotes it. Two escaping layers compose: the quoting layer prefixes a
    backslash to `"`, `` ` ``, `$` and `\\` inside the quotes, then the general
    string-escape layer doubles every backslash. So a literal backslash becomes
    four backslashes and a literal `$` becomes `\\$` in the file — exactly the
    examples the spec calls out."""
    if not any(c in _EXEC_RESERVED for c in path):
        return path
    quoted = path
    for ch in ("\\", '"', "`", "$"):  # quoting layer (backslash first)
        quoted = quoted.replace(ch, "\\" + ch)
    quoted = quoted.replace("\\", "\\\\")  # string layer: double all backslashes
    return '"' + quoted + '"'

_ENTRY_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=FusedRender
Comment=FusedRender desktop
Exec={exec_line}
Terminal=false
X-GNOME-Autostart-enabled=true
"""


def _autostart_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home and os.path.isabs(config_home) else Path.home() / ".config"
    return base / "autostart"


def _desktop_file() -> Path:
    return _autostart_dir() / _DESKTOP_FILE_NAME


def appimage_path() -> Path | None:
    """The AppImage this process runs from, or None when unpackaged.

    `$APPIMAGE` set and pointing at an existing file (the AppImage runtime
    exports it for the launched process and its children); otherwise None — a
    dev `python -m …` run, or a stale/missing `$APPIMAGE`. Shared with
    integration.py, which self-integrates ONLY from a real AppImage and treats
    None as a silent no-op (never a raise), so this deliberately reports the
    missing-file case as None rather than raising."""
    appimage = os.environ.get("APPIMAGE")
    if not appimage:
        return None
    path = Path(appimage)
    return path if path.is_file() else None


def _launcher_path() -> Path:
    """The runnable AppImage/launcher to name in Exec. Raises if it can't be
    resolved to an existing file, so callers never write a broken entry."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        path = appimage_path()
        if path is None:
            raise FileNotFoundError(f"$APPIMAGE points at a missing file: {appimage}")
        return path
    # Dev / unpackaged fallback: whatever launched us — but only if the desktop
    # session could actually exec it. Under `python -m fused_render.supervisor`
    # argv[0] is the package's __main__.py (a plain, non-executable script), and
    # persisting that as Exec= is exactly the broken entry this module promises
    # never to write.
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0:
        raise FileNotFoundError("cannot resolve a launcher path (no $APPIMAGE, no argv[0])")
    resolved = Path(argv0).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"resolved launcher is not a file: {resolved}")
    if resolved.suffix == ".py" or not os.access(resolved, os.X_OK):
        raise FileNotFoundError(
            f"launcher is not directly executable by the desktop session: {resolved}"
        )
    return resolved


def enabled() -> bool:
    return _desktop_file().is_file()


def _entry_text(launcher: Path) -> str:
    """The autostart `.desktop` contents for `launcher` — the single source of
    truth shared by `set_enabled(True)` (which writes it) and
    `refresh_autostart` (which compares against it), so the two never drift."""
    exec_line = f"{_exec_quote(str(launcher))} --startup"
    return _ENTRY_TEMPLATE.format(exec_line=exec_line)


def set_enabled(value: bool) -> None:
    desktop = _desktop_file()
    if not value:
        try:
            desktop.unlink()
        except FileNotFoundError:
            pass
        return

    launcher = _launcher_path()  # raises before any write if unresolvable
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text(_entry_text(launcher), encoding="utf-8")


def refresh_autostart() -> None:
    """Self-heal the autostart entry after the AppImage moved. If autostart is
    enabled and the launcher path is resolvable, rewrite the entry only when its
    current contents differ from what set_enabled(True) would write now. No-op
    when disabled or when the launcher can't be resolved (dev/unpackaged).

    The parallel "Open with"/deep-link `.desktop` is already re-healed on every
    packaged start by integration.integrate() (its stamp includes the resolved
    AppImage path), but the autostart entry set_enabled(True) writes had no such
    healing — after a move, login-autostart pointed at a dead path forever. This
    closes that gap while never writing a broken entry (the module's discipline):
    an unresolvable launcher is left strictly as-is."""
    if not enabled():
        return
    try:
        launcher = _launcher_path()
    except FileNotFoundError:
        return  # unresolvable (dev/unpackaged, or $APPIMAGE now missing): leave as-is
    desired = _entry_text(launcher)
    desktop = _desktop_file()
    try:
        current = desktop.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = None
    if current == desired:
        return  # already correct: no needless rewrite / mtime churn
    set_enabled(True)
