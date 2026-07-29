"""The script-venv install loader for the fused engine (SPEC PY-18, D173).

A script with no PEP 723 header runs on the app's own interpreter and installs
nothing (PY-17). The few templates that keep a header — geotiff's imagecodecs
and pyproj, zarr_aoi's s3fs/gcsfs/crc32c, pano's py360convert, the pandocs —
need a real download, and `fused.runPython` has roughly a 30-second budget. A
build that overruns it used to surface as a timeout or an opaque `EngineError`
with the resolver's real complaint buried inside, which is the worst possible
answer to "you need a package you don't have".

So the build moves off the request path entirely, in the shape
`templates/docs/install_worker.py` already uses for the typst download (one
pattern in this repo, not two):

  1. `/api/run` pre-flight: header present + venv absent -> `needs_install`
     with the venv key and the resolved requirement list. Nothing blocks.
  2. `POST /api/env/install` -> `start()` spawns a **detached** worker
     (`_env_install_worker.py`) that builds the venv and writes `progress.json`.
  3. `GET /api/env/progress?key=` -> `progress()`, polled by the page shell.
  4. `POST /api/env/cancel` -> `cancel()`, by the pid the worker recorded.

Two things this module must never get wrong:

**The key is `fused`'s, not ours.** `venv_key_for` composes upstream's own
`requirements_venv_id` / `venv_key`, so the directory the loader fills is
exactly the one `ensure_requirements_venv` will look in. A local
"sha256 of the sorted requirements" would agree until upstream changed the
recipe, and would then build a venv no run ever reads — a permanent double
download with no error anywhere.

**Errors are verbatim.** `venvs._run_step` raises with uv's/pip's own stderr in
the message, and that text is written into `progress.json` unchanged. "No
solution found ... imagecodecs has no wheels with a matching platform tag" is
the whole reason this flow is visible; a generic message would leave the user
exactly where they started.

Progress granularity is deliberately coarse. `_run_step` captures output, so
pip's per-package progress cannot be streamed without changing `fused`, which
is out of scope. `STAGES` names what is actually observable; nothing here
invents a percentage implying more resolution than that.

Scope is **per-file** (D173): the venv belongs to the requirement set one .py
declares. Sharing one venv across several Python files in a folder is
deliberately not built.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

# The marker `fused` writes once a venv is complete. A directory without it is
# half-built and `ensure_requirements_venv` deletes and rebuilds it, so this —
# not the directory's existence — is what "installed" means.
READY_MARKER = ".openfused-ready"

# Every stage the worker can report, in order. Coarse on purpose: see the module
# docstring. `spawn` is written by start() so the first poll after the click has
# something to show even before the worker's first write lands.
STAGES = ("spawn", "create", "install", "done")

# Percent per stage. Four numbers, not a continuous bar: they mark which step is
# running, and the gap between `install` and `done` is honestly unmeasurable
# here — a download whose length we cannot see.
STAGE_PCT = {"spawn": 0, "create": 10, "install": 25, "done": 100}


def venvs_path() -> str:
    """Where the backend keeps its script venvs.

    Read off the live backend instance rather than restating its default, so a
    server constructed with a different `venvs_path` cannot drift from the
    loader. Monkeypatched by tests to a tmp dir.
    """
    from fused_render.engine import get_backend

    return getattr(get_backend(), "_venvs_path", "~/.openfused/venvs")


def _python_executable() -> str | None:
    """The base interpreter the backend builds venvs from (None = ours).

    Folded into the venv key by `python_identity`, so the loader has to use the
    same value the backend will.
    """
    from fused_render.engine import get_backend

    return getattr(get_backend(), "_python_executable", None)


def venv_key_for(requirements: list[str]) -> str:
    """The backend's own cache key for a venv with `requirements` installed."""
    from fused.agent_core.backends.local.venvs import requirements_venv_id, venv_key

    return venv_key(requirements_venv_id(list(requirements), _python_executable()))


def venv_dir_for(requirements: list[str]) -> str:
    return os.path.join(os.path.expanduser(venvs_path()), venv_key_for(requirements))


def is_installed(requirements: list[str]) -> bool:
    """True iff the venv for `requirements` exists AND is complete."""
    if not requirements:
        return True  # nothing to install; the bare/interpreter paths handle it
    return os.path.exists(os.path.join(venv_dir_for(requirements), READY_MARKER))


def progress_dir(key: str) -> str:
    """Where a given install's `progress.json` and worker log live.

    Under the shell's home dir (so FUSED_RENDER_HOME redirects it for tests and
    per-branch state nests correctly), NOT inside the venv dir — a failed
    install deletes the venv dir, and the error is the one thing that must
    survive that.
    """
    from fused_render.shell.storage import home_dir

    return os.path.join(home_dir(), "cache", "_env_install", key)


def _progress_path(key: str) -> str:
    return os.path.join(progress_dir(key), "progress.json")


def _write(key: str, record: dict) -> None:
    """Atomically replace `progress.json` — a poll must never read a half-write."""
    os.makedirs(progress_dir(key), exist_ok=True)
    path = _progress_path(key)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f)
    os.replace(tmp, path)


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
        )
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # EPERM: someone else's live process
    return True


def _kill(pid: int) -> bool:
    """Stop the installer at `pid`, and the uv/pip child it is waiting on.

    Signalling only the worker would leave the actual download running, so the
    whole process GROUP is signalled — but ONLY when `pid` is its own group
    leader, which a `start_new_session` worker always is. That guard is not
    defensive decoration: the pid comes out of a file, and a stale or recycled
    one that happened to live in the SERVER's group would make `killpg` shut the
    server down. (It did exactly that to a pytest session, which is how the
    guard got here.) Anything not a group leader gets a plain single-pid kill.
    """
    if os.name == "nt":
        try:
            # The worker is spawned CREATE_NEW_PROCESS_GROUP, so CTRL_BREAK
            # reaches it and its children; taskkill /T is the fallback.
            os.kill(pid, signal.CTRL_BREAK_EVENT)
            return True
        except (OSError, AttributeError, ValueError):
            return subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True
            ).returncode == 0
    try:
        leader = os.getpgid(pid) == pid
    except OSError:
        leader = False
    try:
        if leader:
            os.killpg(pid, signal.SIGTERM)
        else:
            logger.warning(
                "install worker pid %s is not a process-group leader, so only it "
                "(not any download it started) is being killed", pid,
            )
            os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def progress(key: str) -> dict | None:
    """The install's current record, or None when it was never started.

    A record that is not `done` but whose pid is gone is a crash, and is
    reported as finished-with-an-error — the same liveness check
    `templates/docs/docs.py` does, and for the same reason: otherwise the page
    polls a dead installer forever.
    """
    path = _progress_path(key)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("done") and not _pid_alive(data.get("pid", -1)):
        data["done"] = True
        data["error"] = data.get("error") or (
            "the installer exited unexpectedly — see worker.log in "
            + progress_dir(key)
        )
    return data


def _in_flight(key: str) -> bool:
    prog = progress(key)
    return bool(prog) and not prog.get("done")


def _spawn(key: str, requirements: list[str]) -> int:
    """Launch the detached worker; returns its pid.

    Detached (`start_new_session` / DETACHED_PROCESS) so the build outlives the
    request that started it and any page reload — exactly docs.py's spawn.
    `sys.executable`, deliberately: `python_identity` keys the venv on the
    interpreter, so the worker must build from the same one the server would.
    """
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_env_install_worker.py")
    d = progress_dir(key)
    os.makedirs(d, exist_ok=True)
    detach = (
        {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt" else {"start_new_session": True}
    )
    with open(os.path.join(d, "worker.log"), "ab") as logf:
        child = subprocess.Popen(
            [sys.executable, worker, key, d, venvs_path(), *requirements],
            stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, **detach,
        )
    return child.pid


def start(requirements: list[str]) -> dict:
    """Begin (or join) the install for `requirements`; returns its progress.

    Idempotent in the two ways that matter: already installed is a no-op, and an
    install already running is joined rather than duplicated. Two workers
    building one venv directory is the race `fused`'s in-process lock cannot
    cover — the loser dies on a half-built `<venv>/bin/python`.
    """
    key = venv_key_for(requirements)
    if is_installed(requirements):
        record = {"stage": "done", "pct": 100, "detail": "already installed",
                  "done": True, "error": None, "pid": os.getpid(), "ts": time.time()}
        _write(key, record)
        return record
    if _in_flight(key):
        return progress(key)
    pid = _spawn(key, list(requirements))
    # Written by the PARENT, before the worker's first write lands, so the very
    # first poll after the click shows "starting" instead of "never started" —
    # and so `_in_flight` is true immediately, closing the double-click window.
    record = {"stage": "spawn", "pct": STAGE_PCT["spawn"],
              "detail": f"starting installer for {len(requirements)} package(s)",
              "done": False, "error": None, "pid": pid, "ts": time.time()}
    _write(key, record)
    return record


def cancel(key: str) -> bool:
    """Kill the recorded installer; True if there was a live one to kill.

    The half-built venv dir is left as-is on purpose: it has no ready marker, so
    `ensure_requirements_venv` removes and rebuilds it on the next attempt. The
    record is marked done-with-an-error so the poller stops and the page can say
    what happened rather than falling silent.
    """
    prog = progress(key)
    if not prog or prog.get("done"):
        return False
    pid = prog.get("pid", -1)
    killed = _kill(pid) if _pid_alive(pid) else False
    prog.update(done=True, error="the install was cancelled", stage="done",
                pct=100, ts=time.time())
    _write(key, prog)
    return killed
