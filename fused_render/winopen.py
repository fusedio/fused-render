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

_PROGID = "FusedRender.file"
_ICON_PATH = os.path.join(os.path.dirname(__file__), "assets", "fused-render.ico")
_REGISTRY_JSON = os.path.join(os.path.dirname(__file__), "templates", "registry.json")
_NOT_EXTENSIONS = {".zgroup", ".zattrs", ".zmetadata"}  # zarr member files, not extensions

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


def _probe(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def find_running_server() -> int | None:
    """Port of a live instance, or None — HTTP probe only; on Windows
    os.kill(pid, 0) calls TerminateProcess, so the pid is never probed."""
    port = _read_int(PORTFILE)
    if port is None or not _probe(port):
        return None
    return port


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
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.2)
    return False


def _write_pidfile(pid: int, port: int) -> None:
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    with open(PIDFILE, "w", encoding="utf-8") as f:
        f.write(str(pid))
    with open(PORTFILE, "w", encoding="utf-8") as f:
        f.write(str(port))


def _start_server(port: int) -> subprocess.Popen:
    """Spawn the server detached; stdout/stderr go to server.out.log since a
    windowless parent has None streams and cli.py prints at startup."""
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    log_file = open(SERVER_LOG, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "fused_render.cli", "serve", "--no-browser", "--port", str(port)],
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        close_fds=True,
        creationflags=(
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
        ),
    )
    log_file.close()
    return proc


def _view_url(port: int, path: str | None) -> str:
    """Build the /view URL the frontend codec (router.ts urlForFsPath) decodes:
    forward slashes, each segment percent-encoded on its own ("C:" -> "C%3A")."""
    if not path:
        return f"http://127.0.0.1:{port}/"
    fs_path = os.path.abspath(path).replace("\\", "/")
    segments = [quote(seg, safe="") for seg in fs_path.split("/") if seg]
    return f"http://127.0.0.1:{port}/view/" + "/".join(segments)


def _fail(msg: str) -> None:
    logger.error(msg)
    ctypes.windll.user32.MessageBoxW(0, msg, "fused-render", 0x10)


def _report(msg: str) -> None:
    """fused-render-open.exe is a gui-scripts launcher (no console), so
    sys.stdout is None there; fall back to a message box."""
    if sys.stdout is not None:
        print(msg)
    else:
        ctypes.windll.user32.MessageBoxW(0, msg, "fused-render", 0x40)


def extensions() -> list[str]:
    with open(_REGISTRY_JSON, encoding="utf-8") as f:
        registry = json.load(f)
    # registry.json also keys directory sentinels ("/", ".zarr/"); real
    # extensions start with "." and contain no "/".
    return sorted(
        ext
        for ext in registry
        if ext.startswith(".") and "/" not in ext and ext not in _NOT_EXTENSIONS
    )


def _build_command(port: int | None) -> str:
    launcher = os.path.join(sysconfig.get_path("scripts"), "fused-render-open.exe")
    if os.path.exists(launcher):
        parts = [f'"{launcher}"']
    else:
        # editable/dev installs may not have the entry-point exe on PATH yet
        parts = [f'"{sys.executable}"', "-m", "fused_render.winopen"]
    if port is not None:
        parts += ["--port", str(port)]
    parts.append('"%1"')
    return " ".join(parts)


def _delete_tree(root, path: str) -> None:
    """winreg has no recursive delete; walk subkeys depth-first before
    deleting the key itself. Missing keys are not errors."""
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

    progid_key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{_PROGID}")
    winreg.SetValueEx(progid_key, "", 0, winreg.REG_SZ, "fused-render")
    winreg.SetValueEx(progid_key, "FriendlyTypeName", 0, winreg.REG_SZ, "fused-render")
    icon_key = winreg.CreateKeyEx(progid_key, "DefaultIcon")
    winreg.SetValueEx(icon_key, "", 0, winreg.REG_SZ, f'"{_ICON_PATH}",0')
    open_cmd_key = winreg.CreateKeyEx(progid_key, r"shell\open\command")
    winreg.SetValueEx(open_cmd_key, "", 0, winreg.REG_SZ, command)

    # The Open With dialog resolves display names via Applications\<exe>
    # FriendlyAppName (the entry-point launcher exe has no version info).
    app_key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Classes\Applications\fused-render-open.exe")
    winreg.SetValueEx(app_key, "FriendlyAppName", 0, winreg.REG_SZ, "fused-render")
    app_icon_key = winreg.CreateKeyEx(app_key, "DefaultIcon")
    winreg.SetValueEx(app_icon_key, "", 0, winreg.REG_SZ, f'"{_ICON_PATH}",0')
    app_cmd_key = winreg.CreateKeyEx(app_key, r"shell\open\command")
    winreg.SetValueEx(app_cmd_key, "", 0, winreg.REG_SZ, command)

    for ext in ext_list:
        openwith_key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{ext}\OpenWithProgids")
        winreg.SetValueEx(openwith_key, _PROGID, 0, winreg.REG_SZ, "")

    verb_key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Classes\*\shell\FusedRender")
    winreg.SetValueEx(verb_key, "", 0, winreg.REG_SZ, "Open with fused-render")
    winreg.SetValueEx(verb_key, "Icon", 0, winreg.REG_SZ, f'"{_ICON_PATH}"')
    verb_cmd_key = winreg.CreateKeyEx(verb_key, "command")
    winreg.SetValueEx(verb_cmd_key, "", 0, winreg.REG_SZ, command)

    # Explorer caches associations; without this the new entry doesn't show
    # up in "Open with" until the next Explorer restart.
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x1000, None, None)
    _report(f"Registered fused-render:\n  command: {command}\n  extensions: {len(ext_list)}")


def _unregister() -> None:
    if sys.platform != "win32":
        raise SystemExit("fused-render: --unregister is Windows-only.")
    import winreg

    _delete_tree(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{_PROGID}")
    _delete_tree(winreg.HKEY_CURRENT_USER, r"Software\Classes\Applications\fused-render-open.exe")
    _delete_tree(winreg.HKEY_CURRENT_USER, r"Software\Classes\*\shell\FusedRender")

    for ext in extensions():
        try:
            openwith_key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, rf"Software\Classes\{ext}\OpenWithProgids", 0, winreg.KEY_ALL_ACCESS
            )
        except FileNotFoundError:
            continue
        try:
            winreg.DeleteValue(openwith_key, _PROGID)
        except FileNotFoundError:
            pass
        finally:
            openwith_key.Close()

    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x1000, None, None)
    _report("Unregistered fused-render.")


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

    if args.register:
        _register(args.port)
        return
    if args.unregister:
        _unregister()
        return

    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    setup_logging()  # first: everything after this can crash-report to the file
    logger.info("winopen starting (pid %s, path=%r, port=%r)", os.getpid(), args.path, args.port)

    if args.path and not os.path.exists(args.path):
        _fail(f"fused-render: file not found:\n{args.path}")
        return

    if args.port is not None:
        port, alive = args.port, _probe(args.port)
    else:
        port = find_running_server()
        alive = port is not None
        if port is None:
            port = pick_port()

    if not alive:
        # Re-probe before spawning: rapid double-clicks race here, and the
        # loser's bind fails while this probe finds the winner.
        alive = _probe(port)

    if not alive:
        proc = _start_server(port)
        logger.info("starting server (pid %s, port %s)", proc.pid, port)
        if not _wait_until_ready(port):
            _fail(f"fused-render: server did not start on port {port}.\nSee log: {SERVER_LOG}")
            return
        _write_pidfile(proc.pid, port)

    url = _view_url(port, args.path)
    logger.info("opening %s", url)
    webbrowser.open(url)


if __name__ == "__main__":
    main()
