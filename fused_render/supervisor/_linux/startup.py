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
import shlex
import sys
from pathlib import Path

_DESKTOP_FILE_NAME = "fused-render.desktop"

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


def _launcher_path() -> Path:
    """The runnable AppImage/launcher to name in Exec. Raises if it can't be
    resolved to an existing file, so callers never write a broken entry."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        path = Path(appimage)
        if not path.is_file():
            raise FileNotFoundError(f"$APPIMAGE points at a missing file: {path}")
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


def set_enabled(value: bool) -> None:
    desktop = _desktop_file()
    if not value:
        try:
            desktop.unlink()
        except FileNotFoundError:
            pass
        return

    launcher = _launcher_path()  # raises before any write if unresolvable
    exec_line = f"{shlex.quote(str(launcher))} --startup"
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text(_ENTRY_TEMPLATE.format(exec_line=exec_line), encoding="utf-8")
