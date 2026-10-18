"""Merge the user's LOGIN-SHELL PATH into this server's own environment.

A GUI app launched from Finder gets launchd's PATH (/usr/bin:/bin:...), not the
one a terminal has. Everything the server spawns inherits it — Claude sessions,
git, template tools — so anything installed through a shell manager (fnm, nvm,
volta, asdf, pyenv) is invisible: `npm: command not found` from a FusedRender
session whose very same command works in Terminal. Those managers only edit
rc files (~/.zshrc, ~/.zprofile), so the only thing that knows the real PATH is
the user's shell. claude_health._shell_probe already asks it, but only where
`claude` is.

A hardcoded list of dirs (the Homebrew append in claude_agent._spawn_env, #1670)
can never cover version managers, whose dirs carry per-shell or per-version
names. So: ONE probe at server start, in a background thread, merged into
os.environ. Every child then inherits it with no per-call-site work.

Ordering rule: (a) the LEADING run of the server's current entries that the
login shell does not have stays first — those are the app's own prepended dirs
(Contents/Resources/bin, ~/.fused-render/fused-bin, the bundled uv dir) and
must keep winning; (b) then every login-shell entry in the login shell's order,
so version-manager shims (pyenv/asdf/rbenv) beat launchd's /usr/bin exactly as
in Terminal; (c) then any remaining current entries.

A probe can take longer than a session's first message, so the last good result
is cached in home_dir()/login-path.json and applied synchronously by start();
the background probe then refreshes it.

Skipped on Windows (no login-shell PATH to ask), and when
FUSED_RENDER_NO_LOGIN_PATH is set — the test suite sets it so no test spawns
the developer's real shell.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from typing import Optional

# claude_health imports nothing from here, so no cycle.
from fused_render.claude_health import SUBPROCESS_KWARGS, executable

logger = logging.getLogger(__name__)

OPT_OUT_ENV = "FUSED_RENDER_NO_LOGIN_PATH"

_BEGIN = "__FR_PATH_BEGIN__"
_END = "__FR_PATH_END__"
# printenv rather than $PATH: fish keeps PATH as a list and `echo $PATH` there
# prints it space-separated. The markers make rc-file chatter (motd, "nvm
# loaded", prompt escapes) harmless: only the text between them is read.
_COMMAND = (f"printf %s {_BEGIN}; /usr/bin/printenv PATH; printf %s {_END}")

# Same order as claude_health._shell_probe: `-ilc` picks up an interactive rc
# file (where version managers usually live), but a shell that rejects the
# combined form prints nothing rather than failing loudly.
_FLAG_SETS = (["-ilc"], ["-lc"], ["-ic"])

_done = threading.Event()   # the probe finished (or was skipped)
_ready = threading.Event()  # a PATH is applied: from the cache or the probe
_started = False
_lock = threading.Lock()


def _parse(out: str) -> Optional[list]:
    if _BEGIN not in out:
        return None
    body = out.split(_BEGIN, 1)[1]
    if _END not in body:
        return None
    body = body.split(_END, 1)[0].strip()
    seen: list = []
    for entry in body.split(os.pathsep):
        if entry and os.path.isabs(entry) and entry not in seen:
            seen.append(entry)
    return seen or None


def _user_shell() -> str:
    """$SHELL, else the account's login shell, else /bin/bash."""
    sh = os.environ.get("SHELL")
    if sh:
        return sh
    if os.name != "nt":
        try:
            import pwd  # unix-only: must not be imported at module level
            sh = pwd.getpwuid(os.getuid()).pw_shell
        except Exception:
            sh = None
        if sh:
            return sh
    return "/bin/bash"


def probe(shell: Optional[str] = None, timeout: float = 8.0) -> Optional[list]:
    """The login shell's PATH entries, or None if it could not be read.

    Costs seconds (it sources the user's whole profile), which is why it runs
    once, off the main thread. After a timeout no further flag set is tried
    (each would source the same slow profile); whatever the timed-out shell
    already printed is still parsed, because an rc file that leaves a
    background process holding stdout open makes the shell exit with the
    markers printed while communicate() waits for EOF.
    """
    shell = shell or _user_shell()
    if not executable(shell):
        return None
    # PYTHONHOME/PYTHONPATH scrubbed for the reason _shell_probe does: the
    # packaged app exports both, and a non-bundled child dies on them.
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONHOME", "PYTHONPATH")}
    for flags in _FLAG_SETS:
        try:
            out = subprocess.run(
                [shell, *flags, _COMMAND],
                stdin=subprocess.DEVNULL, capture_output=True,
                timeout=timeout, env=env, **SUBPROCESS_KWARGS,
            ).stdout
        except subprocess.TimeoutExpired as e:
            partial = e.stdout
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            return _parse(partial or "")
        except (OSError, subprocess.SubprocessError):
            continue
        parsed = _parse(out or "")
        if parsed:
            return parsed
    return None


def merge(current: str, login: list) -> str:
    """Leading app-owned run of `current`, then `login` in order, then the rest.

    The leading run is the `current` entries before the first one the login
    shell also has. Deduped, empties dropped; merging its own result again
    returns it unchanged.
    """
    cur = [e for e in current.split(os.pathsep) if e]
    login_set = set(login)
    lead = 0
    while lead < len(cur) and cur[lead] not in login_set:
        lead += 1
    out: list = []
    for entry in [*cur[:lead], *login, *cur[lead:]]:
        if entry and entry not in out:
            out.append(entry)
    return os.pathsep.join(out)


def _cache_path() -> str:
    from fused_render.shell.storage import home_dir
    return os.path.join(home_dir(), "login-path.json")


def _read_cache(shell: str) -> Optional[list]:
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        entries = data["path"]
        if data["shell"] != shell or not isinstance(entries, list):
            return None
        entries = [e for e in entries if isinstance(e, str) and os.path.isabs(e)]
        return entries or None
    except Exception:
        logger.debug("login-shell PATH cache unreadable; ignoring", exc_info=True)
        return None


def _write_cache(shell: str, entries: list) -> None:
    try:
        path = _cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"shell": shell, "path": entries}, f)
        os.replace(tmp, path)
    except OSError:
        logger.debug("could not write login-shell PATH cache", exc_info=True)


def _apply(entries: list) -> None:
    before = os.environ.get("PATH", "")
    merged = merge(before, entries)
    os.environ["PATH"] = merged
    old = set(before.split(os.pathsep))
    added = len([e for e in merged.split(os.pathsep) if e not in old])
    logger.info("login-shell PATH merged: %d dir(s) added", added)


def _run(shell: str, cached: Optional[list]) -> None:
    try:
        entries = probe(shell)
        if entries:
            _write_cache(shell, entries)
            if entries != cached:
                _apply(entries)
        else:
            logger.info("login-shell PATH probe found nothing; PATH unchanged")
    except Exception:  # never let a PATH nicety take the server down
        logger.exception("login-shell PATH probe failed")
    finally:
        _done.set()
        _ready.set()


def start() -> None:
    """Kick off the probe once. Idempotent; never raises."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
        if os.name == "nt" or os.environ.get(OPT_OUT_ENV):
            _done.set()
            _ready.set()
            return
        try:
            shell = _user_shell()
            cached = _read_cache(shell)
            if cached:
                _apply(cached)
                _ready.set()
            threading.Thread(target=_run, args=(shell, cached),
                             name="login-path", daemon=True).start()
        except Exception:
            logger.exception("could not start login-shell PATH probe")
            _done.set()
            _ready.set()


def wait(timeout: float) -> bool:
    """Block until a login-shell PATH is in os.environ (True) or `timeout` passed.

    True at once if start() was never called, so contexts that never start the
    probe (tests, scripts) are not slowed down, and True at once when start()
    applied the disk cache: a usable PATH is already there, and the background
    probe only refreshes it.
    """
    if not _started:
        return True
    return _ready.wait(timeout)


def _reset_for_tests() -> None:
    global _started
    with _lock:
        _started = False
        _done.clear()
        _ready.clear()
