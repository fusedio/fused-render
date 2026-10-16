import logging
import os
import subprocess
import threading

from fused_render.supervisor.paths import environment_block

logger = logging.getLogger(__name__)

_SYSTEM_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
_FALLBACK_DIRS = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
                  os.path.expanduser("~/.local/bin"))
_MARK = "__FUSED_RENDER_LOGIN_PATH__"
_TIMEOUT_S = 5
_lock = threading.Lock()
_cached = None


def _has_controlling_tty() -> bool:
    try:
        os.close(os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY))
    except OSError:
        return False
    return True


def probe(shell: str = "") -> str:
    shell = shell or os.environ.get("SHELL") or "/bin/zsh"
    if not os.path.isabs(shell):
        return ""
    env = environment_block({"PATH": os.pathsep.join(_SYSTEM_DIRS)})
    script = "echo %s; /usr/bin/printenv PATH" % _MARK
    attempts = ((["-l", "-c"],) if _has_controlling_tty()
                else (["-l", "-i", "-c"], ["-l", "-c"]))
    for flags in attempts:
        try:
            out = subprocess.run(
                [shell, *flags, script], stdin=subprocess.DEVNULL,
                capture_output=True, encoding="utf-8", errors="replace",
                env=env, timeout=_TIMEOUT_S, close_fds=False).stdout
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or b"").decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return ""
        _, found, tail = out.rpartition(_MARK + "\n")
        line, ended, _ = tail.partition("\n")
        path = line.strip() if found and ended else ""
        if path:
            return path
    return ""


def login_path() -> str:
    global _cached
    with _lock:
        if _cached is None:
            _cached = probe()
        return _cached


def warm() -> None:
    threading.Thread(target=login_path, daemon=True, name="login-path").start()


def merge(current: str, login: str) -> str:
    parts = current.split(os.pathsep)
    head = login.split(os.pathsep) if login else list(_FALLBACK_DIRS)
    cut = next((i for i, p in enumerate(parts) if p in _SYSTEM_DIRS), len(parts))
    return os.pathsep.join(filter(None, dict.fromkeys(parts[:cut] + head + parts[cut:])))


def adopt() -> None:
    login = login_path()
    if not login:
        logger.warning("login shell gave no PATH; adding the default install dirs only")
    os.environ["PATH"] = merge(os.environ.get("PATH", ""), login)
    logger.info("PATH for this app and its children: %s", os.environ["PATH"])
