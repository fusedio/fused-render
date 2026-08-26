"""Running the Claude Code installer, and `claude update`, on the user's behalf.

`claude_health.py` is the other half of this and came first: it establishes what
is wrong with Claude Code on this machine, carefully and without ever guessing.
Every one of its findings then ended the same way — a sentence telling the user
to open a terminal and type something. On a machine where the app was launched
from the Dock, that is the app knowing the answer and asking the user to go and
apply it somewhere else.

This module applies two of them.

**Why a module and not two `subprocess.run` calls in a route.** Both actions are
minutes long, both replace a binary the rest of the app spawns, and both have a
failure surface that is the whole point of running them (a 403 from
downloads.claude.ai and a proxy blocking TLS are different problems with
different fixes, and a generic "install failed" throws both away). So they need
progress, a verbatim error, single-flight, and a re-probe on success. That is a
state machine, not a call.

**One record, two actions.** `install` and `update` differ only in the command
line: they are both "run something that changes the CLI on disk, then re-measure
health". Keeping one record means the UI has one thing to poll and there is one
place where "is something already running?" is answered.

**A thread, not a detached worker.** `envinstall.py` detaches, because a `uv
sync` can run for many minutes and must survive the page that started it. This
is shorter and, more importantly, its whole value is the re-probe at the end —
which has to happen in the server process, because that is where the health
cache and the `FUSED_RENDER_CLAUDE_BIN` adoption live. A detached child would
finish and have nobody to tell.

It reports into `jobs.py` as well, so the work stays visible in the download
manager after the user navigates away from the strip that started it.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Optional

from fused_render import claude_health, jobs

logger = logging.getLogger(__name__)

#: The job id in the shared registry. A `sys:` id, because the server runs this
#: work — a page may not post progress for it (jobs.SERVER_ID_PREFIX).
JOB_ID = "sys:claude-install"

#: Hard ceiling on either action. The native installer downloads one binary and
#: is normally well under a minute; ten minutes is "the network is gone and
#: nobody is coming", not a budget anyone should reach.
TIMEOUT_S = 600

#: How much of the child's output we keep. The tail is what carries the error —
#: curl's exit code, uv-style resolver complaints, a proxy's HTML — and the head
#: is rarely interesting, so a bounded tail is the right thing to hold.
_OUTPUT_TAIL_LINES = 40

ACTIONS = ("install", "update")


class InstallError(RuntimeError):
    """A refusal the caller should turn into a 4xx with this text in it."""


def install_argv() -> tuple:
    """(argv, display) for the native installer on this platform.

    NOT `shell=True` with a user-visible string. The command is a fixed literal
    of ours either way, but the argv form is what makes that legible at the call
    site: there is exactly one string here that a shell ever parses, it is
    spelled in this module, and nothing from a request reaches it.

    `display` is what we SHOW before running — the line the docs give and the
    one `claude_health.install_command()` publishes to the UI, so what the user
    is told will run and what runs are the same sentence.
    """
    display = claude_health.install_command()
    if os.name == "nt":
        # -NoProfile so a user's PowerShell profile cannot change what this
        # does; -ExecutionPolicy Bypass because the default policy blocks a
        # piped script and this is the vendor's own documented one-liner.
        return (
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", "irm https://claude.ai/install.ps1 | iex"],
            display,
        )
    return (["bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"], display)


def update_argv(path: str) -> tuple:
    """(cmd, display) for `claude update` on an already-resolved binary.

    Takes the resolved path rather than the bare name so this updates the CLI
    the app actually spawns — on a machine with two installs, `claude` on the
    PATH and the one we resolved are not always the same file, and updating the
    other one would leave the app on the old version reporting success.

    THROUGH `_probe_cmd`, WHICH IS NOT A DETAIL ON WINDOWS. npm installs `claude`
    as a `.cmd` shim, which `CreateProcess` cannot run — only cmd.exe can — and
    npm is one of the two methods `update_plan` calls self-updating, so this is
    precisely the install type the Update button is offered for. Spawning the
    shim as a raw argv list raises OSError on every Windows npm install, i.e.
    the button would be broken for the users it exists to serve. `claude_health`
    already solved this for its own probes; reusing it is what keeps the spawn
    and the probe agreeing about how to start the same file.

    So `cmd` is an argv LIST normally and one command STRING behind a shim; the
    caller passes `shell=` accordingly.
    """
    return (claude_health._probe_cmd(path, "update"), "claude update")


# --- the single record --------------------------------------------------------

_lock = threading.Lock()
_state: dict = {"action": None, "state": "idle", "detail": "", "output": "",
                "error": None, "command": None, "started_at": None,
                "finished_at": None}


def _report(snapshot: dict) -> None:
    """Mirror one record into the job registry. Never raises.

    Takes an already-taken snapshot rather than reading `_state` itself, so it
    can be called from inside and outside the lock — `start` claims the slot and
    reports the claim it made, not whatever the record says a moment later.
    """
    try:
        state = snapshot["state"]
        jobs.upsert({
            "id": JOB_ID,
            "title": ("Installing Claude Code" if snapshot["action"] == "install"
                      else "Updating Claude Code"),
            "kind": "task",
            "state": (jobs.RUNNING if state == "running"
                      else "error" if state == "error" else "done"),
            "detail": snapshot["detail"],
            "message": snapshot["error"] or "",
        }, server=True)
    except Exception:  # noqa: BLE001 - reporting must never break the work
        logger.debug("could not report the Claude Code %s job", snapshot["action"])


def _publish(**fields) -> None:
    """Update the record and mirror it into the job registry. Never raises."""
    with _lock:
        _state.update(fields)
        snapshot = dict(_state)
    _report(snapshot)


def status() -> dict:
    """The record as the endpoint answers it.

    `output` is the child's own words, verbatim and untranslated. That is the
    entire reason this is worth surfacing: "curl: (22) 403" and "Failure writing
    output to destination" are documented failures with their own documented
    fixes, and a generic message would leave the user exactly where they were.
    """
    with _lock:
        return dict(_state)


def running() -> bool:
    with _lock:
        return _state["state"] == "running"


def _child_env() -> dict:
    """The environment for the installer or updater.

    Two edits to our own. **PYTHONHOME/PYTHONPATH are stripped**: the packaged
    app runs a bundled interpreter and exports both, and a child that inherits
    them and is not that interpreter dies before it starts. **PATH is
    augmented** with the known install dirs, for the same reason every probe in
    `claude_health` runs that way — the CLI is a program that has to find its own
    runtime, and a GUI-launched app's stripped PATH is as short of `node` as it
    is of `claude`.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONHOME", "PYTHONPATH")}
    env["PATH"] = claude_health.augmented_path()
    return env


def _run(action: str, cmd, display: str) -> None:
    """The worker body: spawn, drain, re-probe. Never raises out of the thread.

    `cmd` is an argv list, or — behind a Windows `.cmd` shim — one command
    string for the cmd.exe hop (see `update_argv`). `shell=` follows from which.
    """
    tail: list = []
    timed_out = threading.Event()
    try:
        proc = subprocess.Popen(
            cmd,
            shell=isinstance(cmd, str),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=_child_env(),
            **claude_health.SUBPROCESS_KWARGS,
        )
    except (OSError, ValueError) as exc:
        _publish(state="error", error=f"could not start the {action}: {exc}",
                 finished_at=time.time())
        return

    # A WATCHDOG, NOT A DEADLINE CHECKED BETWEEN LINES. The check used to live
    # inside the drain loop, which meant it could only fire when the child said
    # something — and `curl -fsSL` is silent by construction. A download that
    # stalled produced no line, so the loop blocked in `read()` forever, the
    # timeout never ran, and the record stayed `running` for the life of the
    # process, refusing every later install and update. The kill has to come
    # from a timer that is not waiting on the child's own output.
    def _expire() -> None:
        timed_out.set()
        try:
            proc.kill()  # unblocks the read below, which then sees EOF
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

    # THE RE-PROBE IS THE POINT. A forced re-measure drops the cached snapshot,
    # finds the freshly installed binary (its directory is already first in
    # `claude_health`'s candidate list, so no PATH change and no restart is
    # needed) and re-adopts it if only a login shell can see it. Without this the
    # app would sit on a 60-second-old "not installed" until something else
    # happened to refresh.
    health = None
    try:
        health = claude_health.summary_refreshed()
    except Exception:  # noqa: BLE001 - a probe that failed is not a failed install
        logger.warning("Claude Code %s finished but the re-probe failed", action)

    # An install that ran cleanly and left nothing runnable behind is a FAILURE,
    # however happy its exit code was. Reporting success here and letting the
    # strip re-render the same "can't find Claude Code" card would be the app
    # telling the user two contradictory things in the same second.
    if health is not None and not health.get("found"):
        _publish(state="error", finished_at=time.time(), output=text,
                 error=f"the {action} reported success, but Claude Code still "
                       "cannot be found on this machine")
        return
    _publish(state="done", finished_at=time.time(), output=text,
             detail=("Claude Code " + (health or {}).get("version", "")).strip()
                    if health else "Finished")


def start(action: str = "install") -> dict:
    """Kick off `action` and return the opening record.

    Refuses rather than queues. Two installers writing the same files at once is
    not a race worth surviving, and the honest answer to "install again while an
    install is running" is that one already is.
    """
    if action not in ACTIONS:
        raise InstallError(f"action must be one of {', '.join(ACTIONS)}")
    # An early, friendly refusal. NOT the guard — see the claim below.
    with _lock:
        if _state["state"] == "running":
            raise InstallError(
                f"a Claude Code {_state['action']} is already running")

    if action == "install":
        argv, display = install_argv()
    else:
        # Resolved WITHOUT the login-shell probe: this runs on a request, and
        # sourcing the user's whole profile here would add seconds to a button
        # press. Anything the shell probe would have found has already been
        # adopted into the override by the health measure that got us here.
        path, _source = claude_health.resolve(allow_shell=False)
        if not path or not claude_health.executable(path):
            raise InstallError(
                "there is no Claude Code on this machine to update — install it first")
        plan = claude_health.update_plan(
            claude_health.install_method(path, None))
        # THE GUARD THE FEATURE EXISTS FOR. A Homebrew/WinGet/apt install answers
        # `claude update` with "Claude is up to date!" and changes nothing, so
        # running it would spend a minute to tell the user their old CLI is
        # current. Refuse, and say which command actually would work.
        if plan["updatable"] is False:
            raise InstallError(
                f"`claude update` would not change anything here — {plan['reason']}."
                + (f" Run `{plan['command']}` instead." if plan["command"] else ""))
        argv, display = update_argv(path)

    # THE ACTUAL GUARD, and it has to be one operation. The check above releases
    # the lock before the resolve/plan work above, so two concurrent
    # POST /api/claude/install calls — which FastAPI runs on a threadpool, so
    # genuinely at once — could both pass it and both spawn an installer over
    # the same files. Re-testing the state in the SAME critical section that
    # claims it is what makes "one at a time" true rather than likely.
    with _lock:
        if _state["state"] == "running":
            raise InstallError(
                f"a Claude Code {_state['action']} is already running")
        _state.update(action=action, state="running", detail="Starting…",
                      output="", error=None, command=display,
                      started_at=time.time(), finished_at=None)
        claimed = dict(_state)
    _report(claimed)  # mirrored to the job registry outside the lock

    # A CATCH-ALL AROUND THE WHOLE BODY. The slot is claimed now, so any escape
    # from `_run` that did not publish a terminal state would leave the record
    # `running` forever and refuse every later install — the same permanent
    # wedge the watchdog exists to prevent, arriving by a different route.
    def _guarded() -> None:
        try:
            _run(action, argv, display)
        except BaseException as exc:  # noqa: BLE001 - a stuck record is worse
            logger.exception("the Claude Code %s worker died", action)
            _publish(state="error", finished_at=time.time(),
                     error=f"the {action} stopped unexpectedly: {exc}")

    threading.Thread(target=_guarded, daemon=True,
                     name=f"claude-{action}").start()
    return claimed


def reset() -> None:
    """Test seam — the record is module state and suites share a module."""
    with _lock:
        _state.update({"action": None, "state": "idle", "detail": "", "output": "",
                       "error": None, "command": None, "started_at": None,
                       "finished_at": None})
