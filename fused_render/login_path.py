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

Ordering rule: the server's CURRENT entries stay first. The app deliberately
prepends its own bin dirs (Contents/Resources/bin, ~/.fused-render/fused-bin)
and those must keep winning; login-shell entries are only appended.

Skipped on Windows (no login-shell PATH to ask), and when
FUSED_RENDER_NO_LOGIN_PATH is set — the test suite sets it so no test spawns
the developer's real shell.
"""
from __future__ import annotations

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

_done = threading.Event()
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


def probe(shell: Optional[str] = None, timeout: float = 8.0) -> Optional[list]:
    """The login shell's PATH entries, or None if it could not be read.

    Costs seconds (it sources the user's whole profile), which is why it runs
    once, off the main thread.
    """
    shell = shell or os.environ.get("SHELL") or "/bin/bash"
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
        except (OSError, subprocess.SubprocessError):
            continue
        parsed = _parse(out or "")
        if parsed:
            return parsed
    return None


def merge(current: str, login: list) -> str:
    """`current` entries first, in order; then login entries not yet present."""
    out: list = []
    for entry in [*current.split(os.pathsep), *login]:
        if entry and entry not in out:
            out.append(entry)
    return os.pathsep.join(out)


def _run() -> None:
    try:
        entries = probe()
        if entries:
            before = os.environ.get("PATH", "")
            merged = merge(before, entries)
            os.environ["PATH"] = merged
            added = len(merged.split(os.pathsep)) - len([p for p in before.split(os.pathsep) if p])
            logger.info("login-shell PATH merged: %d dir(s) added", added)
        else:
            logger.info("login-shell PATH probe found nothing; PATH unchanged")
    except Exception:  # never let a PATH nicety take the server down
        logger.exception("login-shell PATH probe failed")
    finally:
        _done.set()


def start() -> None:
    """Kick off the probe once. Idempotent; never raises."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
        if os.name == "nt" or os.environ.get(OPT_OUT_ENV):
            _done.set()
            return
        try:
            threading.Thread(target=_run, name="login-path", daemon=True).start()
        except Exception:
            logger.exception("could not start login-shell PATH probe")
            _done.set()


def wait(timeout: float) -> bool:
    """Block until the probe finished (True) or `timeout` passed (False).

    True at once if start() was never called, so contexts that never start the
    probe (tests, scripts) are not slowed down.
    """
    if not _started:
        return True
    return _done.wait(timeout)


def _reset_for_tests() -> None:
    global _started
    with _lock:
        _started = False
        _done.clear()
