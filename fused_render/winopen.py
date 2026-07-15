"""Windows Explorer "Open with" entry point.

Invoked as `fused-render-open [--port N] [FILE]` (registered into HKCU by
scripts/windows/register_open_with.ps1): reuses a live server or starts one
detached, then opens the browser at FILE's /view URL (or / with no FILE).
`--register`/`--unregister` write/remove the HKCU associations themselves.
"""
import argparse
import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
import sysconfig
import time
import urllib.error
import urllib.request
import webbrowser
from urllib.parse import quote

from fused_render._branch import branch_port, branch_ref
from fused_render.logs import setup_logging

logger = logging.getLogger("fused_render")

_LEGACY_PROGID = "FusedRender.file"  # single-ProgID scheme; cleaned up on (re)register
_ICON_PATH = os.path.join(os.path.dirname(__file__), "assets", "fused-render.ico")
_FILE_ICONS_DIR = os.path.join(os.path.dirname(__file__), "assets", "file_icons")
_REGISTRY_JSON = os.path.join(os.path.dirname(__file__), "templates", "registry.json")
_NOT_EXTENSIONS = {".zgroup", ".zattrs", ".zmetadata"}  # zarr member files, not extensions

# Extension token (after the last dot) -> category icon, mirroring EXT_VARIANT
# in frontend/src/components/FileIcons.tsx so a file's Explorer icon matches the
# glyph fused-render's own listing shows for it. Keep the two in sync; the .ico
# files come from scripts/windows/gen_file_icons.py. Anything unmapped falls
# back to the plain "file" icon.
_ICON_VARIANT_FOR_TOKEN = {
    # code / config / shell / style / markup
    **dict.fromkeys(
        "py js ts tsx jsx cjs mjs cts mts sh zsh fish ps1 csh zsh-theme vim "
        "yaml yml toml ini cfg conf tf hcl css plist twb tds".split(),
        "code",
    ),
    # tabular / gridded data
    **dict.fromkeys(
        "parquet csv tsv xlsx xlsm nc nc4 cdf zgroup zattrs zmetadata hyper".split(),
        "data",
    ),
    # structured text
    **dict.fromkeys("json jsonl ndjson".split(), "json"),
    # web
    **dict.fromkeys("html htm".split(), "html"),
    # images
    **dict.fromkeys(
        "png jpg jpeg gif webp svg bmp avif heic heif dng".split(), "image"
    ),
    # documents / prose
    **dict.fromkeys(
        "pdf md markdown txt log docx pptx tex ltx latex".split(), "doc"
    ),
    # audio / video
    **dict.fromkeys("mp4 mov m4v webm mp3 wav m4a ogg flac".split(), "media"),
    # geospatial / vector
    **dict.fromkeys(
        "geojson shp kml kmz gpx gpkg fgb pmtiles tif tiff las laz".split(), "geo"
    ),
    # archives
    **dict.fromkeys(
        "zip jar whl egg tar tgz tbz2 txz gz bz2 xz zst twbx tdsx".split(), "archive"
    ),
    # databases
    **dict.fromkeys("sqlite sqlite3 db duckdb ddb".split(), "db"),
    # 3D models / point clouds
    **dict.fromkeys(
        "usd usda usdc usdz ply splat ksplat obj stl glb gltf".split(), "model"
    ),
}

_LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
_APP_SUPPORT_BASE = os.path.join(_LOCALAPPDATA, "fused-render")
APP_SUPPORT_DIR = (
    _APP_SUPPORT_BASE if not branch_ref() else os.path.join(_APP_SUPPORT_BASE, branch_ref())
)
PIDFILE = os.path.join(APP_SUPPORT_DIR, "server.pid")
PORTFILE = os.path.join(APP_SUPPORT_DIR, "server.port")
SERVER_LOG = os.path.join(APP_SUPPORT_DIR, "server.out.log")

DEFAULT_PORT = branch_port()
MAX_PORT = DEFAULT_PORT + 10


def _read_int(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _fused_server(port: int) -> bool:
    """True when a native fused-render answers on port: /api/config parses and
    its home has a drive letter (a WSL server mirrored onto localhost has none)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=1) as resp:
            if resp.status != 200:
                return False
            cfg = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return isinstance(cfg, dict) and bool(os.path.splitdrive(cfg.get("home", ""))[0])


def _settle(port: int) -> bool:
    """Like _fused_server, but a bound-yet-silent port — usually a peer
    opener's server still booting — gets the spawn grace period."""
    if _fused_server(port):
        return True
    if not _port_in_use(port):
        return False
    return _wait_until_ready(port)


def find_running_server() -> int | None:
    """Port of a live native instance, or None. Probes by HTTP, never by pid
    (os.kill(pid, 0) kills on Windows); a manual `fused-render serve` writes
    no portfile, so the default range is scanned too."""
    filed = _read_int(PORTFILE)
    if filed is not None and _settle(filed):
        return filed
    for port in range(DEFAULT_PORT, MAX_PORT + 1):
        if port != filed and _fused_server(port):
            return port
    return None


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def pick_port(start: int = DEFAULT_PORT, end: int = MAX_PORT) -> int:
    for port in range(start, end + 1):
        if not _port_in_use(port):
            return port
    raise RuntimeError(f"no free port between {start} and {end}; is something hogging the whole range?")


def _wait_until_ready(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _fused_server(port):
            return True
        time.sleep(0.2)
    return False


def _write_pidfile(pid: int, port: int) -> None:
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    with open(PIDFILE, "w", encoding="utf-8") as f:
        f.write(str(pid))
    with open(PORTFILE, "w", encoding="utf-8") as f:
        f.write(str(port))


def _remove_pidfile() -> None:
    for path in (PIDFILE, PORTFILE):
        try:
            os.remove(path)
        except OSError:
            pass


def _spawn(port: int) -> int:
    """Start the server detached and wait for it. stdout/stderr go to
    server.out.log — a windowless parent has None streams and cli.py prints."""
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    log_file = open(SERVER_LOG, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "fused_render.cli", "serve", "--no-browser", "--port", str(port)],
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        creationflags=(
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
        ),
    )
    log_file.close()
    logger.info("starting server (pid %s, port %s)", proc.pid, port)
    # pidfile precedes readiness so racing peers _settle instead of double-spawning
    _write_pidfile(proc.pid, port)
    if _wait_until_ready(port):
        return port
    proc.kill()
    _remove_pidfile()
    raise RuntimeError(f"server did not start on port {port}; see log: {SERVER_LOG}")


def _ensure_server(requested: int | None) -> int:
    """Return the port of a ready native server, starting one when needed."""
    if requested is not None:
        if _settle(requested):
            return requested
        if _port_in_use(requested):
            raise RuntimeError(f"port {requested} is in use by another application")
        return _spawn(requested)

    port = find_running_server()
    if port is not None:
        return port
    port = pick_port()
    if _fused_server(port):  # a racing double-click may have spawned here first
        return port
    return _spawn(port)


def _view_url(port: int, path: str | None) -> str:
    """Build the /view URL the frontend codec (router.ts urlForFsPath) decodes:
    forward slashes, each segment percent-encoded on its own ("C:" -> "C%3A")."""
    if not path:
        return f"http://127.0.0.1:{port}/"
    fs_path = os.path.abspath(path).replace("\\", "/")
    segments = [quote(seg, safe="") for seg in fs_path.split("/") if seg]
    return f"http://127.0.0.1:{port}/view/" + "/".join(segments)


def _report(msg: str) -> None:
    # sys.stdout is None under the gui-scripts launcher; use a message box there.
    if sys.stdout is not None:
        print(msg)
    else:
        ctypes.windll.user32.MessageBoxW(0, msg, "fused-render", 0x40)


def extensions() -> list[str]:
    with open(_REGISTRY_JSON, encoding="utf-8") as f:
        registry = json.load(f)
    # skip directory sentinels ("/", ".zarr/"), zarr member files, and glob
    # patterns like ".*.json" (not real extensions Explorer can register)
    return sorted(
        ext
        for ext in registry
        if ext.startswith(".") and "/" not in ext and "*" not in ext and ext not in _NOT_EXTENSIONS
    )


def _progid(ext: str) -> str:
    return f"FusedRender{ext}"


def _type_name(ext: str) -> str:
    return f"{ext[1:].upper()} File (fused-render)"


def _icon_for_ext(ext: str) -> str:
    """Category .ico for this extension, or the fused-render diamond if that
    variant's icon is missing (e.g. assets not generated in a source tree)."""
    token = ext.rsplit(".", 1)[-1].lower()
    variant = _ICON_VARIANT_FOR_TOKEN.get(token, "file")
    icon = os.path.join(_FILE_ICONS_DIR, f"{variant}.ico")
    return icon if os.path.exists(icon) else _ICON_PATH


def _build_command(port: int | None) -> str:
    launcher = os.path.join(sysconfig.get_path("scripts"), "fused-render-open.exe")
    if os.path.exists(launcher):
        parts = [f'"{launcher}"']
    else:
        parts = [f'"{sys.executable}"', "-m", "fused_render.winopen"]
    if port is not None:
        parts += ["--port", str(port)]
    parts.append('"%1"')
    return " ".join(parts)


def _delete_value(key_path: str, name: str) -> None:
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
    except FileNotFoundError:
        return
    try:
        winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass
    finally:
        key.Close()


def _delete_tree(root, path: str) -> None:
    # winreg has no recursive delete; walk subkeys depth-first
    import winreg

    try:
        key = winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS)
    except FileNotFoundError:
        return
    try:
        while True:
            try:
                subkey_name = winreg.EnumKey(key, 0)
            except OSError:
                break
            _delete_tree(root, path + "\\" + subkey_name)
    finally:
        key.Close()
    winreg.DeleteKey(root, path)


def _register(port: int | None) -> None:
    if sys.platform != "win32":
        raise SystemExit("fused-render: --register is Windows-only.")
    import winreg

    command = _build_command(port)
    ext_list = extensions()

    # one ProgID per extension so Explorer's Type column keeps naming the format
    for ext in ext_list:
        progid_key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{_progid(ext)}")
        winreg.SetValueEx(progid_key, "", 0, winreg.REG_SZ, _type_name(ext))
        winreg.SetValueEx(progid_key, "FriendlyTypeName", 0, winreg.REG_SZ, _type_name(ext))
        icon_key = winreg.CreateKeyEx(progid_key, "DefaultIcon")
        winreg.SetValueEx(icon_key, "", 0, winreg.REG_SZ, f'"{_icon_for_ext(ext)}",0')
        open_cmd_key = winreg.CreateKeyEx(progid_key, r"shell\open\command")
        winreg.SetValueEx(open_cmd_key, "", 0, winreg.REG_SZ, command)
        openwith_key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{ext}\OpenWithProgids")
        winreg.SetValueEx(openwith_key, _progid(ext), 0, winreg.REG_SZ, "")
        _delete_value(rf"Software\Classes\{ext}\OpenWithProgids", _LEGACY_PROGID)
    _delete_tree(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{_LEGACY_PROGID}")

    # the Open With dialog resolves display names via Applications\<exe> FriendlyAppName
    app_key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Classes\Applications\fused-render-open.exe")
    winreg.SetValueEx(app_key, "FriendlyAppName", 0, winreg.REG_SZ, "fused-render")
    app_icon_key = winreg.CreateKeyEx(app_key, "DefaultIcon")
    winreg.SetValueEx(app_icon_key, "", 0, winreg.REG_SZ, f'"{_ICON_PATH}",0')
    app_cmd_key = winreg.CreateKeyEx(app_key, r"shell\open\command")
    winreg.SetValueEx(app_cmd_key, "", 0, winreg.REG_SZ, command)

    verb_key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Classes\*\shell\FusedRender")
    winreg.SetValueEx(verb_key, "", 0, winreg.REG_SZ, "Open with fused-render")
    winreg.SetValueEx(verb_key, "Icon", 0, winreg.REG_SZ, f'"{_ICON_PATH}"')
    verb_cmd_key = winreg.CreateKeyEx(verb_key, "command")
    winreg.SetValueEx(verb_cmd_key, "", 0, winreg.REG_SZ, command)

    # Explorer caches associations; broadcast so the entry shows without a restart
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x1000, None, None)
    _report(f"Registered fused-render:\n  command: {command}\n  extensions: {len(ext_list)}")


def _unregister() -> None:
    if sys.platform != "win32":
        raise SystemExit("fused-render: --unregister is Windows-only.")
    import winreg

    _delete_tree(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{_LEGACY_PROGID}")
    _delete_tree(winreg.HKEY_CURRENT_USER, r"Software\Classes\Applications\fused-render-open.exe")
    _delete_tree(winreg.HKEY_CURRENT_USER, r"Software\Classes\*\shell\FusedRender")

    for ext in extensions():
        _delete_tree(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{_progid(ext)}")
        _delete_value(rf"Software\Classes\{ext}\OpenWithProgids", _progid(ext))
        _delete_value(rf"Software\Classes\{ext}\OpenWithProgids", _LEGACY_PROGID)

    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x1000, None, None)
    _report("Unregistered fused-render.")


def _open(path: str | None, requested_port: int | None) -> None:
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    setup_logging()
    logger.info("winopen starting (pid %s, path=%r, port=%r)", os.getpid(), path, requested_port)
    if path and not os.path.exists(path):
        raise RuntimeError(f"file not found: {path}")
    url = _view_url(_ensure_server(requested_port), path)
    logger.info("opening %s", url)
    webbrowser.open(url)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m fused_render.winopen",
        description="Windows Explorer 'Open with' entry point for fused-render.",
    )
    parser.add_argument("--port", type=int, default=None, help="port to use/reuse (default: autodetect)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--register", action="store_true", help="register the 'Open with' associations and exit")
    group.add_argument("--unregister", action="store_true", help="remove the 'Open with' associations and exit")
    parser.add_argument("path", nargs="?", default=None, help="file to open in /view (default: home)")
    args = parser.parse_args()

    try:
        if args.register:
            _register(args.port)
        elif args.unregister:
            _unregister()
        else:
            _open(args.path, args.port)
    except Exception as exc:
        # windowless launcher: an uncaught exception is invisible without this
        logger.exception("winopen failed")
        ctypes.windll.user32.MessageBoxW(0, f"fused-render: {exc}", "fused-render", 0x10)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
