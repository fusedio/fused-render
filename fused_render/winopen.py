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
import re
import socket
import subprocess
import sys
import sysconfig
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import contextmanager
from urllib.parse import quote

from fused_render._branch import branch_dir, branch_port, branch_suffix
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
    **dict.fromkeys("png jpg jpeg gif webp svg bmp avif heic heif dng".split(), "image"),
    # documents / prose
    **dict.fromkeys("pdf md markdown txt log docx pptx tex ltx latex".split(), "doc"),
    # audio / video
    **dict.fromkeys("mp4 mov m4v webm mp3 wav m4a ogg flac".split(), "media"),
    # geospatial / vector
    **dict.fromkeys("geojson shp kml kmz gpx gpkg fgb pmtiles tif tiff las laz".split(), "geo"),
    # archives
    **dict.fromkeys("zip jar whl egg tar tgz tbz2 txz gz bz2 xz zst twbx tdsx".split(), "archive"),
    # databases
    **dict.fromkeys("sqlite sqlite3 db duckdb ddb".split(), "db"),
    # 3D models / point clouds
    **dict.fromkeys("usd usda usdc usdz ply splat ksplat obj stl glb gltf".split(), "model"),
}

_LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
_APP_SUPPORT_BASE = os.path.join(_LOCALAPPDATA, "fused-render")
APP_SUPPORT_DIR = branch_dir(_APP_SUPPORT_BASE)
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


def find_running_server() -> int | None:
    """Port of a live native instance answering right now, or None. Discovery
    never waits — a still-booting server is the spawn lock's problem, and its
    holder releases only once the server is ready. A manual `fused-render
    serve` writes no portfile, so the default range is scanned too."""
    filed = _read_int(PORTFILE)
    if filed is not None and _fused_server(filed):
        return filed
    for port in range(DEFAULT_PORT, MAX_PORT + 1):
        if port != filed and _fused_server(port):
            return port
    return None


@contextmanager
def _spawn_lock():
    """Serialize server startup across racing double-clicks: the winner holds
    a named mutex while its server boots, the rest wait here and then
    rediscover the ready server instead of double-spawning. Best-effort — on
    timeout or mutex failure the caller proceeds unlocked."""
    if sys.platform != "win32":
        yield
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\fused-render-open" + branch_suffix())
    if not handle:
        yield
        return
    # 0 = WAIT_OBJECT_0, 0x80 = WAIT_ABANDONED (previous holder died: ours now)
    acquired = kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 20_000) in (0, 0x80)
    try:
        yield
    finally:
        if acquired:
            kernel32.ReleaseMutex(ctypes.c_void_p(handle))
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def pick_port(start: int = DEFAULT_PORT, end: int = MAX_PORT) -> int:
    for port in range(start, end + 1):
        if not _port_in_use(port):
            return port
    raise RuntimeError(
        f"no free port between {start} and {end}; is something hogging the whole range?"
    )


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
    _write_pidfile(proc.pid, port)  # port = next opener's fast path, pid = diagnostics
    if _wait_until_ready(port):
        return port
    if proc.poll() is None:  # a child that already died during boot needs no kill
        proc.kill()
    _remove_pidfile()
    raise RuntimeError(f"server did not start on port {port}; see log: {SERVER_LOG}")


def _probe(requested: int | None) -> int | None:
    if requested is not None:
        return requested if _fused_server(requested) else None
    return find_running_server()


def _ensure_server(requested: int | None) -> int:
    """Return the port of a ready native server, starting one when needed.
    Probing never blocks; anything slow (a boot, a race) happens under
    _spawn_lock, so waiting peers re-probe once the winner is done."""
    port = _probe(requested)
    if port is not None:
        return port
    with _spawn_lock():
        port = _probe(requested)  # the lock's previous holder may have booted it
        if port is not None:
            return port
        if requested is not None:
            if _port_in_use(requested):
                # bound but silent: a just-started manual `fused-render --port N`
                # gets a moment to come up before we call the port foreign
                if _wait_until_ready(requested, timeout=3):
                    return requested
                raise RuntimeError(f"port {requested} is in use by another application")
            return _spawn(requested)
        return _spawn(pick_port())


_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def _view_url(port: int, path: str | None) -> str:
    """Encode an fs path into a /view URL the way the frontend codec
    (router.ts urlForFsPath) decodes it: only drive-letter paths get their
    backslashes normalized, so a UNC path stays one percent-encoded segment."""
    if not path:
        return f"http://127.0.0.1:{port}/"
    fs_path = os.path.abspath(path)
    norm = fs_path.replace("\\", "/") if _DRIVE_PATH.match(fs_path) else fs_path
    segments = [quote(seg, safe="") for seg in norm.lstrip("/").split("/") if seg]
    return f"http://127.0.0.1:{port}/view/" + "/".join(segments)


def _clone_url(port: int, raw_url: str) -> str:
    """URL of the /clone confirm page for an OS-delivered fused-render://
    deep link (SPEC §26, D110). Parsing/validation is server-side
    (deeplink.py); this only ferries the raw string as ?src=."""
    return f"http://127.0.0.1:{port}/clone?src=" + quote(raw_url, safe="")


def _report(msg: str) -> None:
    # sys.stdout is None under the gui-scripts launcher; use a message box there.
    if sys.stdout is not None:
        print(msg)
    else:
        ctypes.windll.user32.MessageBoxW(0, msg, "fused-render", 0x40)


def extensions() -> list[str]:
    """Distinct extensions to register, each reduced to its final suffix —
    Explorer keys on the last one, so ".csv.zst" becomes ".zst" and the glob
    ".*.json" becomes ".json". Directory sentinels and zarr members drop out."""
    with open(_REGISTRY_JSON, encoding="utf-8") as f:
        registry = json.load(f)
    exts = set()
    for key in registry:
        if not key.startswith(".") or "/" in key or key in _NOT_EXTENSIONS:
            continue
        exts.add("." + key.rsplit(".", 1)[-1])
    return sorted(exts)


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
        progid_key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, rf"Software\Classes\{_progid(ext)}"
        )
        winreg.SetValueEx(progid_key, "", 0, winreg.REG_SZ, _type_name(ext))
        winreg.SetValueEx(progid_key, "FriendlyTypeName", 0, winreg.REG_SZ, _type_name(ext))
        icon_key = winreg.CreateKeyEx(progid_key, "DefaultIcon")
        winreg.SetValueEx(icon_key, "", 0, winreg.REG_SZ, f'"{_icon_for_ext(ext)}",0')
        open_cmd_key = winreg.CreateKeyEx(progid_key, r"shell\open\command")
        winreg.SetValueEx(open_cmd_key, "", 0, winreg.REG_SZ, command)
        openwith_key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, rf"Software\Classes\{ext}\OpenWithProgids"
        )
        winreg.SetValueEx(openwith_key, _progid(ext), 0, winreg.REG_SZ, "")
        _delete_value(rf"Software\Classes\{ext}\OpenWithProgids", _LEGACY_PROGID)
    _delete_tree(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{_LEGACY_PROGID}")

    # the Open With dialog resolves display names via Applications\<exe> FriendlyAppName
    app_key = winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, r"Software\Classes\Applications\fused-render-open.exe"
    )
    winreg.SetValueEx(app_key, "FriendlyAppName", 0, winreg.REG_SZ, "fused-render")
    app_icon_key = winreg.CreateKeyEx(app_key, "DefaultIcon")
    winreg.SetValueEx(app_icon_key, "", 0, winreg.REG_SZ, f'"{_ICON_PATH}",0')
    app_cmd_key = winreg.CreateKeyEx(app_key, r"shell\open\command")
    winreg.SetValueEx(app_cmd_key, "", 0, winreg.REG_SZ, command)

    # fused-render:// URL protocol (SPEC §26, D110): browsers hand the whole
    # URL over as %1; _open detects the scheme prefix and routes it to the
    # /clone confirm page instead of /view. The empty "URL Protocol" value is
    # what marks the class as a scheme handler rather than a file type.
    proto_key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Classes\fused-render")
    winreg.SetValueEx(proto_key, "", 0, winreg.REG_SZ, "URL:fused-render")
    winreg.SetValueEx(proto_key, "URL Protocol", 0, winreg.REG_SZ, "")
    proto_icon_key = winreg.CreateKeyEx(proto_key, "DefaultIcon")
    winreg.SetValueEx(proto_icon_key, "", 0, winreg.REG_SZ, f'"{_ICON_PATH}",0')
    proto_cmd_key = winreg.CreateKeyEx(proto_key, r"shell\open\command")
    winreg.SetValueEx(proto_cmd_key, "", 0, winreg.REG_SZ, command)

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
    _delete_tree(winreg.HKEY_CURRENT_USER, r"Software\Classes\fused-render")
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
    if path and path.lower().startswith("fused-render:"):
        # Deep link (SPEC §26, D110): the URL-protocol registration hands the
        # whole fused-render:// URL over as %1 — not a filesystem path.
        url = _clone_url(_ensure_server(requested_port), path)
    else:
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
    parser.add_argument(
        "--port", type=int, default=None, help="port to use/reuse (default: autodetect)"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--register", action="store_true", help="register the 'Open with' associations and exit"
    )
    group.add_argument(
        "--unregister", action="store_true", help="remove the 'Open with' associations and exit"
    )
    parser.add_argument(
        "path", nargs="?", default=None, help="file to open in /view (default: home)"
    )
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
