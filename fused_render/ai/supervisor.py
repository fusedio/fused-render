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
import logging
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

logger = logging.getLogger(__name__)

# How long to wait for a freshly spawned worker to publish its port before
# calling the launch failed. Generous: the process has to import its runtime
# (mlx/torch are seconds of import alone) before it binds.
BOOTSTRAP_TIMEOUT_S = 120.0
BOOTSTRAP_POLL_S = 0.25

#: How many `envinstall.start()` rounds one runner environment may take. Two is
#: the real number — the pinned interpreter, then the packages (D214) — and the
#: third is slack for a loader that grows another bootstrap stage rather than a
#: retry loop: a round only repeats when the previous one finished CLEANLY and
#: still left nothing installed, which is progress or a bug, never a poll.
_VENV_ROUNDS = 3

# A health probe is a localhost request to a process that may be inside a
# multi-second GPU call, so this is loose. It is not a load timeout — loading is
# bounded by the job, not by this.
HEALTH_TIMEOUT_S = 5.0

# Generation can take minutes (an image at high step counts, a long completion).
GENERATE_TIMEOUT_S = 900.0

# A transcription can take HOURS, and 900s was a silent cap on the feature's own
# motivating case. The worker sends nothing until the decode finishes — one JSON
# reply when `generate` returns — so this socket timeout covers the whole run,
# and a 90-minute recording is ~18 minutes of it at the default model's CPU int8
# speed. At 900s that request died, the row went to error, and the worker
# carried on to write a transcript nobody was told about; worse, it was still
# holding the worker's `GENERATE_LOCK`, so every queued transcription repeated
# the failure in turn.
#
# Four hours of DECODING is ~20 hours of audio, which is past anything somebody
# hands a file explorer. It is a backstop rather than the stop: the ✕ makes the
# worker reply (its per-segment tick carries the cancel back), and an unload
# kills the process and closes the socket — both unblock this in seconds. What
# is left for a timeout is a worker that is alive but wedged, and parking a
# daemon thread on that forever is the thing worth refusing.
TRANSCRIBE_TIMEOUT_S = 4 * 3600.0

# How long an image request will wait for its model to become resident. Long,
# because the honest worst case is a multi-GB download on a slow connection
# followed by a minutes-long load — and the alternative to waiting is failing a
# request the user is already watching a progress row for. Bounded all the same:
# a wait with no end is a wedge, not a feature.
LOAD_WAIT_TIMEOUT_S = 3600.0

JOB_PREFIX = jobs.SERVER_ID_PREFIX + "ai-model:"
#: One row per RENDER, not per model: two images from the same pipeline are two
#: pieces of work with two progress bars, and a shared id would have the second
#: overwrite the first's row mid-flight.
IMAGE_JOB_PREFIX = jobs.SERVER_ID_PREFIX + "ai-image:"
#: And one row per RECORDING, for the same reason.
TRANSCRIBE_JOB_PREFIX = jobs.SERVER_ID_PREFIX + "ai-transcribe:"

#: One transcription in flight at a time, decided HERE rather than left to the
#: worker's `GENERATE_LOCK`.
#:
#: The worker serializes generations anyway — one model, one process — but it
#: does so by parking the second request inside `_single`, BEFORE that handler
#: reaches `heartbeat()`. So the second row got no ticks at all while it waited,
#: and with `TRANSCRIBE_TIMEOUT_S` at four hours that wait is long enough to hit
#: every timer in `jobs`: stalled at 30s ("no longer reporting" about work that
#: is merely queued), swept away at 600s, at which point `watchJob` resolves
#: with nothing and the page is told a still-running transcription failed. Two
#: 90-minute recordings back to back is all it takes.
#:
#: Waiting on THIS side of the request is what makes the wait describable. The
#: row says it is queued, keeps saying so, and its ✕ is honoured — none of which
#: is reachable from inside a blocked `urlopen`.
_TRANSCRIBE_LOCK = threading.Lock()

#: How often a queued transcription re-states itself.
#:
#: 1s, not the 5s this started at, and the number is set by EVICTION rather than
#: by staleness. `jobs._sweep` drops rows over `MAX_JOBS` (64) sorted by
#: `(state == RUNNING, updated_at)`, so among running rows the least recently
#: updated goes first — and a queue is exactly the situation that produces more
#: than 64 rows, since queueing is what a user pointing at a folder of
#: recordings is doing. Ticking slower than the decode being waited for made the
#: waiting rows the FIRST candidates for eviction, which is precisely backwards:
#: the active one is the one whose absence anybody would notice.
#:
#: Matching the worker's own per-segment cadence keeps every transcription row,
#: running or queued, equally recent. Staleness (`STALE_AFTER_S`, 30s) was never
#: the binding constraint; it is simply also satisfied.
_QUEUE_TICK_S = 1.0


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
    #: Unique per bring-up, and the reason the status and log files are named
    #: after it rather than after the capability. Two workers for one capability
    #: DO overlap — an eviction's replacement starts while the old one is still
    #: being killed, a Download runs beside a Load — and when they shared a path
    #: the second one's `unlink` deleted the port the first had just published,
    #: so the first waited out its whole bootstrap timeout on a file that would
    #: never come back. Not the token: a secret must not become a filename.
    uid: str = field(default_factory=lambda: secrets.token_hex(4))
    #: The `envinstall` key of the environment build running for this bring-up,
    #: while one is. Set because a worker in its VENV phase has no process of its
    #: own yet — the multi-GB `uv sync` belongs to a detached installer — so
    #: without this, "stop this worker" could not stop the only thing it was
    #: actually doing, and quitting the app during a first-ever runner build left
    #: gigabytes downloading with nothing left to cancel them.
    install_key: str = ""
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
#: model -> the weights-only fetch running for it right now.
#:
#: A separate table from `_workers` because a download is not residency — it
#: evicts nothing and holds no memory — but it IS something this machine is
#: doing, and leaving it out of `describe()` made it invisible: the AI Models
#: page polls job rows only while the runtime says something is happening, so a
#: pure Download reported progress that nothing was reading, and the sidebar
#: showed a quiet machine that was pulling 8GB.
_downloads: dict[str, dict] = {}
#: model -> the download-only WORKER fetching it, for as long as it is running.
#:
#: `_downloads` above is what `describe()` publishes and holds no process
#: handle; this holds the handle and is published nowhere. Two tables because a
#: weights-only fetch is the one worker that never enters `_workers` — it evicts
#: nothing and holds no memory — and that is exactly how it came to outlive the
#: app: `unload_all()` walked the residents at shutdown, found none, and left a
#: detached `snapshot_download` pulling gigabytes with nothing left to stop it.
_fetch_workers: dict[str, Worker] = {}
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


def _cleanup_files(worker: Worker) -> None:
    """Drop this bring-up's status and log files.

    Per-bring-up names mean they would otherwise accumulate one pair per load
    forever. Nothing is lost: a worker that failed has already had its stderr
    read into the error the job row carries, and this only runs once the process
    is gone. Best-effort — on Windows an unlink can lose a race with the child's
    last write, and a leftover log is not worth failing an unload over.
    """
    for path in (_status_path(worker), _log_path(worker)):
        try:
            os.unlink(path)
        except OSError:
            pass


def _terminate(worker: Worker) -> None:
    """Ask the worker to quit, then make sure of it.

    `/quit` first because a clean exit releases GPU buffers the OS is slower to
    reclaim; the kill is what happens when the process is wedged inside a
    generation and cannot get back to its accept loop.
    """
    # An environment build first, because during that phase it is the ONLY
    # thing this worker is doing: there is no process of ours to kill yet, and
    # the `uv sync` pulling several GB is detached, so it survives both the
    # thread and the app unless it is cancelled by name.
    if worker.install_key:
        from fused_render import envinstall

        envinstall.cancel(worker.install_key)
        worker.install_key = ""
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
    _cleanup_files(worker)


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


def _slug(worker: Worker) -> str:
    return worker.capability.replace("/", "-") + "-" + worker.uid


def _status_path(worker: Worker) -> str:
    """Where this worker publishes its port. One file per BRING-UP, never per
    capability — see `Worker.uid`."""
    return os.path.join(_worker_dir(), _slug(worker) + ".json")


def _log_path(worker: Worker) -> str:
    """Where a worker's stderr goes.

    A FILE, never `subprocess.PIPE`. A pipe nobody drains holds ~64KB before the
    child BLOCKS on its next write — so a worker that logs while downloading
    (hf and torch both do, at length) would wedge mid-load while still looking
    alive, and the pipe only gets read after exit, which by then never comes.
    """
    return os.path.join(_worker_dir(), _slug(worker) + ".log")


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
    status = _status_path(worker)
    try:
        os.unlink(status)
    except OSError:
        pass

    job = job_id_for(worker.model)
    log = _log_path(worker)
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


def _require_build_tools() -> None:
    """Refuse a load this machine cannot possibly build an environment for.

    Checked BEFORE a job row is opened, so a missing prerequisite is a 409 on
    the request with a sentence the page can show — not a row that appears, sits
    at "Preparing…", and dies with somebody's import error in it.

    Two things, and the second is the one that surprised us. `uv` is obvious.
    The **`fused` package** is not: `envinstall` is the loader for the fused
    engine (PY-18) and reads the base interpreter off that engine's live backend
    (`engine.get_backend()`, a hard import of `fused`), so a machine running the
    builtin engine cannot build ANY project venv — including a runner's. The
    symptom was a bare "ModuleNotFoundError: No module named 'fused'" on a
    download card, which says nothing about what to install or why a model
    download wanted it. The wording matches `_forced_engine`'s, because it is
    the same package and the same remedy.
    """
    if not shutil.which("uv"):
        raise SupervisorError("uv is not available, so the model environment cannot be built")
    from fused_render import engine

    if not engine.available():
        raise SupervisorError(
            "running a model needs the `fused` package: runner environments are "
            "built by the install loader, which builds them on the interpreter "
            "the fused engine runs code with. Install it with "
            "`pip install 'fused-render[fused]'`"
        )


def _failure_text(e: BaseException) -> str:
    """What to put on a failed job row, for any exception a bring-up threw.

    A `SupervisorError` is already a sentence written for the user — including
    the literal "cancelled", which the callers switch on — so it passes through.
    Anything else is a bug or an environment fault (the loader refusing to
    start, a spawn that could not exec), and its class name is the only part a
    user can act on or paste into a report, so the row names it rather than
    saying "failed". The traceback goes to the server log, where it is the only
    copy: this thread is the top of its own stack and nothing else will print it.
    """
    if isinstance(e, SupervisorError):
        return str(e)
    logger.exception("AI bring-up failed")
    return f"{e.__class__.__name__}: {e}".strip().rstrip(":")


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

    # ROUNDS, because an install is not always one install. On a machine with no
    # pinned interpreter yet (D214), the first `start()` fetches the INTERPRETER
    # under its own key and finishes; the packages are a SECOND call. Every
    # other caller of this loader is a page that re-POSTs, so the second round
    # was the client's — here there is no client, and without it a first-ever
    # runner build would sit at "Preparing…" forever having installed a python
    # and nothing else.
    for _ in range(_VENV_ROUNDS):
        # The key comes from `start()`, never from a second derivation of our
        # own: in bootstrap mode the two disagree BY DESIGN, and polling a key
        # nobody is writing is a record that never arrives. `envinstall._reported`
        # exists to hand the caller the right one — this is that caller.
        started = envinstall.start(runner.folder)
        key = started.get("key") or envinstall.venv_key_for(runner.folder)
        # Published on the worker so `_terminate` can cancel it. During this
        # phase the install IS the work, and it belongs to a detached process
        # that outlives us unless something says otherwise.
        worker.install_key = key
        while True:
            if worker.stopping or _cancel_requested(job):
                envinstall.cancel(key)
                raise SupervisorError("cancelled")
            record = envinstall.progress(key) or {}
            if record.get("done"):
                if record.get("error"):
                    raise SupervisorError(str(record["error"]))
                break
            _report(job,
                    detail=f"Preparing {runner.label} — {record.get('stage') or 'installing'}…")
            time.sleep(0.5)
        worker.install_key = ""
        if envinstall.is_installed(runner.folder):
            return envinstall.venv_python_for(runner.folder)
    raise SupervisorError(f"the environment for {runner.label} did not build")


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
            # BOTH, and the second is the one a user actually presses. `stopping`
            # is set by an eviction or an explicit unload — things the server
            # decided. The ✕ on the download row sets `cancel_requested` on the
            # JOB, which the env-build loop above already honours; without it
            # here, pressing ✕ during the phase that actually takes the time —
            # the multi-GB fetch the worker is doing — did nothing at all, and
            # the download ran to completion under a row that said cancelled.
            if worker.stopping or _cancel_requested(job):
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
    except BaseException as e:  # noqa: BLE001 - top of a thread; see below
        # EVERYTHING, not just SupervisorError. This is the top of a thread, so
        # an exception that escapes it is not raised to anyone — it kills the
        # only thing that was reporting, and the row it was reporting to sits at
        # its last detail until the manager gives up and says "the process
        # running it stopped reporting". Which is a lie in the one direction
        # that matters: the server is fine, the load is not running, and nothing
        # says so. A load that fails must SAY it failed.
        message = _failure_text(e)
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
        # Registered BEFORE the venv build, not after the spawn. Its first phase
        # may itself be a multi-GB `uv` install, and a stub that only appears
        # once there is a download process to kill is invisible to shutdown for
        # exactly the minutes that install runs — the same hole one layer up.
        # `_terminate` handles either phase: it cancels the install if that is
        # what is running, and kills the process if that is.
        _fetch_workers[model] = stub
    try:
        python = _ensure_venv(runner, stub, job)
        _report(job, detail="Fetching weights…")
        log = _log_path(stub)
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
    except BaseException as e:  # noqa: BLE001 - top of a thread; see _bring_up
        message = _failure_text(e)
        _report(job, state="cancelled" if message == "cancelled" else "error",
                message=None if message == "cancelled" else message)
    finally:
        with _lock:
            _worker_tokens.discard(stub.token)
            _downloads.pop(model, None)
            _fetch_workers.pop(model, None)
        _cleanup_files(stub)


def _runner_or_raise(capability: str) -> registry.Runner:
    """The runner serving `capability`, or a SupervisorError saying why there isn't
    one — the machine's reason ("needs Apple Silicon") where a runner exists but
    cannot run here, and a bare "no runner provides …" where none is registered."""
    runner = registry.for_capability(capability)
    if runner is None:
        known = next((r for r in registry.all_runners() if r.capability == capability), None)
        raise SupervisorError(
            known.available().reason if known
            else f"no runner provides {capability!r}"
        )
    return runner


def _start_resident(model: str, capability: str) -> tuple[dict, Worker]:
    """`load`'s residency path, handing back the WORKER RECORD beside the reply.

    Two callers, two needs. `load` is answering an endpoint, so the reply is all
    it wants. `_wait_ready` is about to sit and watch — and watching the
    `_workers` TABLE is not good enough, because `_bring_up`'s failure path drops
    the worker from the table in the same locked block that records why it
    failed. A waiter polling the table therefore finds the model gone and never
    the reason it went, which is how every failed image render came back as
    "was unloaded before it could be used" (D266). Holding the record, a waiter
    reads the error off the very object it was waiting for.
    """
    runner = _runner_or_raise(capability)
    _require_build_tools()

    job = job_id_for(model)
    with _lock:
        current = _workers.get(capability)
        if current is not None and current.model == model and current.state != "error":
            # Joining an in-flight bring-up hands back ITS record, so the second
            # caller watches the same thing the first one is watching.
            return {"jobId": job, "model": model, "state": current.state}, current
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
    return {"jobId": job, "model": model, "state": worker.state}, worker


def load(model: str, capability: str, *, weights_only: bool = False) -> dict:
    """Make `model` resident for `capability`; returns `{jobId, model, state}`.

    `weights_only` downloads and stops — the AI Models page's "Download", which
    must not evict whatever is currently loaded.

    Idempotent for the model already loading or loaded — a second call joins the
    first rather than starting a duplicate, which matters because two pages
    opening at once is the normal case, not the exotic one.
    """
    if not weights_only:
        return _start_resident(model, capability)[0]

    runner = _runner_or_raise(capability)
    _require_build_tools()

    job = job_id_for(model)
    with _lock:
        # A second Download on a model already being fetched joins the first.
        # Two `snapshot_download` runs over one cache directory is not a
        # faster download, it is a race for the same `.incomplete` files.
        if model in _downloads:
            return {"jobId": job, "model": model, "state": "downloading"}
        _downloads[model] = {"model": model, "capability": capability,
                             "jobId": job, "startedAt": time.time()}
    _report(job, title=model, state="running", kind="download", cancellable=True,
            unit="bytes", detail="Preparing…", done=None, total=None)
    threading.Thread(target=_fetch_only, args=(runner, model, job),
                     name=f"ai-fetch-{capability}", daemon=True).start()
    return {"jobId": job, "model": model, "state": "downloading"}


def image_job_id(uid: str) -> str:
    """The download-manager row for one render.

    Built here rather than by the router for the same reason `job_id_for` is:
    the `sys:` prefix is what makes a row unwritable by a page (BG-4a), so every
    id carrying it is minted in one place.
    """
    return IMAGE_JOB_PREFIX + "".join(c for c in uid if c.isalnum() or c in "._-")


def start_image(model: str, request: dict, job: str) -> None:
    """Open `job` and render on a thread. Raises before starting if it cannot.

    The runner check happens HERE, synchronously, so an image asked of a machine
    with no image runner answers the request with the reason instead of opening
    a job row that immediately fails — the caller gets an error it can show,
    rather than a progress bar it has to watch die.
    """
    runner = registry.for_capability(registry.IMAGE_GENERATION)
    if runner is None:
        known = next((r for r in registry.all_runners()
                      if r.capability == registry.IMAGE_GENERATION), None)
        raise SupervisorError(
            known.available().reason if known
            else f"no runner provides {registry.IMAGE_GENERATION!r}")
    _require_build_tools()

    title = str(request.get("prompt") or model).strip() or model
    _report(job, title=title[:80], state="running", kind="task", cancellable=True,
            unit="", detail="Preparing…", done=None, total=None)

    def run() -> None:
        try:
            result = generate_image(model, request, job)
        except BaseException as e:  # noqa: BLE001 - top of a thread; see _bring_up
            message = _failure_text(e)
            if message == "cancelled":
                _report(job, state="cancelled")
            else:
                _report(job, state="error", message=message)
            return
        _report(job, state="done", done=result.get("steps"), total=result.get("steps"),
                detail=f"Saved {os.path.basename(result.get('path') or 'image')}")

    threading.Thread(target=run, name="ai-image", daemon=True).start()


def _transcribe_title(request: dict, model: str) -> str:
    """The row's title: the FILE, not the model.

    The manager may be showing several of these at once and "meeting-2024.m4a"
    is what tells them apart. Shared by the row's opening report and by every
    queue tick, which must agree — a tick carrying a different title would
    rename the row under the user each time it reopened.
    """
    return (os.path.basename(str(request.get("path") or "")) or model)[:80]


def transcribe_job_id(uid: str) -> str:
    """The download-manager row for one transcription. See `image_job_id`."""
    return TRANSCRIBE_JOB_PREFIX + "".join(c for c in uid if c.isalnum() or c in "._-")


def start_transcribe(model: str, request: dict, job: str) -> None:
    """Open `job` and transcribe on a thread. Raises before starting if it cannot.

    The runner check is synchronous here for the reason `start_image` explains:
    a request a machine cannot serve should answer with the reason, not open a
    row that immediately dies.
    """
    _runner_or_raise(registry.SPEECH_TO_TEXT)
    _require_build_tools()

    title = _transcribe_title(request, model)
    # `unit="s"` from the first tick: the row is drawn before the worker knows
    # the duration, and a bar that starts unitless and acquires seconds later
    # relabels itself under the user.
    _report(job, title=title, state="running", kind="task", cancellable=True,
            unit="s", detail="Preparing…", done=None, total=None)

    def run() -> None:
        try:
            result = generate_transcript(model, request, job)
        except BaseException as e:  # noqa: BLE001 - top of a thread; see _bring_up
            message = _failure_text(e)
            if message == "cancelled":
                _report(job, state="cancelled")
            else:
                _report(job, state="error", message=message)
            return
        duration = result.get("duration")
        _report(job, state="done", done=duration, total=duration,
                detail=f"Saved {os.path.basename(result.get('output') or 'transcript')}")

    threading.Thread(target=run, name="ai-transcribe", daemon=True).start()


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
    """Server shutdown: nothing may outlive the app.

    Residents AND weights-only fetches. A download holds no memory, so it was
    not in the table `unload()` walks — and the consequence was the opposite of
    harmless: quitting the app left a detached `snapshot_download` pulling
    gigabytes, with the one thing that could stop it gone. The user's remedy was
    Activity Monitor.

    Its thread notices `stopping` within its half-second poll and reports the
    row cancelled, but shutdown does not wait for that: `_terminate` is what
    makes the process actually go, and the row is about to be forgotten anyway.
    """
    unload()
    with _lock:
        fetching = list(_fetch_workers.values())
    for stub in fetching:
        stub.stopping = True
        _terminate(stub)


def busy_reason(model: str) -> str | None:
    """Why `model`'s files must not be deleted right now, or None.

    The deletion endpoint owns the cache and the supervisor owns the processes,
    and neither could see the other: `shutil.rmtree` over a repo a worker is
    mid-`from_pretrained` on removes the shards it is still reading, and the
    failure surfaces minutes later as a corrupt-looking model rather than as
    "you deleted it". A resident model is worse — the weights are mapped, so on
    POSIX the delete "succeeds", the card says the model is gone, and it goes on
    answering until something unloads it and the bytes vanish for real.

    A REASON rather than a bool, because the endpoint says it out loud: "unload
    it first" and "wait for the download to finish" are different instructions.
    """
    with _lock:
        for worker in _workers.values():
            if worker.model == model:
                return ("in memory" if worker.state == "ready"
                        else f"being loaded ({worker.state})")
        if model in _downloads:
            return "being downloaded"
    return None


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


def _wait_ready(model: str, capability: str, job: str) -> Worker:
    """Make `model` resident, reporting the wait to `job`. Blocking.

    Text generation cannot do this — a chat box must not hang for the minutes a
    cold load takes, so `generate_text` fails fast with the job id instead. An
    image CAN: the caller already has a job to watch, because rendering a
    picture is minutes of work whether or not the weights were already in
    memory. So the wait is part of the job rather than a second failure the
    caller has to orchestrate around.

    The load reports to its OWN row (`sys:ai-model:<repo>`, with the download's
    byte counts); this row says only that the image is waiting on it. Two rows,
    two truths, and the manager shows both — but BOTH rows have to be able to
    say the same failure, which is what `_start_resident` returning the record
    is for (D266).
    """
    started, pending = _start_resident(model, capability)
    deadline = time.monotonic() + LOAD_WAIT_TIMEOUT_S
    while time.monotonic() < deadline:
        worker = ready_worker(capability, model)
        if worker is not None:
            return worker
        # Every read in ONE critical section, because `_bring_up`'s failure path
        # writes them in one: it stamps the error on the record AND drops the
        # record from the table without releasing the lock between. Read apart,
        # a waiter could catch the table already emptied and the error not yet
        # written, and report a phantom eviction for a load that failed with a
        # real message — the bug this ordering exists to make impossible.
        with _lock:
            state, error, detail = pending.state, pending.error, pending.detail
            evicted = _workers.get(capability) is not pending
        if state == "error":
            raise SupervisorError(error or "the model failed to load")
        if evicted:
            # Genuinely taken away rather than broken: another model claimed the
            # capability, or an unload landed. The record we hold never errored,
            # so there is no better answer than what happened to it.
            raise SupervisorError(f"{model} was unloaded before it could be used")
        if _cancel_requested(job):
            raise SupervisorError("cancelled")
        _report(job, detail=f"Waiting for {model} — {detail or state}…")
        time.sleep(0.5)
    raise SupervisorError(
        f"{model} did not finish loading in time (watch {started['jobId']})")


def generate_image(model: str, request: dict, job: str) -> dict:
    """Render one image. Blocking — call it on a thread, never on the loop.

    Loads the model first if it is not resident, which is the difference from
    the text path (see `_wait_ready`). The worker writes the PNG itself and
    reports its denoising steps straight to `job`, so nothing here polls: this
    function's whole job is to hold the request open and turn a dead worker into
    an error somebody can read.
    """
    worker = ready_worker(registry.IMAGE_GENERATION, model)
    if worker is None:
        worker = _wait_ready(model, registry.IMAGE_GENERATION, job)

    try:
        response = _worker_request(worker, "/generate", body={**request, "job": job},
                                   timeout=GENERATE_TIMEOUT_S)
    except (OSError, ValueError) as e:
        raise SupervisorError(f"the image process did not answer: {e}") from e
    with response:
        try:
            payload = json.loads(response.read().decode() or "{}")
        except ValueError as e:
            raise SupervisorError("the image process sent a malformed reply") from e
    if payload.get("cancelled"):
        raise SupervisorError("cancelled")
    if not payload.get("ok"):
        raise SupervisorError(str(payload.get("error") or "the image failed to render"))
    return payload.get("result") or {}


def _await_turn(job: str, title: str) -> None:
    """Take `_TRANSCRIBE_LOCK`, saying so on `job` for as long as it takes.

    Returns holding the lock — the caller releases it. Raises
    `SupervisorError("cancelled")` if the ✕ is pressed while waiting, which is
    the other half of why the wait lives here: a queued request has sent nothing
    to the worker, so there is nothing there to cancel and no tick coming back
    to carry the request.

    The uncontended case costs one non-blocking `acquire` and reports nothing,
    so a lone transcription's row is unchanged.

    **There is exactly ONE cancel check between acquiring and returning, and it
    is placed so that every route to "we hold the lock" passes through it.** The
    first cut checked only inside the polling loop, which is the contended
    route — so the fast path returned holding the lock having never read the
    flag, and a ✕ pressed on "Preparing…" with a model already resident still
    started a full-file decode that then had to be waited out by the next
    request. A guard an optimisation can skip is a guard in the wrong place.
    """
    if not _TRANSCRIBE_LOCK.acquire(blocking=False):
        _report(job, title=title, state="running", kind="task", unit="s",
                done=None, total=None,
                detail="Queued behind another transcription…")
        while not _TRANSCRIBE_LOCK.acquire(timeout=_QUEUE_TICK_S):
            if _cancel_requested(job):
                raise SupervisorError("cancelled")
            # Re-stated rather than left to go stale. The bar does not move —
            # there is nothing to say but "still waiting" — and that is exactly
            # what a heartbeat is (AI-5h).
            #
            # `title` rides along on EVERY tick, which is not redundancy. A
            # queued row is the first thing `jobs._sweep` evicts when the cap
            # bites (it sorts running rows by `updated_at`, and a queued row
            # ticks less often than the decode it is waiting for), and a row
            # that has been evicted can only be REOPENED by a report carrying a
            # title — without one `upsert` raises and `_report` swallows it, so
            # the ✕ silently stops working and the page is told a transcription
            # that is about to succeed has failed.
            _report(job, title=title,
                    detail="Queued behind another transcription…")
    if _cancel_requested(job):
        _TRANSCRIBE_LOCK.release()
        raise SupervisorError("cancelled")


def generate_transcript(model: str, request: dict, job: str) -> dict:
    """Transcribe one recording. Blocking — call it on a thread, never on the loop.

    The image path's shape exactly (`generate_image`), because the two have the
    same properties: minutes of work, a model that may need loading first, a
    file the worker writes itself, and progress that goes straight to `job`.
    Nothing here polls; this function holds the request open and turns a dead
    worker into an error somebody can read.

    **The turn is taken BEFORE the model is resolved, and that ordering is the
    whole of it.** Resolving first put the one destructive step in this path —
    `_wait_ready` -> `_start_resident`, which EVICTS whatever holds the
    capability when the model differs — outside the lock that exists to
    serialize transcriptions. A page asking for a different Whisper model while
    a 90-minute run was mid-decode terminated that worker, lost its transcript,
    failed its row with "the transcription process did not answer", and then
    queued behind a lock nobody was holding. The identical-model case failed
    more quietly: a worker handle captured before a wait that can last hours is
    a handle to a process an unload may since have killed, so the request went
    to a dead port instead of re-resolving.
    """
    _await_turn(job, _transcribe_title(request, model))
    try:
        worker = ready_worker(registry.SPEECH_TO_TEXT, model)
        if worker is None:
            worker = _wait_ready(model, registry.SPEECH_TO_TEXT, job)
        try:
            response = _worker_request(worker, "/generate", body={**request, "job": job},
                                       timeout=TRANSCRIBE_TIMEOUT_S)
        except (OSError, ValueError) as e:
            raise SupervisorError(f"the transcription process did not answer: {e}") from e
        with response:
            try:
                payload = json.loads(response.read().decode() or "{}")
            except ValueError as e:
                raise SupervisorError(
                    "the transcription process sent a malformed reply") from e
    finally:
        _TRANSCRIBE_LOCK.release()
    if payload.get("cancelled"):
        raise SupervisorError("cancelled")
    if not payload.get("ok"):
        raise SupervisorError(str(payload.get("error") or "the transcription failed"))
    return payload.get("result") or {}


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
        # Weights landing on disk, holding no memory and evicting nothing. The
        # BYTES are the job row's to report; this only says which models have
        # one in flight, which is what tells a page whether to read job rows at
        # all and what stops a Discover card claiming "✓ downloaded" over a pull
        # that is still running.
        downloading = [dict(row) for row in _downloads.values()]
    total = sum(row["residentBytes"] or 0 for row in loaded)
    return {
        "runners": registry.describe(),
        "loaded": loaded,
        "downloading": downloading,
        "totalResidentBytes": total or None,
    }


def reset() -> None:
    """Tests only: drop the table without touching real processes."""
    with _lock:
        _workers.clear()
        _downloads.clear()
