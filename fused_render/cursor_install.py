"""Running the Cursor CLI install, and its update, on the user's behalf.

The Cursor twin of `opencode_install.py`: one record, two actions, a thread
with a watchdog, a verbatim output tail, single-flight, and a re-probe on
success (docs/CURSOR_BACKEND.md D10). `claude_install.py`'s docstring carries
the argument for each; this one differs in the command lines — Cursor's own
installer (`curl https://cursor.com/install -fsS | bash`, run through bash
exactly as `claude_install` runs Claude's) for the install, and the CLI's
own `cursor-agent update` for the update — and in having ITS OWN slot, for
the reason copilot_install gives. A Homebrew cask install is recognised by
`install_method` but never updated here.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time

from fused_render import cursor_health, jobs
from fused_render.claude_health import SUBPROCESS_KWARGS

logger = logging.getLogger(__name__)

#: Its own job id — never the Claude or Copilot one, or the download manager
#: would show one row flipping between products.
JOB_ID = "sys:cursor-install"
TIMEOUT_S = 600
_OUTPUT_TAIL_LINES = 40
ACTIONS = ("install", "update")


class InstallError(RuntimeError):
    """A refusal the caller should turn into a 4xx with this text in it."""


def install_argv() -> tuple:
    """(argv, display) for the install. Through bash, because the installer
    IS a piped script; nothing from a request reaches it, and `display` —
    cursor_health.INSTALL_COMMAND_POSIX, shown before it runs — is exactly
    what runs."""
    display = cursor_health.install_command()
    return (["bash", "-c", display], display)


def update_argv(path: str) -> tuple:
    """(cmd, display) for the update: the CLI's own updater, on the
    resolved binary `start` already checked."""
    return ([path, "update"], cursor_health.update_command(path))


_lock = threading.Lock()
_state: dict = {"action": None, "state": "idle", "detail": "", "output": "",
                "error": None, "command": None, "started_at": None,
                "finished_at": None}


def _report(snapshot: dict) -> None:
    """Mirror one record into the job registry. Never raises."""
    try:
        state = snapshot["state"]
        jobs.upsert({
            "id": JOB_ID,
            "title": ("Installing Cursor CLI" if snapshot["action"] == "install"
                      else "Updating Cursor CLI"),
            "kind": "task",
            "state": (jobs.RUNNING if state == "running"
                      else "error" if state == "error" else "done"),
            "detail": snapshot["detail"],
            "message": snapshot["error"] or "",
            "origin": "Cursor CLI setup",
        }, page="/preferences", server=True)
    except Exception:  # noqa: BLE001 - reporting must never break the work
        logger.debug("could not report the Cursor CLI %s job", snapshot["action"])


def _publish(**fields) -> None:
    with _lock:
        _state.update(fields)
        snapshot = dict(_state)
    _report(snapshot)


def status() -> dict:
    with _lock:
        return dict(_state)


def running() -> bool:
    with _lock:
        return _state["state"] == "running"


def _run(action: str, cmd, display: str) -> None:
    """The worker body: spawn, drain, re-probe. Never raises out of the thread."""
    tail: list = []
    timed_out = threading.Event()
    _publish(detail="Downloading the package" if action == "install"
             else "Checking for updates")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=cursor_health._child_env(),
            **SUBPROCESS_KWARGS,
        )
    except (OSError, ValueError) as exc:
        _publish(state="error", error=f"could not start the {action}: {exc}",
                 finished_at=time.time())
        return

    # A watchdog, not a deadline between lines — a download can sit silent
    # for a while (claude_install._run has the full story).
    def _expire() -> None:
        timed_out.set()
        try:
            proc.kill()
        except OSError:
            pass

    watchdog = threading.Timer(TIMEOUT_S, _expire)
    watchdog.daemon = True
    watchdog.start()

    try:
        for line in proc.stdout or ():
            line = line.rstrip("\n")
            if line.strip():
                tail.append(line)
                del tail[:-_OUTPUT_TAIL_LINES]
                _publish(detail=line[:200], output="\n".join(tail))
        code = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _expire()
        code = -1
    except OSError as exc:
        _publish(state="error", finished_at=time.time(), output="\n".join(tail),
                 error=f"the {action} failed while running: {exc}")
        return
    finally:
        watchdog.cancel()

    if timed_out.is_set():
        _publish(state="error", finished_at=time.time(), output="\n".join(tail),
                 error=f"the {action} took longer than {TIMEOUT_S // 60} minutes "
                       "and was stopped")
        return

    text = "\n".join(tail)
    if code != 0:
        _publish(state="error", finished_at=time.time(), output=text,
                 error=f"`{display}` exited with code {code}")
        return

    # THE RE-PROBE IS THE POINT: it finds the fresh binary (~/.local/bin is
    # on the augmented PATH) and re-adopts a shell-only one.
    health = None
    try:
        health = cursor_health.summary_refreshed()
    except Exception:  # noqa: BLE001 - a probe that failed is not a failed install
        logger.warning("Cursor CLI %s finished but the re-probe failed", action)

    if health is not None and not health.get("found"):
        _publish(state="error", finished_at=time.time(), output=text,
                 error=f"the {action} reported success, but Cursor CLI still "
                       "cannot be found on this machine")
        return
    _publish(state="done", finished_at=time.time(), output=text,
             detail=("Cursor CLI " + ((health or {}).get("version") or "")).strip()
                    if health else "Finished")


def start(action: str = "install") -> dict:
    """Kick off `action` and return the opening record. Refuses rather than
    queues; the real guard is the second check inside the claiming lock."""
    if action not in ACTIONS:
        raise InstallError(f"action must be one of {', '.join(ACTIONS)}")
    with _lock:
        if _state["state"] == "running":
            raise InstallError(
                f"a Cursor CLI {_state['action']} is already running")

    if action == "install":
        argv, display = install_argv()
    else:
        path, _source = cursor_health.resolve(allow_shell=False)
        if not path or not cursor_health.executable(path):
            raise InstallError(
                "there is no Cursor CLI on this machine to update — install it first")
        plan = cursor_health.update_plan(cursor_health.install_method(path))
        if plan["updatable"] is False:
            raise InstallError(
                f"`cursor-agent update` would not change this copy — {plan['reason']}."
                + (f" Run `{plan['command']}` instead." if plan["command"] else ""))
        argv, display = update_argv(path)

    with _lock:
        if _state["state"] == "running":
            raise InstallError(
                f"a Cursor CLI {_state['action']} is already running")
        _state.update(action=action, state="running", detail="Starting…",
                      output="", error=None, command=display,
                      started_at=time.time(), finished_at=None)
        claimed = dict(_state)
    _report(claimed)

    def _guarded() -> None:
        try:
            _run(action, argv, display)
        except BaseException as exc:  # noqa: BLE001 - a stuck record is worse
            logger.exception("the Cursor CLI %s worker died", action)
            _publish(state="error", finished_at=time.time(),
                     error=f"the {action} stopped unexpectedly: {exc}")

    threading.Thread(target=_guarded, daemon=True,
                     name=f"cursor-{action}").start()
    return claimed


def reset() -> None:
    """Test seam — the record is module state and suites share a module."""
    with _lock:
        _state.update({"action": None, "state": "idle", "detail": "", "output": "",
                       "error": None, "command": None, "started_at": None,
                       "finished_at": None})
