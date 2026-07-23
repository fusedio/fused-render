"""User-level desktop self-integration — the Linux counterpart to the Windows
installer's HKCU associations and the macOS bundle's LaunchServices metadata.

An AppImage is a single relocatable file with no install step, so nothing has
registered its `.desktop` entry, MIME types, or icon with the desktop
environment. Historically that made "Open with FusedRender" and
`fused-render://` deep links work only *after* a third-party AppImage integrator
(appimaged, Gear Lever, …) did it. This module removes that dependency: at the
first packaged supervisor start it writes those files into the user's XDG data
dirs itself and pokes the freedesktop databases, so both features work out of
the box (SPEC gate (d)).

Discipline, mirroring startup.py:
  * Only acts from a real AppImage (`$APPIMAGE` points at an existing file). A
    dev `python -m …` run is a silent no-op.
  * Every step is best-effort and log-and-continue — a missing `update-*` tool,
    an unwritable dir, anything — because integration must NEVER stop the app
    from starting (it runs off the startup path, on a daemon thread).
  * Idempotent via a stamp (app version + AppImage path + content hash) under
    the supervisor state dir: work happens only on first install and when the
    AppImage moved or the association set / version changed.
  * File types deliberately get NO `xdg-mime default` — only the deep-link
    scheme does (a scheme needs a default to work at all). Never steal the
    user's file defaults: the macOS "Alternate rank" parity carried through to
    Linux.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from fused_render import __version__
from fused_render._mime_package import custom_mime_xml, desktop_mime_types
from fused_render.supervisor._linux import startup
from fused_render.supervisor.paths import _xdg_home

_DESKTOP_NAME = "fused-render.desktop"
_MIME_PACKAGE_NAME = "fused-render.xml"
_ICON_STEM = "fused-render"
_ICON_NAME = f"{_ICON_STEM}.png"
_STAMP_NAME = "desktop-integration.json"
_SCHEME_HANDLER_TYPE = "x-scheme-handler/fused-render"
_TOOL_TIMEOUT_S = 30

_ENTRY_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=FusedRender
Comment=FusedRender desktop
Exec={exec_line}
Icon={icon}
Categories=Development;Science;
Terminal=false
MimeType={mimetype}
"""


def integrate(
    paths,
    *,
    appimage: Path | None = None,
    icon_source: Path | None = None,
) -> None:
    """Self-integrate this AppImage into the user's desktop, idempotently.

    `paths` is the supervisor's DesktopPaths (its state dir holds the stamp).
    `appimage`/`icon_source` are injectable for tests; in production they
    resolve from `$APPIMAGE` and `$APPDIR`. No-op when unpackaged.
    """
    if appimage is None:
        appimage = startup.appimage_path()
    if appimage is None:
        return  # dev / unpackaged: silent no-op

    data_home = _xdg_home("XDG_DATA_HOME", ".local/share")
    desktop_file = data_home / "applications" / _DESKTOP_NAME
    mime_file = data_home / "mime" / "packages" / _MIME_PACKAGE_NAME
    icon_file = data_home / "icons" / "hicolor" / "256x256" / "apps" / _ICON_NAME

    if icon_source is None:
        icon_source = _bundled_icon_source()
    icon_available = icon_source is not None and icon_source.is_file()
    # Absolute path to the icon we install, so the entry resolves even before
    # the icon-theme cache is rebuilt; fall back to the theme name if we have no
    # source to copy.
    icon_value = str(icon_file) if icon_available else _ICON_STEM

    desktop_text = _desktop_entry(appimage, icon_value)
    mime_text = custom_mime_xml()

    stamp_file = paths.state / _STAMP_NAME
    stamp = _stamp(appimage, desktop_text, mime_text)
    if _read_stamp(stamp_file) == stamp:
        return  # AppImage, version, and association set all unchanged

    try:
        _write(desktop_file, desktop_text)
        _write(mime_file, mime_text)
        if icon_available:
            _install_icon(icon_source, icon_file)  # type: ignore[arg-type]
    except OSError as error:  # noqa: BLE001 handled below — never fatal
        paths.log(f"desktop integration: writing files failed: {error}")
        return

    # Best-effort database refreshes; absent tools are fine.
    _run_tool(["update-mime-database", str(data_home / "mime")], paths)
    _run_tool(["update-desktop-database", str(data_home / "applications")], paths)
    # Only the scheme gets a default (schemes need one to route at all); file
    # types get none — never steal the user's defaults.
    _run_tool(["xdg-mime", "default", _DESKTOP_NAME, _SCHEME_HANDLER_TYPE], paths)

    try:
        _write(stamp_file, json.dumps(stamp))
    except OSError as error:  # noqa: BLE001 stamp is an optimization, not correctness
        paths.log(f"desktop integration: could not write stamp: {error}")


def _desktop_entry(appimage: Path, icon_value: str) -> str:
    exec_line = f"{shlex.quote(str(appimage))} %u"
    mimetype = ";".join(desktop_mime_types()) + ";"
    return _ENTRY_TEMPLATE.format(exec_line=exec_line, icon=icon_value, mimetype=mimetype)


def _bundled_icon_source() -> Path | None:
    """The icon staged inside the mounted AppImage ($APPDIR), if present."""
    appdir = os.environ.get("APPDIR")
    if not appdir:
        return None
    base = Path(appdir)
    for candidate in (
        base / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps" / _ICON_NAME,
        base / _ICON_NAME,
    ):
        if candidate.is_file():
            return candidate
    return None


def _stamp(appimage: Path, desktop_text: str, mime_text: str) -> dict:
    digest = hashlib.sha256((desktop_text + "\0" + mime_text).encode("utf-8")).hexdigest()
    return {"version": __version__, "appimage": str(appimage), "hash": digest}


def _read_stamp(stamp_file: Path) -> dict | None:
    try:
        return json.loads(stamp_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _install_icon(icon_source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(icon_source, dest)


def _run_tool(argv: list[str], paths) -> None:
    """Run a desktop-database refresh tool if present; log-and-continue on any
    failure. An absent tool (a minimal desktop) is not an error."""
    tool = argv[0]
    if shutil.which(tool) is None:
        paths.log(f"desktop integration: {tool} not found, skipping")
        return
    try:
        subprocess.run(argv, capture_output=True, timeout=_TOOL_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as error:  # noqa: BLE001 best-effort
        paths.log(f"desktop integration: {tool} failed: {error}")
