"""Resident model processes: start, evict, measure, stop (SPEC §40).

One table, one lock, one worker per capability. Everything a model does happens
in a subprocess this module owns:

    load(model, capability) -> a job id
        build the runner's venv if needed (envinstall, PY-18)
        -> start worker.py on that venv's python
        -> worker downloads what it lacks and loads it
        -> worker binds an ephemeral port and publishes {port, token}
        -> supervisor polls /health until ready

**One resident model per capability, auto-evicting.** Loading a second text model
unloads the first before the new one starts. This is not a policy choice so much
as arithmetic: an 8GB model and another 8GB model on a 16GB machine is a swap
storm, and a swap storm reads to the user as "the app hung". A text model and an
image model coexist because they are different capabilities and the user asked
for both.

**Load is asynchronous and reported, never awaited on the request path.** A cold
load is a multi-GB download followed by a minutes-long weight load. `load()`
returns a JOB ID immediately and the work continues on a thread; progress goes to
the download manager (`jobs.py`, SPEC §36) under the deterministic id
`sys:ai-model:<repo>`, so the AI Models page can join a job row onto a repo card
and the manager's ✕ can stop it. The `sys:` prefix marks it server-owned: the ✕
really kills the process here, rather than asking a page to stop politely.

**The worker is trusted to describe itself.** Its `/health` reports resident
bytes, because only the process holding the weights can measure them — and on
Apple Silicon "GPU memory" is the same unified pool as RSS, so there is one
honest number rather than two. What the supervisor knows independently is
whether the process is ALIVE; a worker that stops answering is `error`, never a
`ready` row that lies.

Nothing here imports torch, mlx, or huggingface_hub. This module speaks HTTP to a
process and reads a status file; the heavy imports live on the other side of that
boundary, in an interpreter this one built.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from fused_render import jobs
from fused_render.ai import registry

# How long to wait for a freshly spawned worker to publish its port before
# calling the launch failed. Generous: the process has to import its runtime
# (mlx/torch are seconds of import alone) before it binds.
BOOTSTRAP_TIMEOUT_S = 120.0
BOOTSTRAP_POLL_S = 0.25

# A health probe is a localhost request to a process that may be inside a
# multi-second GPU call, so this is loose. It is not a load timeout — loading is
# bounded by the job, not by this.
HEALTH_TIMEOUT_S = 5.0

# Generation can take minutes (an image at high step counts, a long completion).
GENERATE_TIMEOUT_S = 900.0

JOB_PREFIX = jobs.SERVER_ID_PREFIX + "ai-model:"


class SupervisorError(RuntimeError):
    """Something the caller can be told verbatim."""


class ModelNotReady(SupervisorError):
    """Asked to generate with a model that is not resident yet.

    Carries the JOB the caller should watch, because by the time this is raised
    the load has already been started — the answer to "not loaded" is never just
    "no", it is "no, and here is the work that fixes it".
    """

    def __init__(self, message: str, job_id: str):
        super().__init__(message)
        self.job_id = job_id


def job_id_for(model: str) -> str:
    """The download-manager row for this model's bring-up.

    Deterministic so the AI Models page can look up "is this repo downloading"
    by name instead of maintaining a second index — the same trick the two
    sandbox apps used with `local-chat:<model>`, now with a reserved prefix so a
    page cannot post to it.
    """
    safe = "".join(c if (c.isalnum() or c in "._-/") else "-" for c in model).replace("/", "--")
    return JOB_PREFIX + safe


@dataclass
class Worker:
    """One resident model. Mutated under `_lock`; read as a snapshot."""

    model: str
    capability: str
    runner_code: str
    state: str = "starting"  # starting | venv | downloading | loading | ready | error
    detail: str = ""
    error: str = ""
    port: int | None = None
    token: str = ""
    pid: int | None = None
    #: The Popen. Kept because it is the only thing that can REAP the child:
    #: `os.kill(pid, 0)` succeeds on a zombie, so a supervisor that checked
    #: liveness by pid would report an exited worker as alive forever — and go
    #: on offering a dead model as `ready`.
    proc: subprocess.Popen | None = field(default=None, repr=False)
    resident_bytes: int | None = None
    loaded_at: float | None = None
    started_at: float = field(default_factory=time.time)
    #: Set when the user cancels or a newer load evicts this one. The bring-up
    #: thread checks it at every step so an evicted load stops downloading
    #: instead of finishing into a table it no longer belongs to.
    stopping: bool = False


_lock = threading.RLock()
#: capability -> the one Worker resident for it.
_workers: dict[str, Worker] = {}
#: Every token handed to a live worker. A worker reports its own download
#: progress to `/api/jobs` under a `sys:` id, which pages are forbidden from
#: writing — so the endpoint has to be able to tell a worker from a page, and
#: this is how: the token the supervisor generated and passed in the child's
#: environment, presented back in a header. Tokens are dropped the moment the
#: worker they belong to stops.
_worker_tokens: set[str] = set()


def is_worker_token(token: str) -> bool:
    """Is this the token of a worker THIS supervisor started?

    The one thing standing between "a model reports its download" and "any page
    can forge a completed download", so it is an exact membership test against
    live tokens — never a prefix, never a truthiness check on a string.
    """
    if not token:
        return False
    with _lock:
        return token in _worker_tokens


# ----------------------------------------------------------------- worker HTTP


def _worker_url(worker: Worker, path: str) -> str:
    return f"http://127.0.0.1:{worker.port}{path}"


def _worker_request(worker: Worker, path: str, body: dict | None = None,
                    timeout: float = HEALTH_TIMEOUT_S):
    """One request to a worker. Raises OSError/urllib errors for the caller.

    The token is a header, not a query parameter: a foreign page that guessed
    the port still cannot drive the model, and the value never lands in a log
    line or a Referer.
    """
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        _worker_url(worker, path),
        data=data,
        headers={"X-Fused-Worker": worker.token,
                 **({"Content-Type": "application/json"} if data else {})},
        method="POST" if data is not None else "GET",
    )
    return urllib.request.urlopen(request, timeout=timeout)


def _health(worker: Worker) -> dict | None:
    try:
        with _worker_request(worker, "/health") as response:
            return json.loads(response.read().decode() or "{}")
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------- lifecycle


def _alive(worker: Worker) -> bool:
    """Is this worker's process still running?

    `poll()` rather than `os.kill(pid, 0)`, for two independent reasons:

    * On POSIX an exited child nobody has waited on is a ZOMBIE, and signal 0 to
      a zombie succeeds — so the pid check answers "alive" for a model that
      crashed, which is the one answer this function must never get wrong.
      `poll()` reaps as it asks, so the zombie also goes away.
    * On Windows `os.kill(pid, sig)` is `TerminateProcess(handle, sig)` for any
      signal that is not a console event. `os.kill(pid, 0)` there does not probe
      a process, it KILLS it with exit code 0 — a liveness check that is fatal to
      the thing it asks about.
    """
    proc = worker.proc
    if proc is None:
        return False
    return proc.poll() is None


#: Detach a worker into its own process group so a stop reaches the CHILDREN it
#: spawned too — a dataloader, a compile step, a `uv` download — and not just the
#: leader that would otherwise leave them holding the weights. Two different
#: mechanisms for the same idea; `envinstall._spawn` makes the same split.
SPAWN_KWARGS = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    if os.name == "nt" else {"start_new_session": True}
)

#: Windows has no SIGKILL. Read through getattr so merely IMPORTING this module
#: does not fail there — the image runner is cross-platform, so this code really
#: does run on Windows.
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _kill_tree(worker: Worker) -> None:
    """Stop the worker process and everything it started.

    Two platforms, two mechanisms, and neither is optional:

    * **POSIX** — `killpg` on the worker's own group, but ONLY when the pid is
      that group's leader. The guard is not decoration: `envinstall._kill`
      carries the same one because a stale pid that happened to live in the
      SERVER's group once made `killpg` shut down a pytest session. A non-leader
      gets a plain single-pid kill.
    * **Windows** — there is no `killpg` (it does not exist as an attribute, so a
      naive port raises AttributeError rather than OSError), and `os.kill(pid,
      sig)` maps onto `TerminateProcess`. CTRL_BREAK reaches the group we spawned
      with CREATE_NEW_PROCESS_GROUP; `taskkill /T /F` is the fallback that walks
      the tree.
    """
    pid = worker.pid
    if not pid:
        return
    if os.name == "nt":
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        except (OSError, AttributeError, ValueError):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _alive(worker):
            time.sleep(0.05)
        if _alive(worker):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        return

    try:
        leader = os.getpgid(pid) == pid
    except OSError:
        leader = False
    for sig in (signal.SIGTERM, _SIGKILL):
        if not _alive(worker):
            return
        try:
            os.killpg(pid, sig) if leader else os.kill(pid, sig)
        except OSError:
            return
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _alive(worker):
            time.sleep(0.05)


def _terminate(worker: Worker) -> None:
    """Ask the worker to quit, then make sure of it.

    `/quit` first because a clean exit releases GPU buffers the OS is slower to
    reclaim; the kill is what happens when the process is wedged inside a
    generation and cannot get back to its accept loop.
    """
    if worker.port:
        try:
            _worker_request(worker, "/quit", body={}, timeout=2.0).close()
        except (OSError, ValueError):
            pass
    with _lock:
        _worker_tokens.discard(worker.token)
    _kill_tree(worker)
    # Reap, so the child does not linger as a zombie in the process table —
    # which `_alive` would then have to keep answering questions about.
    if worker.proc is not None:
        try:
            worker.proc.wait(timeout=1.0)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _child_env(token: str) -> dict:
    """Environment for a worker process.

    The PYTHON* vars are stripped for the reason `local_chat/chat.py` documents
    and the macOS bundle proves: the packaged app exports `PYTHONHOME` pointing
    inside itself, and a venv interpreter that inherits it dies at startup with
    "Failed to import encodings module" before running a line. The origin is
    passed through so the worker can report its own download progress.
    """
    env = dict(os.environ)
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "PYTHONSTARTUP"):
        env.pop(name, None)
    env["FUSED_AI_WORKER_TOKEN"] = token
    return env


def _worker_dir() -> str:
    from fused_render.shell.storage import home_dir

    directory = os.path.join(home_dir(), "ai", "workers")
    os.makedirs(directory, exist_ok=True)
    return directory


def _status_path(capability: str) -> str:
    return os.path.join(_worker_dir(), capability.replace("/", "-") + ".json")


def _log_path(capability: str) -> str:
    """Where a worker's stderr goes.

    A FILE, never `subprocess.PIPE`. A pipe nobody drains holds ~64KB before the
    child BLOCKS on its next write — so a worker that logs while downloading
    (hf and torch both do, at length) would wedge mid-load while still looking
    alive, and the pipe only gets read after exit, which by then never comes.
    """
    return os.path.join(_worker_dir(), capability.replace("/", "-") + ".log")


def _tail(path: str, limit: int = 2000) -> str:
    try:
        with open(path, errors="replace") as handle:
            return handle.read()[-limit:]
    except OSError:
        return ""


def _spawn(runner: registry.Runner, worker: Worker, python: str) -> None:
    """Start worker.py and wait for it to publish its port.

    The worker writes `{port, pid}` to a status file rather than being handed a
    port: binding :0 in the child and reporting back is the only version with no
    race, since anything this process reserves can be taken between the bind and
    the exec.
    """
    status = _status_path(worker.capability)
    try:
        os.unlink(status)
    except OSError:
        pass

    job = job_id_for(worker.model)
    log = _log_path(worker.capability)
    proc = subprocess.Popen(
        [python, runner.worker, "--model", worker.model, "--status", status, "--job", job],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=open(log, "w"),
        cwd=runner.folder,
        env=_child_env(worker.token),
        close_fds=True,
        **SPAWN_KWARGS,
    )
    worker.pid = proc.pid
    worker.proc = proc

    deadline = time.monotonic() + BOOTSTRAP_TIMEOUT_S
    while time.monotonic() < deadline:
        if worker.stopping:
            raise SupervisorError("cancelled")
        if proc.poll() is not None:
            stderr = _tail(log)
            raise SupervisorError(
                f"the worker exited before it started (code {proc.returncode})"
                + (f"\n{stderr}" if stderr.strip() else "")
            )
        try:
            with open(status) as handle:
                published = json.load(handle)
        except (OSError, ValueError):
            time.sleep(BOOTSTRAP_POLL_S)
            continue
        port = published.get("port")
        if isinstance(port, int) and port > 0:
            worker.port = port
            return
        time.sleep(BOOTSTRAP_POLL_S)
    raise SupervisorError("the worker never published a port")


# --------------------------------------------------------------- bring-up flow


def _report(job: str, **fields) -> None:
    """One progress tick, best-effort. Reporting must never break the load."""
    try:
        jobs.upsert({"id": job, **fields}, server=True)
    except (jobs.JobError, ValueError):
        pass


def _cancel_requested(job: str) -> bool:
    for record in jobs.list_jobs():
        if record["id"] == job:
            return bool(record.get("cancel_requested"))
    return False


def _ensure_venv(runner: registry.Runner, worker: Worker, job: str) -> str:
    """The runner's interpreter, building its environment first if needed.

    Reuses `envinstall` wholesale (PY-18) — the same detached `uv sync`, the same
    progress record, the same verbatim uv errors that every declaring folder in
    the app already gets. A first `torch` install is gigabytes, which is exactly
    why this is a reported stage and not a silent wait.
    """
    from fused_render import envinstall

    if envinstall.is_installed(runner.folder):
        return envinstall.venv_python_for(runner.folder)

    worker.state = "venv"
    _report(job, state="running", kind="download", detail=f"Preparing {runner.label}…",
            done=None, total=None)
    envinstall.start(runner.folder)
    key = envinstall.venv_key_for(runner.folder)
    while True:
        if worker.stopping or _cancel_requested(job):
            envinstall.cancel(key)
            raise SupervisorError("cancelled")
        record = envinstall.progress(key) or {}
        if record.get("done"):
            if record.get("error"):
                raise SupervisorError(str(record["error"]))
            break
        _report(job, detail=f"Preparing {runner.label} — {record.get('stage') or 'installing'}…")
        time.sleep(0.5)
    if not envinstall.is_installed(runner.folder):
        raise SupervisorError(f"the environment for {runner.label} did not build")
    return envinstall.venv_python_for(runner.folder)


def _bring_up(runner: registry.Runner, worker: Worker, job: str) -> None:
    """Venv -> spawn -> wait for ready. Runs on its own thread."""
    try:
        python = _ensure_venv(runner, worker, job)
        if worker.stopping:
            raise SupervisorError("cancelled")

        worker.state = "starting"
        _report(job, detail="Starting the model process…")
        _spawn(runner, worker, python)

        # From here the WORKER is the one that knows what is happening — it is
        # doing the downloading and the loading — so its /health is the source
        # of truth and it reports its own byte counts to the same job row.
        while True:
            if worker.stopping:
                raise SupervisorError("cancelled")
            if not _alive(worker):
                raise SupervisorError("the model process exited while loading")
            health = _health(worker)
            if health:
                worker.state = str(health.get("state") or "loading")
                worker.detail = str(health.get("detail") or "")
                resident = health.get("resident_bytes")
                worker.resident_bytes = resident if isinstance(resident, int) else None
                if worker.state == "ready":
                    worker.loaded_at = time.time()
                    _report(job, state="done", detail="Model loaded")
                    return
                if worker.state == "error":
                    raise SupervisorError(str(health.get("error") or "the model failed to load"))
            time.sleep(0.5)
    except SupervisorError as e:
        message = str(e)
        with _lock:
            worker.state = "error"
            worker.error = message
            if _workers.get(worker.capability) is worker:
                del _workers[worker.capability]
        _terminate(worker)
        if message == "cancelled":
            _report(job, state="cancelled")
        else:
            _report(job, state="error", message=message)


# ---------------------------------------------------------------- public façade


def _fetch_only(runner: registry.Runner, model: str, job: str) -> None:
    """Download a model's weights and stop — no residency, no eviction.

    A separate path from `_bring_up` because it must NOT touch the worker table:
    someone pressing Download on the AI Models page is filling a cache, not
    asking to replace the model they are currently chatting with. The runner's
    own worker does the fetching (`--download-only`) because what a model's
    files even ARE differs by backend — a GGUF single file for the image runner,
    a full snapshot for MLX.
    """
    # A token even though it serves nothing: the download-only worker still
    # REPORTS, and reporting is what the token authenticates.
    stub = Worker(model=model, capability=runner.capability, runner_code=runner.code,
                  token=secrets.token_urlsafe(24))
    with _lock:
        _worker_tokens.add(stub.token)
    try:
        python = _ensure_venv(runner, stub, job)
        _report(job, detail="Fetching weights…")
        log = _log_path(runner.capability + "-download")
        proc = subprocess.Popen(
            [python, runner.worker, "--model", model, "--job", job, "--download-only"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=open(log, "w"),
            cwd=runner.folder, env=_child_env(stub.token), close_fds=True,
            **SPAWN_KWARGS,
        )
        stub.pid = proc.pid
        stub.proc = proc
        while proc.poll() is None:
            if stub.stopping or _cancel_requested(job):
                _terminate(stub)
                _report(job, state="cancelled")
                return
            time.sleep(0.5)
        if proc.returncode != 0:
            stderr = _tail(log)
            raise SupervisorError(stderr.strip() or f"the download exited {proc.returncode}")
        _report(job, state="done", detail="Downloaded")
    except SupervisorError as e:
        message = str(e)
        _report(job, state="cancelled" if message == "cancelled" else "error",
                message=None if message == "cancelled" else message)
    finally:
        with _lock:
            _worker_tokens.discard(stub.token)


def load(model: str, capability: str, *, weights_only: bool = False) -> dict:
    """Make `model` resident for `capability`; returns `{jobId, model, state}`.

    `weights_only` downloads and stops — the AI Models page's "Download", which
    must not evict whatever is currently loaded.

    Idempotent for the model already loading or loaded — a second call joins the
    first rather than starting a duplicate, which matters because two pages
    opening at once is the normal case, not the exotic one.
    """
    runner = registry.for_capability(capability)
    if runner is None:
        known = next((r for r in registry.all_runners() if r.capability == capability), None)
        raise SupervisorError(
            known.available().reason if known
            else f"no runner provides {capability!r}"
        )
    if not shutil.which("uv"):
        raise SupervisorError("uv is not available, so the model environment cannot be built")

    job = job_id_for(model)
    if weights_only:
        _report(job, title=model, state="running", kind="download", cancellable=True,
                unit="bytes", detail="Preparing…", done=None, total=None)
        threading.Thread(target=_fetch_only, args=(runner, model, job),
                         name=f"ai-fetch-{capability}", daemon=True).start()
        return {"jobId": job, "model": model, "state": "downloading"}

    with _lock:
        current = _workers.get(capability)
        if current is not None and current.model == model and current.state != "error":
            return {"jobId": job_id_for(model), "model": model, "state": current.state}
        if current is not None:
            # Eviction: the weights of the old model must be released BEFORE the
            # new ones start loading, or the machine holds both at once — which
            # on 16GB of unified memory is the difference between a load and a
            # swap storm.
            current.stopping = True
            _terminate(current)
            _workers.pop(capability, None)

        worker = Worker(model=model, capability=capability, runner_code=runner.code,
                        token=secrets.token_urlsafe(24))
        _workers[capability] = worker
        _worker_tokens.add(worker.token)

    _report(job, title=model, state="running", kind="download", cancellable=True,
            detail="Preparing…", done=None, total=None)
    threading.Thread(target=_bring_up, args=(runner, worker, job),
                     name=f"ai-load-{capability}", daemon=True).start()
    return {"jobId": job, "model": model, "state": worker.state}


def unload(model: str | None = None, capability: str | None = None) -> bool:
    """Stop a resident worker. True if there was one to stop."""
    with _lock:
        targets = [
            w for w in _workers.values()
            if (model is None or w.model == model)
            and (capability is None or w.capability == capability)
        ]
        for worker in targets:
            worker.stopping = True
            _workers.pop(worker.capability, None)
    for worker in targets:
        _terminate(worker)
        _report(job_id_for(worker.model), state="done", detail="Unloaded")
    return bool(targets)


def unload_all() -> None:
    """Server shutdown: nothing may outlive the app holding gigabytes."""
    unload()


def ready_worker(capability: str, model: str | None = None) -> Worker | None:
    """The resident, READY worker for a capability — what generation needs."""
    with _lock:
        worker = _workers.get(capability)
        if worker is None or worker.state != "ready":
            return None
        if model is not None and worker.model != model:
            return None
        return worker


def generate_text(model: str, body: dict):
    """Yield `{"type":"chunk","text":…}` events from the resident text model.

    Raises `SupervisorError` when the model is not ready — and STARTS THE LOAD
    on the way out, returning its job id in the message payload. That is the
    ergonomic middle: a caller should not have to orchestrate load-then-wait
    before its first `fused.ai(...)`, but generation must not block for the
    minutes a cold 8GB load takes either. So the first call fails fast, having
    kicked off exactly the work the caller needed, and the page watches the job.
    """
    worker = ready_worker(registry.TEXT_GENERATION, model)
    if worker is None:
        with _lock:
            current = _workers.get(registry.TEXT_GENERATION)
        if current is not None and current.model == model:
            raise ModelNotReady(
                f"{model} is still loading ({current.state})", job_id_for(model))
        started = load(model, registry.TEXT_GENERATION)
        raise ModelNotReady(f"{model} is loading now", started["jobId"])

    try:
        response = _worker_request(worker, "/generate", body=body,
                                   timeout=GENERATE_TIMEOUT_S)
    except (OSError, ValueError) as e:
        raise SupervisorError(f"the model process did not answer: {e}") from e
    with response:
        for line in response:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line.decode())
            except ValueError:
                continue


def cancel_generation(capability: str = registry.TEXT_GENERATION) -> bool:
    worker = ready_worker(capability)
    if worker is None:
        return False
    try:
        _worker_request(worker, "/cancel", body={}, timeout=2.0).close()
    except (OSError, ValueError):
        return False
    return True


def refresh_memory() -> None:
    """Re-read resident bytes from every live worker.

    Called by the status endpoint rather than by a timer: the number is only
    interesting when someone is looking at it, and a background poll of a
    process mid-generation is a request that waits on a GPU call for no reason.
    """
    with _lock:
        current = list(_workers.values())
    for worker in current:
        if worker.state != "ready":
            continue
        if not _alive(worker):
            # The one thing the supervisor knows better than the worker does.
            worker.state = "error"
            worker.error = "the model process is gone"
            with _lock:
                if _workers.get(worker.capability) is worker:
                    del _workers[worker.capability]
            continue
        health = _health(worker)
        if health and isinstance(health.get("resident_bytes"), int):
            worker.resident_bytes = health["resident_bytes"]


def describe() -> dict:
    """The runtime as the API reports it."""
    refresh_memory()
    with _lock:
        loaded = [
            {
                "model": w.model,
                "capability": w.capability,
                "runner": w.runner_code,
                "state": w.state,
                "detail": w.detail or None,
                "error": w.error or None,
                "residentBytes": w.resident_bytes,
                "loadedAt": w.loaded_at,
                "startedAt": w.started_at,
                "jobId": job_id_for(w.model),
            }
            for w in _workers.values()
        ]
    total = sum(row["residentBytes"] or 0 for row in loaded)
    return {
        "runners": registry.describe(),
        "loaded": loaded,
        "totalResidentBytes": total or None,
    }


def reset() -> None:
    """Tests only: drop the table without touching real processes."""
    with _lock:
        _workers.clear()
