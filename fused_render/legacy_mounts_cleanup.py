"""TEMPORARY upgrade shim: clean up after the removed rclone "mount" feature.

DELETE THIS MODULE (and its call in server/app.py, and
tests/test_legacy_mounts_cleanup.py) once every install has had a release or
two to upgrade past the removal (DECISIONS.md, "Remove the rclone mount
feature").

An install that upgrades from a build with mounts can still have, from the old
version: a detached `rclone rcd` daemon, live FUSE/NFS mounts under
`~/.fused-render/mounts/*`, and a pile of state files nothing reads any more.
Left alone, the daemon and the mounts keep running (and the kernel mounts can
wedge anything that stats them), so the first start of the new version tidies
up once, in the background.

Contract, all of it deliberate:

  * Best-effort and never raises. Every step is wrapped; a failure is logged at
    debug level and the next step still runs.
  * Off the startup critical path: `start_background()` runs it on a daemon
    thread. Every subprocess has a short timeout.
  * It NEVER stats or lists a mount path before unmounting it. The only
    directory it lists is `<home>/mounts` itself (the parent, which is on the
    local disk); the entries are handed to the unmount command by name.
  * It NEVER deletes `rclone.conf`. That file holds the user's remotes and
    credentials; a user who also runs rclone by hand may depend on it, so the
    explicit delete list below simply does not name it.
  * Subprocess spawns follow the PROJ atfork rule: `close_fds=False`, an
    absolute executable path, and no `cwd=`.
  * Runs once per home: a `legacy-mounts-cleanup.done` marker is written when
    it finishes, so a normal start afterwards costs one `os.path.exists`.
"""
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

from fused_render.shell import storage

logger = logging.getLogger(__name__)

MARKER = "legacy-mounts-cleanup.done"

# Files in the state dir that only the removed feature read or wrote.
# rclone.conf is intentionally NOT here, and neither is its directory.
_STATE_FILES = ("mounts.json", "connectors.json", "rcd.json", "rcd.log",
                "serves.json")
_STATE_DIRS = ("nfs-handle-cache",)

_CMD_TIMEOUT_S = 5.0
_TERM_GRACE_S = 3.0


def _run(argv, runner=subprocess.run):
    """Run one short command; return the CompletedProcess or None on any
    failure. argv[0] must already be absolute."""
    try:
        return runner(argv, capture_output=True, text=True,
                      timeout=_CMD_TIMEOUT_S, close_fds=False)
    except Exception as exc:  # noqa: BLE001 - best-effort by contract
        logger.debug("legacy mounts cleanup: %s failed: %s", argv, exc)
        return None


def _which(name: str, fallbacks=()) -> str | None:
    found = shutil.which(name)
    if found:
        return os.path.abspath(found)
    for p in fallbacks:
        if os.path.isfile(p):
            return p
    return None


# ------------------------------------------------------------- stop the daemon


def _pid_is_rcd(pid: int, *, runner, platform) -> bool:
    """True only when `pid` is recognisably an `rclone ... rcd` process, so a
    recycled pid is never signalled."""
    if not isinstance(pid, int) or pid <= 1:
        return False
    if platform == "win32":
        tasklist = _which("tasklist.exe")
        if not tasklist:
            return False
        res = _run([tasklist, "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], runner)
        return bool(res and "rclone" in (res.stdout or "").lower())
    ps = _which("ps", ("/bin/ps", "/usr/bin/ps"))
    if not ps:
        return False
    res = _run([ps, "-o", "command=", "-p", str(pid)], runner)
    out = (res.stdout or "").lower() if res else ""
    return "rclone" in out and "rcd" in out


def _stop_pid(pid: int, *, runner, kill, platform, sleep) -> None:
    if platform == "win32":
        taskkill = _which("taskkill.exe")
        if taskkill:
            _run([taskkill, "/PID", str(pid), "/T", "/F"], runner)
        return
    try:
        kill(pid, signal.SIGTERM)  # lets rclone unmount cleanly
    except OSError:
        return
    deadline = time.monotonic() + _TERM_GRACE_S
    while time.monotonic() < deadline:
        sleep(0.2)
        try:
            kill(pid, 0)
        except OSError:
            return
    try:
        kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _rcd_pids(home: str, base_home: str) -> list[int]:
    pids: list[int] = []
    state = storage.read_json(os.path.join(home, "rcd.json"))
    if isinstance(state, dict) and isinstance(state.get("pid"), int):
        pids.append(state["pid"])
    reg = storage.read_json(os.path.join(base_home, "rcd-registry.json"))
    if isinstance(reg, list):
        for e in reg:
            if (isinstance(e, dict) and isinstance(e.get("pid"), int)
                    and e.get("dir") == home and e["pid"] not in pids):
                pids.append(e["pid"])
    return pids


def _stop_rcd(home, base_home, *, runner, kill, platform, sleep) -> None:
    for pid in _rcd_pids(home, base_home):
        try:
            if _pid_is_rcd(pid, runner=runner, platform=platform):
                _stop_pid(pid, runner=runner, kill=kill, platform=platform,
                          sleep=sleep)
        except Exception as exc:  # noqa: BLE001
            logger.debug("legacy mounts cleanup: stopping pid %s: %s", pid, exc)


# ---------------------------------------------------------------- unmount


def _unmount_argvs(path: str, platform: str) -> list[list[str]]:
    """Force-unmount command candidates for one mountpoint, most specific
    first; each is only run if its executable resolves to an absolute path."""
    argvs: list[list[str]] = []
    if platform == "darwin":
        umount = _which("umount", ("/sbin/umount",))
        diskutil = _which("diskutil", ("/usr/sbin/diskutil",))
        if umount:
            argvs.append([umount, "-f", path])
        if diskutil:
            argvs.append([diskutil, "unmount", "force", path])
    elif platform.startswith("linux"):
        for name in ("fusermount3", "fusermount"):
            exe = _which(name)
            if exe:
                argvs.append([exe, "-uz", path])
        umount = _which("umount", ("/bin/umount", "/usr/bin/umount"))
        if umount:
            argvs.append([umount, "-l", path])
    # Windows: rclone's cmount dies with the process we just stopped; there is
    # no mountpoint directory to force-unmount.
    return argvs


def _unmount_all(mounts_dir: str, *, runner, platform) -> list[str]:
    """Force-unmount every entry under `mounts_dir`. Returns the entry paths.
    Lists only `mounts_dir` itself and never stats an entry."""
    try:
        with os.scandir(mounts_dir) as it:
            names = [e.name for e in it]
    except OSError:
        return []
    paths = [os.path.join(mounts_dir, n) for n in names]
    for path in paths:
        for argv in _unmount_argvs(path, platform):
            res = _run(argv, runner)
            if res is not None and res.returncode == 0:
                break
    return paths


# ---------------------------------------------------------------- delete


def _cache_dirs(environ) -> list[str]:
    """rclone VFS cache dirs the removed feature could have created: the
    supervisor's RCLONE_CACHE_DIR / <FUSED_RENDER_CACHE_DIR>/rclone."""
    out: list[str] = []
    explicit = environ.get("RCLONE_CACHE_DIR")
    if explicit:
        out.append(explicit)
    base = environ.get("FUSED_RENDER_CACHE_DIR")
    if base:
        out.append(os.path.join(base, "rclone"))
    return list(dict.fromkeys(out))


def _rm(path: str) -> None:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.remove(path)
    except OSError:
        pass


def run(*, home=None, base_home=None, environ=None, runner=subprocess.run,
        kill=os.kill, platform=None, sleep=time.sleep) -> None:
    """The whole cleanup, synchronously. Never raises. The keyword arguments
    exist so tests can fake the subprocess/kill layer and the paths."""
    try:
        home = home or storage.home_dir()
        base_home = base_home or storage.base_home_dir()
        environ = os.environ if environ is None else environ
        platform = platform or sys.platform
        marker = os.path.join(home, MARKER)
        if os.path.exists(marker):
            return
        legacy_present = any(
            os.path.exists(os.path.join(home, n))
            for n in _STATE_FILES + _STATE_DIRS + ("mounts",)
        ) or os.path.exists(os.path.join(base_home, "rcd-registry.json"))

        if legacy_present:
            steps = (
                lambda: _stop_rcd(home, base_home, runner=runner, kill=kill,
                                  platform=platform, sleep=sleep),
                lambda: _finish_mounts(home, runner=runner, platform=platform),
                lambda: _delete_state(home, base_home, environ),
            )
            for step in steps:
                try:
                    step()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("legacy mounts cleanup step failed: %s", exc)
        try:
            os.makedirs(home, exist_ok=True)
            with open(marker, "w", encoding="utf-8") as fh:
                json.dump({"done": time.time()}, fh)
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("legacy mounts cleanup failed: %s", exc)


def _finish_mounts(home, *, runner, platform) -> None:
    mounts_dir = os.path.join(home, "mounts")
    paths = _unmount_all(mounts_dir, runner=runner, platform=platform)
    for p in paths:
        try:
            os.rmdir(p)  # only succeeds on an empty, unmounted dir
        except OSError:
            pass
    for hidden in (".metadata_never_index",):
        try:
            os.remove(os.path.join(mounts_dir, hidden))
        except OSError:
            pass
    try:
        os.rmdir(mounts_dir)
    except OSError:
        pass


def _delete_state(home, base_home, environ) -> None:
    for name in _STATE_FILES:
        _rm(os.path.join(home, name))
    for name in _STATE_DIRS:
        _rm(os.path.join(home, name))
    _rm(os.path.join(base_home, "rcd-registry.json"))
    for d in _cache_dirs(environ):
        _rm(d)


def start_background() -> None:
    """Fire-and-forget: run the cleanup on a daemon thread."""
    try:
        threading.Thread(target=run, name="legacy-mounts-cleanup",
                         daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        logger.debug("legacy mounts cleanup: could not start: %s", exc)
