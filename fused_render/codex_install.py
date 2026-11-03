"""Running the Codex CLI install, and its update, on the user's behalf.

The Codex twin of `copilot_install.py`: one record, two actions, a thread with
a watchdog, a verbatim output tail, single-flight, and a re-probe on success.
`claude_install.py`'s docstring carries the argument for each; this one differs
in the command line — `npm install -g @openai/codex` for both actions, since
`npm install -g` with no version installs the `latest` tag over whatever is
there — and in having ITS OWN slot, for the reason copilot_install gives.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time

from fused_render import codex_health, jobs
from fused_render.claude_health import SUBPROCESS_KWARGS

logger = logging.getLogger(__name__)

#: Its own job id — never the Claude or Copilot one, or the download manager
#: would show one row flipping between products.
JOB_ID = "sys:codex-install"
TIMEOUT_S = 600
_OUTPUT_TAIL_LINES = 40
ACTIONS = ("install", "update")


class InstallError(RuntimeError):
    """A refusal the caller should turn into a 4xx with this text in it."""


def _npm() -> str:
    """The `npm` to run, resolved on the augmented PATH — a Dock-launched
    app's PATH rarely has the Node prefix on it. Refused up front when there
    is none: "npm: command not found" after a spinner is worse than saying
    what is missing before anything runs."""
    found = shutil.which("npm", path=codex_health.augmented_path())
    if not found:
        raise InstallError("Codex CLI installs through npm, and there is no npm "
                           "on this machine — install Node.js first")
    return found


def install_argv() -> tuple:
    """(argv, display) for the install. No shell: the argv is the resolved
    npm plus the three fixed words of codex_health.INSTALL_COMMAND_POSIX, and
    nothing from a request reaches it; `display` is that line, shown before
    it runs."""
    display = codex_health.install_command()
    return ([_npm(), *display.split()[1:]], display)


def update_argv(path: str) -> tuple:
    """(cmd, display) for the update — the same npm line (D10). `path` is
    the resolved binary, already checked by `start`; npm finds the package
    itself."""
    return install_argv()


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
            "title": ("Installing Codex CLI" if snapshot["action"] == "install"
                      else "Updating Codex CLI"),
            "kind": "task",
            "state": (jobs.RUNNING if state == "running"
                      else "error" if state == "error" else "done"),
            "detail": snapshot["detail"],
            "message": snapshot["error"] or "",
            "origin": "Codex setup",
        }, page="/preferences", server=True)
    except Exception:  # noqa: BLE001 - reporting must never break the work
        logger.debug("could not report the Codex CLI %s job", snapshot["action"])


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
            env=codex_health._child_env(),
            **SUBPROCESS_KWARGS,
        )
    except (OSError, ValueError) as exc:
        _publish(state="error", error=f"could not start the {action}: {exc}",
                 finished_at=time.time())
        return

    # A watchdog, not a deadline between lines — npm can sit silent through a
    # slow registry fetch (claude_install._run has the full story).
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

    # THE RE-PROBE IS THE POINT: it finds the fresh binary (the npm prefix's
    # bin is on the augmented PATH) and re-adopts a shell-only one.
    health = None
    try:
        health = codex_health.summary_refreshed()
    except Exception:  # noqa: BLE001 - a probe that failed is not a failed install
        logger.warning("Codex CLI %s finished but the re-probe failed", action)

    if health is not None and not health.get("found"):
        _publish(state="error", finished_at=time.time(), output=text,
                 error=f"the {action} reported success, but Codex CLI still "
                       "cannot be found on this machine")
        return
    _publish(state="done", finished_at=time.time(), output=text,
             detail=("Codex CLI " + ((health or {}).get("version") or "")).strip()
                    if health else "Finished")


def start(action: str = "install") -> dict:
    """Kick off `action` and return the opening record. Refuses rather than
    queues; the real guard is the second check inside the claiming lock."""
    if action not in ACTIONS:
        raise InstallError(f"action must be one of {', '.join(ACTIONS)}")
    with _lock:
        if _state["state"] == "running":
            raise InstallError(
                f"a Codex CLI {_state['action']} is already running")

    if action == "install":
        argv, display = install_argv()
    else:
        path, _source = codex_health.resolve(allow_shell=False)
        if not path or not codex_health.executable(path):
            raise InstallError(
                "there is no Codex CLI on this machine to update — install it first")
        plan = codex_health.update_plan(codex_health.install_method(path))
        if plan["updatable"] is False:
            raise InstallError(
                f"an npm update would not change this copy — {plan['reason']}."
                + (f" Run `{plan['command']}` instead." if plan["command"] else ""))
        argv, display = update_argv(path)

    with _lock:
        if _state["state"] == "running":
            raise InstallError(
                f"a Codex CLI {_state['action']} is already running")
        _state.update(action=action, state="running", detail="Starting…",
                      output="", error=None, command=display,
                      started_at=time.time(), finished_at=None)
        claimed = dict(_state)
    _report(claimed)

    def _guarded() -> None:
        try:
            _run(action, argv, display)
        except BaseException as exc:  # noqa: BLE001 - a stuck record is worse
            logger.exception("the Codex CLI %s worker died", action)
            _publish(state="error", finished_at=time.time(),
                     error=f"the {action} stopped unexpectedly: {exc}")

    threading.Thread(target=_guarded, daemon=True,
                     name=f"codex-{action}").start()
    return claimed


def reset() -> None:
    """Test seam — the record is module state and suites share a module."""
    with _lock:
        _state.update({"action": None, "state": "idle", "detail": "", "output": "",
                       "error": None, "command": None, "started_at": None,
                       "finished_at": None})
