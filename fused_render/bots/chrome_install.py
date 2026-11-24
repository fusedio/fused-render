"""Installing Google Chrome inside WSL for the person, from a button.

Bots launch their own browser on the Linux side (browser.find_chrome), and a fresh WSL distro has none: the one
the person uses is on Windows and cannot be driven from here. Until now the page said so and left them to find
the .deb and the apt line. This runs both.

    plan()    -> {available, reason, command}    can the button work here, and what it runs
    start()   -> the opening record              one install at a time (InstallError when refused)
    status()  -> {state, detail, output, error}  idle | running | done | error

What it runs, and why this way:

- Google's own .deb, downloaded by this process as the person (no root needed for that), into a private temp dir.
- `apt-get install -y <the .deb>` as root, so apt brings the forty-odd libraries Chrome needs that a WSL distro
  does not ship (libnss3, libgbm1, fonts). Unpacking a browser into the home folder would need no root, and would
  not start. The package also adds Google's apt source, so `apt upgrade` keeps it current.
- Root comes from `wsl.exe -d <distro> -u root`, which asks for no password: the Windows account owns the distro
  (_wsl.py). So no sudo prompt, which a server has no terminal to show. Each command goes through `-e` (exec, no
  shell) with space-free arguments, so nothing is re-quoted on the way through the Windows command line.

Only where all of that holds: WSL, a named distro, apt-get, x86-64 (Google ships no other Linux build as a
.deb). Anywhere else `plan()` says why and the page keeps the sentence it had.

A thread in the server process and one module-level record, as claude_install.py (whose reasons apply here:
single flight, the child's own words kept verbatim, a re-probe at the end that decides success).
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

from fused_render import _wsl

logger = logging.getLogger(__name__)

DEB_URL = "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
DEB_NAME = "google-chrome-stable_current_amd64.deb"
JOB_ID = "sys:bots-chrome-install"
DOWNLOAD_TIMEOUT_S = 60       # per read: a stalled connection, not the whole download
STEP_TIMEOUT_S = 900          # one apt command; a first `apt-get update` on a slow link is minutes, not this
_OUTPUT_TAIL_LINES = 40
# What the person can paste into a WSL terminal instead (shown when the button cannot work, and on failure).
MANUAL = (f"wget {DEB_URL} && sudo apt-get install -y ./{DEB_NAME}")

_lock = threading.Lock()
_IDLE = {"state": "idle", "detail": "", "output": "", "error": None, "started_at": None, "finished_at": None}
_state = dict(_IDLE)


class InstallError(RuntimeError):
    """A refusal the route answers as a 409 with this sentence."""


def plan(env=None, machine=None, which=shutil.which, wsl=None, euid=None) -> dict:
    """Whether the button can work on this machine. Pure when every argument is passed."""
    env = os.environ if env is None else env
    machine = platform.machine() if machine is None else machine
    wsl = _wsl.is_wsl() if wsl is None else wsl
    euid = os.geteuid() if euid is None else euid
    reason = ""
    if not wsl:
        reason = "only inside WSL"
    elif machine.lower() not in ("x86_64", "amd64"):
        reason = f"Google ships no Chrome package for {machine} Linux"
    elif not which("apt-get"):
        reason = "this distro has no apt-get"
    elif euid != 0 and not env.get("WSL_DISTRO_NAME"):
        reason = "the WSL distro's name is unknown (WSL_DISTRO_NAME is not set)"
    elif euid != 0 and not _wsl.windows_exe("wsl.exe", which):
        reason = "wsl.exe cannot be reached from this distro (Windows interop is off)"
    return {"available": not reason, "reason": reason, "command": MANUAL}


def _root_argv(argv, env=None, euid=None, which=shutil.which) -> list:
    """`argv` as a command that runs as root in this distro: as is when the server already is root, else
    through `wsl.exe -u root -e` (no password, no shell)."""
    env = os.environ if env is None else env
    if (os.geteuid() if euid is None else euid) == 0:
        return list(argv)
    return [_wsl.windows_exe("wsl.exe", which) or "wsl.exe", "-d", env.get("WSL_DISTRO_NAME", ""),
            "-u", "root", "-e", *argv]


def status() -> dict:
    with _lock:
        return dict(_state)


def running() -> bool:
    with _lock:
        return _state["state"] == "running"


def _publish(**fields) -> None:
    with _lock:
        _state.update(fields)
        snap = dict(_state)
    try:  # the shell's download manager: the work stays visible after the page that started it is gone
        from fused_render import jobs

        jobs.upsert({"id": JOB_ID, "title": "Installing Google Chrome in WSL", "kind": "task",
                     "state": (jobs.RUNNING if snap["state"] == "running"
                               else "error" if snap["state"] == "error" else "done"),
                     "detail": snap["detail"], "message": snap["error"] or "", "origin": "Bots"},
                    page="/bots", server=True)
    except Exception:  # noqa: BLE001 — reporting must never break the install
        logger.debug("could not report the Chrome install job", exc_info=True)


def _download(url: str, dest: str) -> None:
    """Fetch the package, saying how far along it is. Raises OSError (URLError is one) on any failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "fused-render"})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got, said = 0, -1
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            pct = (got * 100 // total) if total else -1
            if pct != said:
                said = pct
                _publish(detail=f"Downloading Google Chrome: {pct}%" if total
                         else f"Downloading Google Chrome: {got >> 20} MB")
    if total and got != total:
        raise OSError(f"the download stopped at {got} of {total} bytes")


def _run_step(argv, tail: list) -> int:
    """One root command to its end, its output kept in `tail`. Returns the exit code (-1: timed out)."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            cwd="/", start_new_session=True)
    timed_out = threading.Event()

    def expire() -> None:  # a timer, not a check between lines: apt can sit silent on a lock
        timed_out.set()
        try:
            proc.kill()
        except OSError:
            pass

    watchdog = threading.Timer(STEP_TIMEOUT_S, expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        for raw in proc.stdout or ():
            # apt's lines are UTF-8; wsl.exe's own complaints are UTF-16 (a NUL after every letter).
            line = raw.decode("utf-8", "replace").replace("\x00", "").rstrip()
            if line.strip():
                tail.append(line)
                del tail[:-_OUTPUT_TAIL_LINES]
                _publish(detail=line[:200], output="\n".join(tail))
        code = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        expire()
        code = -1
    finally:
        watchdog.cancel()
    return -1 if timed_out.is_set() else code


def _run() -> None:
    from fused_render.bots import browser

    tail: list = []

    def fail(msg: str) -> None:
        _publish(state="error", error=msg, output="\n".join(tail), finished_at=time.time())

    tmp = tempfile.mkdtemp(prefix="fused-render-chrome-")  # 0700: nobody else can swap the package before root reads it
    try:
        if " " in tmp:
            return fail(f"the temp folder has a space in its path ({tmp}); run this in a WSL terminal instead: {MANUAL}")
        deb = os.path.join(tmp, DEB_NAME)
        _publish(detail="Downloading Google Chrome")
        try:
            _download(DEB_URL, deb)
        except OSError as e:
            return fail(f"could not download Google Chrome: {e}")
        # `update` first: a fresh distro has empty package lists, and the install would find none of the libraries.
        # A failed `update` (one dead mirror among many) need not stop the install; a failed install does.
        steps = (("Reading the package lists", ["apt-get", "update"], False),
                 ("Installing Google Chrome",
                  ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", deb], True))
        for label, argv, needed in steps:
            _publish(detail=label)
            try:
                code = _run_step(_root_argv(argv), tail)
            except OSError as e:
                return fail(f"could not run {argv[0]} as root: {e}")
            if code == -1:
                return fail(f"`{' '.join(argv[:3])}` took longer than {STEP_TIMEOUT_S // 60} minutes and was stopped")
            if code != 0 and needed:
                return fail(f"`apt-get install` exited with code {code}")
        found = browser.found_chrome()
        if not found:
            return fail("the install finished, but no browser can be found")
        _publish(state="done", detail=f"Installed: {found}", output="\n".join(tail), finished_at=time.time())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def start() -> dict:
    """Begin the install and return the opening record. Refuses when it cannot work here or one is running."""
    p = plan()
    if not p["available"]:
        raise InstallError(f"Chrome cannot be installed from here: {p['reason']}")
    with _lock:
        if _state["state"] == "running":
            raise InstallError("a Chrome install is already running")
        _state.update({**_IDLE, "state": "running", "detail": "Starting", "started_at": time.time()})
        claimed = dict(_state)

    def guarded() -> None:
        try:
            _run()
        except BaseException as e:  # noqa: BLE001 — a record left "running" would refuse every later install
            logger.exception("the Chrome install worker died")
            _publish(state="error", error=f"the install stopped unexpectedly: {e}", finished_at=time.time())

    threading.Thread(target=guarded, daemon=True, name="bots-chrome-install").start()
    return claimed


def reset() -> None:
    """Tests only."""
    with _lock:
        _state.clear()
        _state.update(_IDLE)
