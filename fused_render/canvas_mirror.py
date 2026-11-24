"""Reference canvases: run `fused canvas mirror` in the background (D1477).

`fused workbench canvas mirror --output <env root>` pulls every canvas the
signed-in account can reach — its own, its team's, the public catalog — into
`~/Fused/canvas/<env>/{user,team,public}/` as read-only reference copies an
agent greps for examples. The command owns those copies (their marker says
`sync: "mirror"`, canvas_marker.py); it never touches a folder fused-render
marked `live`, nor one with no marker. This module only RUNS it and reports
what it said. It decides nothing about any folder.

WHEN. A run per environment, never two at once: about `STARTUP_DELAY_S` after
the server starts, after a sign-in completes, every `INTERVAL_S` (jittered),
and on demand (`POST /api/canvases/mirror/run`). The scheduled ones skip an
environment whose last good run is younger than the interval, so restarts and
dev reloads cost nothing, and skip an environment nobody uses (`eligible`):
prod and unstable share one sign-in, and mirroring both for everyone would
double every download.

WHAT A RUN IS. The clones still at the pre-D1476 path are moved first
(`canvas_layout.ensure_layout`) and the three scope folders are created, then
the CLI runs as a child this module owns. Its stderr carries one progress
line per canvas (`[12/340] pulled public/Some_Canvas`), its stdout one JSON
document. The outcome is read off the exit, never guessed beforehand:

* exit 0 and a document — `ok`;
* exit 1 and a document — `partial`: a scope's listing failed or its
  removals were not confirmed, or some canvas failed. The counts are kept;
  nothing is assumed removed;
* exit 2 and click's "No such command" — `unsupported`: the bundled `fused`
  predates the command. Remembered for this CLI for the life of the process;
  nothing is spawned again and the timer stops. Everything else works;
* the CLI's "Another canvas mirror run is using" — `busy`, not an error;
* its re-authenticate message — `auth`;
* anything else — `failed`, with the tail of stderr.

The last result per environment is kept in `<env>/.sync/.mirror.json`.

HOLDS. Cloning a canvas whose folder is a reference copy, renaming and
deleting all change `user/` under the command's feet. `hold(env)` stops this
process's run for that environment (and waits until the child is gone),
keeps new ones from starting, and starts one again afterwards if it had to
stop one. A run in ANOTHER process is not ours to stop: against that one the
caller marks the folder live first, and the command re-reads the marker
before every destructive step.

A child this module had to kill cannot remove its own lock
(`<root>/.mirror.lock`), which would read as `busy` for an hour. The lock
names its holder's pid; when that is the child just reaped, it is removed.

Nothing here runs unless something starts it: the timer only from
`start()` (the server's startup hook, which lean `open` processes and bare
`create_app()` never run), a run only from a kick. Imports and answers
`no_cli` with no `fused` installed.

No import of anything under fused_render.server (the server includes this
router). `canvases` is imported here and imports this module lazily.
"""
from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import logging
import os
import random
import re
import signal
import subprocess
import threading
import time

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render import canvas_layout, canvas_marker, canvases
from fused_render.fusedcli import (
    CUSTOM_WORKBENCH_ENV,
    WORKBENCH_ENV_CHOICES,
    _installed_fused_version,
    cli_error,
    workbench_env,
    workbench_env_locked,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: After the server binds, before the first scheduled run.
STARTUP_DELAY_S = 30.0
#: Between scheduled runs, and the age under which one is skipped.
INTERVAL_S = 30 * 60.0
_JITTER = 0.1
#: A run is killed past this. Below the command's own one-hour lock
#: staleness, so a wedged child of ours never looks like a live one to it.
RUN_TIMEOUT_S = 45 * 60.0
#: Interrupt, then terminate, then kill: how long each is given.
STOP_GRACE_S = 1.5
#: How long `hold` waits for a stopped run to be gone before going on.
HOLD_WAIT_S = 10.0
#: Dot-prefixed on purpose: `.sync/` holds one `<canvas name>.json` per clone
#: (its merge base), and a canvas may be named `mirror`. No canvas name
#: starts with a dot.
STATUS_NAME = ".mirror.json"
LOCK_NAME = ".mirror.lock"
LOCK_PID_FIELD = "pid"
_STATUS_VERSION = 1
_STDERR_TAIL = 20

STATES = ("idle", "running", "ok", "partial", "failed", "unsupported", "auth",
          "no_cli", "busy")
#: A last result this fresh needs no scheduled re-run.
_GOOD_STATES = ("ok", "partial")
COUNT_KEYS = ("pulled", "updated", "unchanged", "removed", "live", "unmanaged", "failed")

# What this module copies from the CLI, in one place (a test checks each
# against the installed `fused` when it has the command): the progress line
# (`[12/340] pulled public/Some_Canvas`), the words of its "another run holds
# this root" error, click's unknown-command error and exit code, and the exit
# code of a partial result.
_PROGRESS_RE = re.compile(r"^\[(\d+)/(\d+)\]\s+(\S+)\s+(.+)$")
BUSY_NEEDLE = "another canvas mirror run is using"
NO_COMMAND_NEEDLE = "no such command"
EXIT_PARTIAL = 1
EXIT_USAGE = 2


def _zero_counts() -> dict:
    return {k: 0 for k in COUNT_KEYS}


@dataclasses.dataclass
class MirrorStatus:
    """One environment's mirror state: the last finished run, and the one in
    flight when `state` is "running"."""

    env: str
    state: str = "idle"
    #: "startup" | "login" | "timer" | "manual" | "hold" — what started it.
    trigger: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    #: Progress of the run in flight (`[done/total]`), 0 otherwise.
    done: int = 0
    total: int = 0
    last_line: str | None = None
    #: The last finished run's counts, per action.
    counts: dict = dataclasses.field(default_factory=_zero_counts)
    #: The last finished run's listings: scope -> {ok, unavailable, listed, error}.
    scopes: dict = dataclasses.field(default_factory=dict)
    error: str | None = None
    #: Clones (`user/<name>`, marked live) whose canvas is in no listing:
    #: deleted on the server, or no longer this account's. Reported only.
    live_orphans: list = dataclasses.field(default_factory=list)

    def mirrored(self) -> int:
        """Reference copies on disk after the last finished run."""
        return sum(int(self.counts.get(k) or 0) for k in ("pulled", "updated", "unchanged"))


@dataclasses.dataclass
class _Run:
    env: str
    trigger: str
    started_at: float
    proc: subprocess.Popen | None = None
    done: int = 0
    total: int = 0
    last_line: str | None = None
    #: Why this run is being stopped ("hold", "logout", "shutdown"), or None.
    cancelled: str | None = None
    finished: threading.Event = dataclasses.field(default_factory=threading.Event)
    #: Serialises `_clear_run_lock` for this run: its own thread and
    #: `shutdown` may both clean up after one child.
    lock_cleanup: threading.Lock = dataclasses.field(default_factory=threading.Lock)


_LOCK = threading.RLock()
_runs: dict[str, _Run] = {}
_holds: dict[str, int] = {}
#: Environments whose run a hold stopped (or refused): kicked when it ends.
_rekick: set[str] = set()
#: (CLI command, installed fused version) known not to have `canvas mirror`.
_unsupported: set[tuple] = set()
#: Environments whose catalog was listed since the process started.
_listed: set[str] = set()
#: status file path -> last finished result (loaded once, kept current).
_last: dict[str, MirrorStatus] = {}
#: status file path -> the file's `_file_sig` when `_last` was last set from
#: or written to it. A file that changed since was written by ANOTHER process
#: (its run finished after ours said `busy`): `_load` reads it again.
_last_sig: dict[str, tuple | None] = {}
_started = False
_latched = False
_timer_stop: threading.Event | None = None


# -- what is known without running anything ------------------------------------


def _root(env: str) -> str:
    return canvases.canvases_root(env)


def _status_path(env: str) -> str:
    return os.path.join(_root(env), ".sync", STATUS_NAME)


@functools.lru_cache(maxsize=1)
def _fused_version() -> str | None:
    """The installed `fused` distribution's version, read once: it does not
    change under a running process, and the status poll asks every time."""
    return _installed_fused_version()


def _cli_key(cli) -> tuple:
    return (tuple(cli.command), _fused_version())


def _is_unsupported(cli) -> bool:
    with _LOCK:
        return _cli_key(cli) in _unsupported


def _clean_scopes(raw) -> dict:
    out = {}
    for scope in canvas_marker.SCOPES:
        entry = raw.get(scope) if isinstance(raw, dict) else None
        if not isinstance(entry, dict):
            continue
        listed, error = entry.get("listed"), entry.get("error")
        out[scope] = {
            "ok": entry.get("ok") is True,
            "unavailable": entry.get("unavailable") is True,
            "listed": listed if isinstance(listed, int) and not isinstance(listed, bool) else 0,
            "error": error if isinstance(error, str) and error else None,
        }
    return out


def _clean_counts(raw) -> dict:
    counts = _zero_counts()
    if isinstance(raw, dict):
        for key in COUNT_KEYS:
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                counts[key] = value
    return counts


def _file_sig(path: str) -> tuple | None:
    """What tells one version of the status file from another, or None."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _load(env: str) -> MirrorStatus:
    """The last finished result for `env`: memory while the status file is
    as this process last saw it, else the status file, else a blank "idle".
    Never raises."""
    path = _status_path(env)
    sig = _file_sig(path)
    with _LOCK:
        cached = _last.get(path)
        if cached is not None and _last_sig.get(path) == sig:
            return cached
    status = MirrorStatus(env=env)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and data.get("version") == _STATUS_VERSION:
        state = data.get("state")
        if state in STATES and state != "running":
            status.state = state
        for key in ("trigger", "last_line", "error"):
            if isinstance(data.get(key), str):
                setattr(status, key, data[key])
        for key in ("started_at", "finished_at"):
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(status, key, float(value))
        status.counts = _clean_counts(data.get("counts"))
        status.scopes = _clean_scopes(data.get("scopes"))
        orphans = data.get("live_orphans")
        if isinstance(orphans, list):
            status.live_orphans = [o for o in orphans if isinstance(o, str)]
    elif cached is not None:
        # Gone or unreadable since (a status that could not be written, a
        # torn read): what this process knows stands.
        status = cached
    with _LOCK:
        if _last.get(path) is not cached:
            return _last[path]  # a run of ours recorded a result meanwhile
        _last[path] = status
        _last_sig[path] = sig
        return status


def _save(status: MirrorStatus, *, persist: bool = True) -> None:
    """Keep `status` as `env`'s last result, in memory and (`persist`) on
    disk (atomic; a file that cannot be written leaves the memory copy)."""
    path = _status_path(status.env)
    with _LOCK:
        _last[path] = status
        _last_sig[path] = _file_sig(path)
    if not persist:
        return
    record = {"version": _STATUS_VERSION, **dataclasses.asdict(status)}
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
    with _LOCK:
        if _last.get(path) is status:
            _last_sig[path] = _file_sig(path)


def note_listed(env: str) -> None:
    """The catalog of `env` was just listed: the environment is in use."""
    with _LOCK:
        _listed.add(env)


def _has_clone(env: str) -> bool:
    """Whether `env` holds at least one CLONE. Not any folder under `user/`:
    the command itself fills that with reference copies of the account's own
    canvases, and one run would then keep the environment "in use" for good."""
    try:
        names = os.listdir(canvases.clones_root(env))
    except OSError:
        return False
    return any(canvases._NAME_RE.match(n) and canvases._is_live_clone(env, n) for n in names)


def eligible(env: str, *, scheduled: bool = True) -> tuple[bool, str | None]:
    """`(True, None)` when a run for `env` may start, else `(False, why)`.

    Always: a CLI that has the command, a sign-in for the environment, a
    configured settings file for `custom`, and the environment not locked
    out by a FUSED_RENDER_WORKBENCH_ENV pin. `scheduled` runs also need the
    environment to be IN USE — it is the default one, or holds a clone, or
    its catalog was listed this session."""
    cli = canvases.fused_cli()
    if cli is None:
        return False, "no_cli"
    if _is_unsupported(cli):
        return False, "unsupported"
    if env not in WORKBENCH_ENV_CHOICES:
        return False, "unknown_env"
    if workbench_env_locked() and env != workbench_env():
        return False, "env_locked"
    if env == CUSTOM_WORKBENCH_ENV and not canvases.fused_settings_configured():
        return False, "not_configured"
    if not canvases._logged_in(env):
        return False, "signed_out"
    if scheduled:
        with _LOCK:
            listed = env in _listed
        if not (env == workbench_env() or listed or _has_clone(env)):
            return False, "not_in_use"
    return True, None


def eligible_envs() -> list[str]:
    """The environments a scheduled run would cover right now."""
    return [env for env in WORKBENCH_ENV_CHOICES if eligible(env)[0]]


def status(env: str) -> dict:
    """`env`'s mirror status for the page — see `MirrorStatus`, plus:
    `mirrored` (reference copies on disk after the last run), `last_state`
    (the last FINISHED run's state while one is running), `root`, `held`,
    `eligible`/`skip_reason` (whether a scheduled run would start), and
    `layout` (`canvas_layout.pending`: clones the move to `user/` could not
    make — `conflicts` are stranded until the user clears the way).
    Reads memory or one small file; never spawns."""
    last = _load(env)
    out = dataclasses.asdict(last)
    out["last_state"] = last.state
    out["done"], out["total"] = 0, 0
    ok, why = eligible(env)
    if why == "no_cli":
        out["state"] = out["last_state"] = "no_cli"
    elif why == "unsupported":
        out["state"] = out["last_state"] = "unsupported"
    with _LOCK:
        run = _runs.get(env)
        held = _holds.get(env, 0) > 0
    if run is not None and not run.finished.is_set():
        out.update(state="running", trigger=run.trigger, started_at=run.started_at,
                   done=run.done, total=run.total, last_line=run.last_line)
    out.update(mirrored=last.mirrored(), root=_root(env), held=held,
               eligible=ok, skip_reason=why, layout=canvas_layout.pending(env))
    return out


def summary(env: str) -> dict:
    """The compact form `/api/canvases/status` carries per environment."""
    full = status(env)
    return {k: full[k] for k in ("state", "last_state", "done", "total", "finished_at",
                                 "mirrored", "error", "eligible", "skip_reason")}


# -- one run -------------------------------------------------------------------


def _argv(cli, env: str) -> list[str]:
    return [*cli.command, "workbench", "--format", "json", "canvas", "mirror",
            "--output", _root(env)]


_POSIX = os.name != "nt"


def _spawn_kwargs() -> dict:
    """The child gets a process group of its own (POSIX): the CLI may be a
    launcher (`uv run fused`, a shell wrapper) whose child is the command,
    and a signal sent to the launcher alone leaves the command running, with
    the root's lock. `_signal` then reaches the whole group.

    Windows has no such group here: a stop reaches the direct child only, so
    the command behind a FUSED_RENDER_FUSED_BIN launcher runs to its own end
    (D1477; the packaged CLI is the direct child, and Windows is not a
    supported desktop platform)."""
    return {"start_new_session": True} if _POSIX else {}


def _signal(proc: subprocess.Popen, sig: int | None) -> None:
    """Send `sig` to the child's whole process group (POSIX), or terminate /
    kill the child itself (Windows; `sig` None means kill). Never raises."""
    try:
        if _POSIX:
            os.killpg(proc.pid, sig if sig is not None else signal.SIGKILL)
        elif sig is None:
            proc.kill()
        else:
            proc.terminate()
    except (OSError, ValueError):
        pass  # already gone


def _stop_steps() -> list:
    """Interrupt, terminate, kill. An interrupt lets the command unwind and
    drop its lock; the rest is for one that does not."""
    if _POSIX:
        return [signal.SIGINT, signal.SIGTERM, None]
    return [signal.SIGTERM, None]


def _stop_child(proc: subprocess.Popen) -> None:
    """Stop the child and everything it started: each of `_stop_steps` given
    `STOP_GRACE_S`. Never raises."""
    for sig in _stop_steps():
        if proc.poll() is not None:
            break
        _signal(proc, sig)
        try:
            proc.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            continue
    # The launcher may be gone while the command it started is not.
    _signal(proc, None)


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # alive, not ours to signal
    # A killed process nobody has reaped yet still answers the probe.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            return f.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def _read_lock_record(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _clear_run_lock(run: _Run, pid: int) -> None:
    """`_clear_own_lock` for `run`'s child, one call at a time per run: the
    run's own thread and `shutdown` may both clean up after it, and two
    cleanups reading the same stale lock could otherwise remove the one a
    new run took in between."""
    with run.lock_cleanup:
        _clear_own_lock(run.env, pid)


def _clear_own_lock(env: str, pid: int) -> None:
    """After a run of OURS on this root was stopped: remove
    `<root>/.mirror.lock` when it names `pid` (the child), or a process that
    no longer exists (the command behind a launcher, killed with its group).
    It could not release the lock itself, and left there it reads `busy` for
    an hour. A lock whose holder is alive is someone else's run: left alone.
    The file is read again right before it is removed: a lock another run
    took meanwhile is a different record, and stays."""
    path = os.path.join(_root(env), LOCK_NAME)
    record = _read_lock_record(path)
    if record is None:
        return
    holder = record.get(LOCK_PID_FIELD) if isinstance(record, dict) else None
    if not isinstance(holder, int) or isinstance(holder, bool):
        return
    if holder != pid and _POSIX:
        # The group was killed a moment ago; give the kernel that moment.
        deadline = time.monotonic() + 1.0
        while _pid_alive(holder) and time.monotonic() < deadline:
            time.sleep(0.02)
        if _pid_alive(holder):
            return
    elif holder != pid:
        return
    if _read_lock_record(path) != record:
        return  # taken over since: not the lock our child left
    try:
        os.remove(path)
    except OSError:
        pass


def _parse_result(stdout: str) -> dict | None:
    """The command's JSON document, or None. Tolerates noise around it (a
    warning some import printed to stdout)."""
    text = (stdout or "").strip()
    candidates = [text]
    first, last = text.find("{"), text.rfind("}")
    if 0 <= first < last:
        candidates.append(text[first:last + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if (isinstance(data, dict) and isinstance(data.get("scopes"), dict)
                and isinstance(data.get("counts"), dict)):
            return data
    return None


def _needs_login(text: str) -> bool:
    lowered = (text or "").lower()
    return "re-authenticate" in lowered or "refresh your fused credentials" in lowered


def _live_orphans(env: str, result: dict) -> list[str]:
    """Clones whose collection id is in no listing of this run. Only when
    the `user` listing succeeded: a failed listing says nothing."""
    scopes = result.get("scopes")
    user = scopes.get("user") if isinstance(scopes, dict) else None
    if not isinstance(user, dict) or user.get("ok") is not True:
        return []
    listed = {c.get("id") for c in result.get("canvases") or () if isinstance(c, dict)}
    root = canvases.clones_root(env)
    try:
        names = sorted(os.listdir(root), key=str.casefold)
    except OSError:
        return []
    orphans = []
    for name in names:
        if name.startswith("."):
            continue
        marker = canvas_marker.read(os.path.join(root, name))
        if (marker is not None and marker.sync == canvas_marker.SYNC_LIVE
                and marker.id and marker.id not in listed):
            orphans.append(name)
    return orphans


def _partial_error(scopes: dict, counts: dict) -> str | None:
    for scope in canvas_marker.SCOPES:
        entry = scopes.get(scope) or {}
        if entry.get("error") and not entry.get("unavailable"):
            return f"{scope}: {entry['error']}"
    if counts.get("failed"):
        n = counts["failed"]
        return f"{n} canvas{'' if n == 1 else 'es'} could not be pulled"
    return None


def _classify(env: str, run: _Run, cli, returncode: int | None, stdout: str,
              stderr_tail: list[str], timed_out: bool) -> MirrorStatus:
    """The finished run as a status. `returncode` None = it never ran."""
    status_ = MirrorStatus(env=env, trigger=run.trigger, started_at=run.started_at,
                           finished_at=time.time(), last_line=run.last_line)
    previous = _load(env)
    # What the last GOOD run found stays on show under a run that says nothing.
    status_.counts, status_.scopes = dict(previous.counts), dict(previous.scopes)
    status_.live_orphans = list(previous.live_orphans)
    stderr = "\n".join(stderr_tail)
    if timed_out:
        status_.state = "failed"
        status_.error = f"the mirror run was stopped after {int(RUN_TIMEOUT_S // 60)} minutes"
        return status_
    result = _parse_result(stdout)
    if result is not None:
        status_.counts = _clean_counts(result.get("counts"))
        status_.scopes = _clean_scopes(result.get("scopes"))
        status_.live_orphans = _live_orphans(env, result)
        failed = [s for s in status_.scopes.values() if not s["ok"] and not s["unavailable"]]
        if returncode == 0:
            status_.state = "ok"
        elif failed and len(failed) == len([s for s in status_.scopes.values()
                                            if not s["unavailable"]]) and any(
                _needs_login(s["error"] or "") for s in failed):
            status_.state = "auth"
            status_.error = failed[0]["error"]
        else:
            status_.state = "partial"
            status_.error = _partial_error(status_.scopes, status_.counts)
        return status_
    lowered = stderr.lower()
    if returncode == EXIT_USAGE and NO_COMMAND_NEEDLE in lowered:
        with _LOCK:
            _unsupported.add(_cli_key(cli))
        status_.state = "unsupported"
        status_.error = "this version of Fused has no `canvas mirror`; a newer one is needed"
    elif BUSY_NEEDLE in lowered:
        status_.state = "busy"
        status_.error = None
    elif _needs_login(stderr):
        status_.state = "auth"
        status_.error = cli_error(stderr, "sign in to Fused again")
    else:
        status_.state = "failed"
        status_.error = (cli_error(stderr, "fused canvas mirror failed") if stderr.strip()
                         else "fused canvas mirror printed no result"
                         + (f" (exit {returncode})" if returncode is not None else ""))
    return status_


def _read_stderr(proc: subprocess.Popen, run: _Run, tail: list[str]) -> None:
    try:
        for raw in proc.stderr:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            match = _PROGRESS_RE.match(line)
            if match:
                run.done, run.total = int(match.group(1)), int(match.group(2))
                run.last_line = line
            tail.append(line)
            del tail[:-_STDERR_TAIL]
    except (OSError, ValueError):
        pass


def _read_stdout(proc: subprocess.Popen, sink: list[str]) -> None:
    try:
        sink.append(proc.stdout.read())
    except (OSError, ValueError):
        pass


def _begin(env: str, trigger: str, guard: threading.Event | None = None) -> _Run | str:
    """Register a run for `env`, or say why none may start right now.
    `guard` is the timer's stop event: checked under the same lock
    `shutdown` sets it under, so a tick that was already past its own check
    when the server shut down cannot register a run after it."""
    with _LOCK:
        if _latched or (guard is not None and guard.is_set()):
            return "shutting_down"
        current = _runs.get(env)
        if current is not None and not current.finished.is_set():
            return "running"
        if _holds.get(env, 0) > 0:
            _rekick.add(env)
            return "held"
        run = _Run(env=env, trigger=trigger, started_at=time.time())
        _runs[env] = run
        return run


def _execute(run: _Run) -> MirrorStatus:
    """Run the command for `run.env` to its end and record the outcome."""
    env = run.env
    proc = None
    try:
        cli = canvases.fused_cli()
        if cli is None or run.cancelled:
            return _load(env)
        report = canvas_layout.ensure_layout(env)
        # The checks below, the folders made for the command and the spawn all
        # run under the layout's pass lock (not reentrant, so after
        # `ensure_layout`): no pass can move a clone named like a scope folder
        # between reading the layout and creating that folder.
        stdout: list[str] = []
        tail: list[str] = []
        timed_out = False
        with canvas_layout.pass_lock(env):
            # Not while the layout is in the command's way: a clone literally
            # named `user` still sits where it would write its `user/` copies; a
            # moved clone named like a scope folder is still owed a carry from
            # that path, which creating the folder would drop; or the pass
            # failed and nothing is known.
            owed = canvas_layout.owed_scope_names(env)
            # ...or a clone of a canvas literally named `team` or `public` is
            # still at `<env>/team` (its move deferred, or in conflict): the
            # command would write every team canvas INTO the user's clone, and
            # that clone's sync would publish them as edits.
            legacy = [n for n in canvas_layout.legacy_clones(env)
                      if n in canvas_layout.RESERVED_NAMES]
            in_the_way = report.blocked or report.error
            if not in_the_way and legacy:
                in_the_way = (f"canvas {legacy[0]!r} is still cloned at "
                              f"{os.path.join(_root(env), legacy[0])}, where the reference "
                              "canvases go; it has to move to user/ first")
            if not in_the_way and owed:
                in_the_way = (f"the chat history of canvas {owed[0]!r} is still being "
                              "moved to its new folder; trying again shortly")
            if in_the_way:
                result = MirrorStatus(env=env, state="failed", trigger=run.trigger,
                                      started_at=run.started_at, finished_at=time.time(),
                                      error=in_the_way)
                previous = _load(env)
                result.counts, result.scopes = dict(previous.counts), dict(previous.scopes)
                _save(result)
                return result
            # The agent's argv names all three; the command makes only the ones
            # it writes into. Remembered, so a CLI that turns out not to have the
            # command leaves none of them behind.
            made = []
            for folder in (_root(env), *(os.path.join(_root(env), scope)
                                         for scope in canvas_marker.SCOPES)):
                if not os.path.isdir(folder):
                    os.makedirs(folder, exist_ok=True)
                    made.append(folder)
            # The child is spawned under the same lock: a pass that is moving a
            # clone into `user/` right now finishes first, and every pass after
            # this point sees the child and leaves `user/` alone
            # (`child_running`).
            with _LOCK:
                if run.cancelled:
                    return _load(env)
                proc = subprocess.Popen(
                    _argv(cli, env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                    env=canvases._cli_env(cli, env), **_spawn_kwargs())
                run.proc = proc
        readers = [threading.Thread(target=_read_stderr, args=(proc, run, tail), daemon=True),
                   threading.Thread(target=_read_stdout, args=(proc, stdout), daemon=True)]
        for reader in readers:
            reader.start()
        try:
            proc.wait(timeout=RUN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop_child(proc)
        for reader in readers:
            reader.join(timeout=5.0)
        if run.cancelled or timed_out:
            _clear_run_lock(run, proc.pid)
        if run.cancelled:
            logger.info("canvas mirror %s: run stopped (%s)", env, run.cancelled)
            return _load(env)
        result = _classify(env, run, cli, proc.returncode, "".join(stdout), tail, timed_out)
        if result.state == "unsupported":
            # Nothing of this feature exists for this CLI: no folders, no
            # status file (the state is the process's own knowledge).
            for folder in reversed(made):
                try:
                    os.rmdir(folder)
                except OSError:
                    pass
        _save(result, persist=result.state != "unsupported")
        level = logging.INFO if result.state in ("ok", "busy") else logging.WARNING
        logger.log(level, "canvas mirror %s: %s%s", env, result.state,
                   f" ({result.error})" if result.error else "")
        return result
    except OSError as exc:
        result = MirrorStatus(env=env, state="failed", trigger=run.trigger,
                              started_at=run.started_at, finished_at=time.time(),
                              error=f"could not run the fused CLI: {exc}")
        _save(result)
        return result
    finally:
        if proc is not None and proc.poll() is None:
            _stop_child(proc)
        for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        run.finished.set()
        with _LOCK:
            if _runs.get(env) is run:
                del _runs[env]


def run_once(env: str, trigger: str = "manual") -> MirrorStatus:
    """Run the mirror for `env` NOW and wait for it: the result, or the last
    one when no run may start (one is running, the environment is held, no
    CLI, the CLI has no such command, the process is shutting down). Blocks
    for the length of the run — call it from a worker, never a request."""
    cli = canvases.fused_cli()
    if cli is None or _is_unsupported(cli):
        return _load(env)
    run = _begin(env, trigger)
    if isinstance(run, str):
        return _load(env)
    return _execute(run)


def _run_quietly(run: _Run) -> None:
    try:
        _execute(run)
    except Exception:  # noqa: BLE001 — never out of a helper thread
        logger.warning("canvas mirror %s: run failed", run.env, exc_info=True)


def kick(env: str, trigger: str, *, force: bool = False, ignore_age: bool = False,
         guard: threading.Event | None = None) -> dict:
    """Start a mirror run for `env` on a thread, if one may start:
    `{"started": bool, "reason": str | None}`. Never blocks, never raises.

    Without `force` the run is a scheduled one: the environment must be in
    use (`eligible`) and its last good run older than `INTERVAL_S`. `force`
    (the page's refresh, the run after a hold) skips both; `ignore_age`
    skips only the second (a sign-in: the last result may be another
    account's, however fresh). `guard`: see `_begin`."""
    try:
        ok, why = eligible(env, scheduled=not force)
        if not ok:
            return {"started": False, "reason": why}
        if not force and not ignore_age:
            last = _load(env)
            if (last.state in _GOOD_STATES and last.finished_at is not None
                    and time.time() - last.finished_at < INTERVAL_S):
                return {"started": False, "reason": "fresh"}
        run = _begin(env, trigger, guard)
        if isinstance(run, str):
            return {"started": False, "reason": run}
        threading.Thread(target=_run_quietly, args=(run,), daemon=True,
                         name=f"canvas-mirror-{env}").start()
        return {"started": True, "reason": None}
    except Exception:  # noqa: BLE001 — a kick is best effort for every caller
        logger.warning("canvas mirror %s: could not start a run", env, exc_info=True)
        return {"started": False, "reason": "error"}


def kick_if_started(env: str, trigger: str, *, ignore_age: bool = False) -> dict:
    """`kick`, but only in a process whose mirror was started (`start`): the
    hook for code that runs in every process — a completed sign-in — and must
    not make a lean process or a test spawn the command."""
    with _LOCK:
        if not _started:
            return {"started": False, "reason": "not_started"}
    return kick(env, trigger, ignore_age=ignore_age)


def child_running(env: str) -> bool:
    """Whether a `canvas mirror` child of THIS process may be writing `env`'s
    scope folders right now. The layout move asks before it touches `user/`;
    a run that has not spawned its child yet cannot spawn one while a pass
    holds the layout's lock (`_execute`)."""
    with _LOCK:
        run = _runs.get(env)
        return (run is not None and not run.finished.is_set()
                and run.proc is not None and run.proc.poll() is None)


def _cancel(env: str, reason: str, *, wait: float = HOLD_WAIT_S) -> bool:
    """Stop `env`'s run in flight and wait for it to be gone. True when one
    was running."""
    with _LOCK:
        run = _runs.get(env)
        if run is None or run.finished.is_set():
            return False
        run.cancelled = reason
        proc = run.proc
    if proc is not None:
        _stop_child(proc)
    if not run.finished.wait(wait):
        logger.warning("canvas mirror %s: run did not stop within %ss", env, wait)
    return True


@contextlib.contextmanager
def hold(env: str):
    """No mirror run of THIS process touches `env`'s folders inside the
    block: a run in flight is stopped first (and is gone when the block is
    entered), none starts meanwhile, and one is started on the way out if
    one was stopped or refused. Re-entrant; never raises of its own."""
    with _LOCK:
        _holds[env] = _holds.get(env, 0) + 1
    try:
        try:
            if _cancel(env, "hold"):
                with _LOCK:
                    _rekick.add(env)
        except Exception:  # noqa: BLE001 — the caller's work matters more
            logger.warning("canvas mirror %s: could not stop the run", env, exc_info=True)
        yield
    finally:
        with _LOCK:
            _holds[env] -= 1
            again = _holds[env] <= 0
            if again:
                del _holds[env]
                again = env in _rekick
                _rekick.discard(env)
        if again:
            kick(env, "hold", force=True)


def stop_for_store(store: str) -> None:
    """A sign-out of credentials store `store`: stop the runs using it. The
    folders stay; the next run under whoever signs in prunes them."""
    for env in WORKBENCH_ENV_CHOICES:
        try:
            if canvases.credentials_file(env) == store:
                _cancel(env, "logout")
        except Exception:  # noqa: BLE001
            logger.warning("canvas mirror %s: could not stop the run", env, exc_info=True)


def signed_in(store: str) -> None:
    """A sign-in into credentials store `store` completed: mirror the
    environments that read it (only in a process whose mirror was started)."""
    for env in WORKBENCH_ENV_CHOICES:
        try:
            if canvases.credentials_file(env) == store:
                # Whatever the last result's age: it may be the account that
                # just signed OUT's.
                kick_if_started(env, "login", ignore_age=True)
        except Exception:  # noqa: BLE001
            logger.warning("canvas mirror %s: could not start a run", env, exc_info=True)


# -- the timer -----------------------------------------------------------------


def _timer_done() -> bool:
    """The CLI has no `canvas mirror`: nothing a later tick could do."""
    cli = canvases.fused_cli()
    return cli is not None and _is_unsupported(cli)


def _timer_loop(stop: threading.Event) -> None:
    if stop.wait(STARTUP_DELAY_S):
        return
    trigger = "startup"
    while not stop.is_set():
        try:
            for env in eligible_envs():
                if stop.is_set():
                    return
                kick(env, trigger, guard=stop)
        except Exception:  # noqa: BLE001 — never out of a helper thread
            logger.warning("canvas mirror: scheduled pass failed", exc_info=True)
        trigger = "timer"
        if stop.wait(INTERVAL_S * random.uniform(1 - _JITTER, 1 + _JITTER)):
            return
        if _timer_done():
            logger.info("canvas mirror: this Fused has no `canvas mirror`; "
                        "no further runs are scheduled")
            return


def started() -> bool:
    with _LOCK:
        return _started


def start() -> None:
    """Start the scheduled runs (the server's startup hook). Idempotent."""
    global _started, _latched, _timer_stop
    with _LOCK:
        if _started:
            return
        _started, _latched = True, False
        _timer_stop = stop = threading.Event()
    threading.Thread(target=_timer_loop, args=(stop,), daemon=True,
                     name="canvas-mirror-timer").start()


def shutdown(*, latch: bool = True, budget_s: float | None = None) -> None:
    """Stop the timer and every run in flight, within `budget_s` (default:
    the full interrupt/terminate/kill ladder). `latch` (the quit rung) also
    refuses every later run; the lifespan caller leaves it off, as
    `canvases.shutdown` does, so a later app in this process may start again.

    EVERY child is signalled before any is waited for, so the wait is one
    ladder long however many environments are running, and each step is cut
    to what is left of the budget. Whatever the budget, the last act is a
    kill of every child's process group and the removal of the lock it could
    not release — done here, not left to the run's own thread, which a
    quitting process may never schedule again."""
    global _started, _latched, _timer_stop
    with _LOCK:
        _started = False
        if latch:
            _latched = True
        stop, _timer_stop = _timer_stop, None
        if stop is not None:
            stop.set()      # under the lock `_begin` checks it under
        runs = [r for r in _runs.values() if not r.finished.is_set()]
        for run in runs:
            run.cancelled = "shutdown"
        _rekick.clear()
    children = [(run, run.proc) for run in runs if run.proc is not None]
    deadline = time.monotonic() + (budget_s if budget_s is not None
                                   else len(_stop_steps()) * STOP_GRACE_S + 1.0)
    for sig in _stop_steps():
        alive = [proc for _run, proc in children if proc.poll() is None]
        if not alive:
            break
        for proc in alive:
            _signal(proc, sig)
        step_end = min(deadline, time.monotonic() + STOP_GRACE_S)
        for proc in alive:
            try:
                proc.wait(timeout=max(0.0, step_end - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        if time.monotonic() >= deadline:
            break
    for run, proc in children:
        _signal(proc, None)
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        _clear_run_lock(run, proc.pid)
    for run in runs:
        run.finished.wait(max(0.0, deadline - time.monotonic()))


def refuse_new_runs() -> None:
    """The quit rung's spawn latch (armed before any killer runs)."""
    global _latched
    with _LOCK:
        _latched = True


def reset() -> None:
    """Forget everything this process knows (tests)."""
    global _started, _latched, _timer_stop
    shutdown(latch=False)
    with _LOCK:
        _runs.clear()
        _holds.clear()
        _rekick.clear()
        _unsupported.clear()
        _listed.clear()
        _last.clear()
        _last_sig.clear()
        _started, _latched, _timer_stop = False, False, None


# -- routes --------------------------------------------------------------------


@router.get("/api/canvases/mirror/status")
def api_canvases_mirror_status(env: str = ""):
    """One environment's mirror status (`status`), or with no `env` every
    environment's: `{"envs": {env: status}}`. A local read, like
    /api/canvases/status: no CLI runs."""
    if not env:
        return {"envs": {e: status(e) for e in WORKBENCH_ENV_CHOICES}}
    if env not in WORKBENCH_ENV_CHOICES:
        return JSONResponse(
            {"error": f"'env' must be one of: {', '.join(WORKBENCH_ENV_CHOICES)}"},
            status_code=400)
    return status(env)


@router.post("/api/canvases/mirror/run")
def api_canvases_mirror_run(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Refresh `env`'s reference canvases now, whatever their age. Answers at
    once: `{"started", "reason", "status"}`; the page follows the status."""
    guard = canvases._require_fused(x_fused)
    if guard is not None:
        return guard
    env, bad = canvases._require_env(body.get("env"))
    if bad is not None:
        return bad
    outcome = kick(env, "manual", force=True)
    return {**outcome, "status": status(env)}
