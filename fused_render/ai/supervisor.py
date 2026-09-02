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
for both — coexistence stays the DEFAULT, and `_start_resident`'s pre-spawn
budget gate only evicts an idle worker of ANOTHER capability when the two
genuinely will not both fit (`_select_budget_victims`, checked whether the
incoming model is a fresh capability spawn or a same-capability replace, since
a bigger replacement can just as easily blow the budget the OTHER capabilities'
warm workers already committed). It is an estimate, not a live reading — see
that function's own docstring for the honest limits.

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

import contextlib
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from fused_render import jobs
from fused_render.ai import catalog, fit, footprints, hub_metadata, hw_detect, registry

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

# How long a video request will wait for the worker to finish rendering.
# `TRANSCRIBE_TIMEOUT_S`'s reasoning restated for the other job that can
# genuinely run for hours on ordinary hardware: a high-resolution video
# render on an M3 can far exceed the image path's `GENERATE_TIMEOUT_S`
# (900s), and the
# precedent for a carve-out this wide is transcription's own four hours. Two,
# not four, because a render — unlike a multi-hour recording — is bounded by
# `frames`/`steps` this app itself clamps (`ai_runtime.py`'s video route), so
# the worst case here is a known ceiling rather than an open-ended file.
VIDEO_TIMEOUT_S = 2 * 3600.0

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
#: And one row per RENDER, same reasoning as `IMAGE_JOB_PREFIX`.
VIDEO_JOB_PREFIX = jobs.SERVER_ID_PREFIX + "ai-video:"
#: And one row per GENERATION, same reasoning as `IMAGE_JOB_PREFIX`: two
#: completions from the same resident model are two pieces of work with two
#: answers, and a shared id would have the second overwrite the first's row
#: mid-stream.
TEXT_JOB_PREFIX = jobs.SERVER_ID_PREFIX + "ai-text:"

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

#: How often a queued transcription re-states its row — a DISPLAY heartbeat and
#: nothing more.
#:
#: It was briefly sized against `worker_base.HEARTBEAT_S`, to decide which live
#: row `jobs._sweep` would shed first under the cap. That whole question is
#: gone: the sweep does not evict live server rows any more (see the cap branch
#: in `jobs.py`), which is what finally stopped this feature compensating for a
#: row it could lose. The one constraint left is the plain one — report often
#: enough that a row merely waiting its turn is not displayed as "no longer
#: reporting" (`jobs.STALE_AFTER_S`, 30s).
_QUEUE_TICK_S = 10.0

#: …and how often it looks at its ✕, which is a different question and so a
#: different number. A cancel must not wait on a display heartbeat.
_QUEUE_POLL_S = 1.0


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
    #: Whether THIS worker is the one that claimed the install named above, as
    #: opposed to one that joined an install already running. Set from
    #: `envinstall.start`'s own `claimed`, never inferred, and always set and
    #: cleared together with `install_key`.
    #:
    #: It words the ROW and nothing else (`_JOINED_INSTALL_DETAIL`). Whether the
    #: install may be cancelled is NOT this question and must not be answered
    #: from here: see `_install_waiters`. Ownership was tried as the condition
    #: and is wrong in both directions — a joiner cancelling on the key tore
    #: down a build others were waiting on, and an OWNER cancelling it killed
    #: the joiners just as dead, because `envinstall.cancel` writes its error
    #: into the shared record that every joiner is polling.
    install_owned: bool = False
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
    #: What the OS says the worker process is holding RIGHT NOW — macOS
    #: `phys_footprint`, i.e. the number Activity Monitor shows, RSS elsewhere
    #: (D597, `worker_base.os_footprint_bytes`). ADDITIVE beside
    #: `resident_bytes`: that field feeds `peak_resident_bytes` ->
    #: `footprints.py` -> `fit.py`'s "measured" rung, and redefining it would
    #: re-verdict every model the user has ever run.
    os_footprint_bytes: int | None = None
    #: The high-water mark of what this model has cost, as the runner
    #: reported it (SPEC AI-8c, D497) — `worker_base.peak_resident_bytes()`,
    #: which prefers a runner's own true-peak probe (`mx.get_peak_memory()`
    #: on MLX) over its own RSS high-water fallback. `refresh_memory` writes
    #: this into `footprints.py` on every poll that grows it, which is what
    #: `fit.py` (AI-16) reads back as the "measured" basis.
    peak_resident_bytes: int | None = None
    #: "cuda" | "mps" | "cpu", as the WORKER reported it — never as this process
    #: worked it out. The supervisor can see that a machine has a GPU and not
    #: whether the runner's torch was built to use it, and since D381 those
    #: differ BY DEFAULT everywhere: the default torch rows pin the `whl/cpu`
    #: build and the accelerated ones are opt-in. Same argument AI-8 makes
    #: about resident bytes: only the process holding the weights knows.
    device: str = ""
    #: Which rung of `torch_image._place()`'s ladder the worker actually
    #: landed on — "all-gpu" | "group-offload" | "offload", as the WORKER
    #: reported it via `set_state(placement=...)`, never inferred here. None
    #: from a runner that never calls `_place` (the CPU/MPS branches report
    #: nothing, and non-diffusers runners have no placement decision to make
    #: at all). Reaches `describe()`'s `"placement"` key the same way `device`
    #: above does, but unlike `device` nothing in `frontend/` reads it yet —
    #: `AiLoadedModel` has no `placement` field — so the value currently
    #: rides the API and stops there.
    placement: str | None = None
    loaded_at: float | None = None
    started_at: float = field(default_factory=time.time)
    #: `time.monotonic()` of the last thing this worker did, and how many turns
    #: are doing it right now. Seeded at construction — not left `None` until
    #: first use — so a model nobody has generated on yet still has a well-formed
    #: idle age rather than a crash in `reap_idle` (AI-13). Monotonic, not
    #: `time.time()`: see Key decisions in the AI-13 plan — wall-clock jumps
    #: (DST, NTP, a laptop's clock stepping on wake) must not make a model look
    #: idle for longer than it was, or fresher than it is.
    last_activity: float = field(default_factory=time.monotonic)
    in_flight: int = 0
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

#: An evicted worker, for as long as `_start_resident` is tearing it down
#: outside `_lock` (see that function). Popped from `_workers` the instant its
#: replacement is published, so for the ~9s worst case `_terminate` can take,
#: it exists nowhere `unload_all()` (walking `_workers` at shutdown) would ever
#: find it — quitting the app in that window used to leave the OLD process
#: running with nothing left tracking it to stop, the same orphan-holding-
#: gigabytes failure `unload_all`'s own docstring exists to prevent for
#: weights-only fetches. Added under the SAME lock hold that pops the worker
#: from `_workers`, removed under `_lock` once its `_terminate` call returns
#: (successfully or not); `unload_all` waits on it rather than re-terminating
#: it itself, since two threads calling `_terminate` on the same `Worker`
#: concurrently is its own hazard.
_draining: dict[str, Worker] = {}

#: `envinstall` key -> how many bring-ups are currently WAITING on that install.
#:
#: The one fact that decides whether an install may be killed, and it cannot be
#: read off any single worker. `envinstall.start` is single-flight per key: one
#: caller spawns the detached `uv sync` and every later caller joins and polls
#: the same record. `envinstall.cancel` kills that process AND writes
#: `error: "the install was cancelled"` into the shared record — so a cancel
#: issued by ANY worker, owner or joiner, is a cancel of every worker joined to
#: it: the others' next poll reads the error and raises past the retry loop.
#:
#: Hence a refcount rather than an ownership flag. The install dies when the
#: LAST worker waiting on it stops waiting, whatever the reason (a ✕, an
#: eviction, `unload_all` at shutdown) — which keeps the property this
#: cancellation exists for (nothing multi-GB outlives the app, and an install
#: nobody is waiting for is not left running) without ever taking down work
#: somebody else is still waiting on.
#:
#: A worker's share is held exactly while its `install_key` is set, so the key
#: is both the state and the token: `_hold_install` takes the share, and
#: `_release_install` gives it back once per worker however many times it is
#: called. Read and written under `_lock`.
_install_waiters: dict[str, int] = {}


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


def _touch(worker: Worker) -> None:
    """Re-stamp `last_activity` mid-turn, without touching `in_flight`.

    For the events inside a long stream: `_in_use` alone would only mark the
    worker busy at the START of a generation, and a transcription running
    twenty minutes on one request would look no fresher at minute nineteen
    than at minute one. The idle reaper (AI-13) reads only this stamp for a
    `ready` worker with nothing in flight, so a still-busy worker never needs
    this call to be spared — but the plan wraps every yielded chunk in it
    anyway, since a stalled loop on the worker side (no chunks, `in_flight`
    still 1) is exactly the leak `reap_idle`'s ceiling is for.
    """
    with _lock:
        worker.last_activity = time.monotonic()


@contextlib.contextmanager
def _in_use(worker: Worker):
    """Bracket one turn of generation on `worker`, for the idle reaper (AI-13).

    Stamps and increments on entry, stamps and decrements in `finally` — both
    under `_lock`, since `reap_idle` reads `last_activity` and `in_flight` from
    the reaper thread while a generation may be mutating them from its own.

    **The `finally` is what makes a client disconnect mid-stream release the
    counter.** `generate_text` is a generator built around this context
    manager: a page that stops iterating without calling `close()` still
    unwinds through here when the generator is garbage-collected, because a
    `with` block inside a generator runs its `finally` on `GeneratorExit` same
    as on a normal return. Without it, one abandoned stream would pin
    `in_flight` at 1 and hold the model resident forever — worse than no idle
    unload at all (see the leak-ceiling decision).
    """
    with _lock:
        worker.last_activity = time.monotonic()
        worker.in_flight += 1
    try:
        yield
    finally:
        with _lock:
            worker.last_activity = time.monotonic()
            worker.in_flight -= 1


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

#: The worker's working directory, when the spawn cannot pass `cwd=` — see
#: `_spawn_kwargs`. `worker_base.serve` reads it and `chdir`s first thing.
WORKER_CWD_ENV = "FUSED_AI_WORKER_CWD"


def _spawn_kwargs(cwd: str, env: dict) -> dict:
    """The Popen keyword arguments for a worker — and the ONE place the
    fork-versus-posix_spawn decision lives, because on macOS it decides whether
    a worker can start at all.

    **On macOS, workers are started with `posix_spawn`, never `fork()`.**
    CPython picks `posix_spawn` only for a narrow Popen shape (3.12
    `subprocess._execute_child`: no `cwd`, `close_fds=False`, no
    `start_new_session`, no `preexec_fn`, every redirected fd above 2), and
    falls back to `fork()` + `exec()` the moment any of those is set. The old
    shape here set three of them, so every worker was forked — and a forked
    child runs the parent's `pthread_atfork` child handlers before it ever
    reaches `exec()`.

    That is what killed the embeddings worker on a fresh machine, with
    `the worker exited before it started (code -11)` and an EMPTY stderr:
    the crash report shows the child dying in `do_fork_exec → fork →
    _pthread_atfork_child_handlers → osgeo::proj::io::SQLiteHandleCache →
    sqlite3Close → sqlite3_log → os_log_type_enabled → SIGSEGV`. PROJ (loaded
    into THIS server process through pyproj/shapely the moment any geo page
    renders) registers an atfork handler that closes its SQLite handles in
    the child, SQLite logs while doing so, and `os_log` after `fork()` is the
    documented macOS hazard. Nothing about the worker, its venv or MLX was at
    fault — the same worker started fine from a shell, and the same server
    could not start ANY worker once PROJ was resident. Reproduced exactly with
    a deliberately crashing atfork handler: the fork shape dies -11 every
    time, the `posix_spawn` shape never does.

    `posix_spawn` runs no atfork handlers, so the worker gets its own process
    with none of the parent's native state in the way. The two things the old
    shape did for the child — `cwd` and a fresh session/process group for
    `_kill_tree`'s `killpg` — the worker now does for itself, first thing in
    `worker_base.serve`: `chdir` to `WORKER_CWD_ENV` and `os.setsid()`.
    `close_fds=False` leaks only descriptors marked inheritable, which since
    Python 3.4 (PEP 446) is none of this server's by default.

    Linux and Windows keep the previous shape: glibc's fork is not where this
    hazard lives, and Windows never forks.
    """
    if sys.platform == "darwin":
        env[WORKER_CWD_ENV] = cwd
        return {"close_fds": False}
    return {"cwd": cwd, "close_fds": True, **SPAWN_KWARGS}

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


def _hold_install(worker: Worker, key: str, owned: bool) -> None:
    """Record that `worker` is waiting on the install `key` names.

    Under one lock with the key itself: the share and the record of holding it
    are the same fact, and a `_terminate` from another thread landing between
    two statements would either cancel an install with a waiter or leave a count
    nobody ever gives back.
    """
    with _lock:
        worker.install_key = key
        worker.install_owned = owned
        _install_waiters[key] = _install_waiters.get(key, 0) + 1


def _release_install(worker: Worker, cancel: bool = True) -> None:
    """`worker` stops waiting on its install; cancel it if nobody else is.

    Idempotent, because two threads legitimately release the same worker: its
    own bring-up thread on the way out of `_ensure_venv`, and `_terminate` from
    an eviction or `unload_all`. The key is the token — cleared inside the lock,
    so the second caller finds nothing to give back.

    `cancel=False` is for leaving an install that is already OVER, built or
    failed: there is no detached process to stop, and the record carries a
    resolver error somebody has to read. (`envinstall.cancel` refuses a `done`
    record anyway — it has no live pid to signal — so this is saying it rather
    than relying on it.) Every other exit walks away from an install that is
    still running, which is the case this whole mechanism is about.
    """
    with _lock:
        key = worker.install_key
        if not key:
            return
        worker.install_key = ""
        worker.install_owned = False
        left = _install_waiters.get(key, 1) - 1
        if left > 0:
            _install_waiters[key] = left
            # Somebody else is still waiting on this install, so it lives —
            # whatever happened to this worker. Cancelling here is what killed
            # every joiner of a cancelled owner: `envinstall.cancel` writes its
            # error into the record they are all polling.
            return
        _install_waiters.pop(key, None)
    if not cancel:
        return
    from fused_render import envinstall

    # Re-checked in a SECOND lock hold rather than folded into the one above,
    # because `envinstall.cancel` signals a pid and writes a small file, and
    # this module never holds `_lock` across I/O (see `ready_worker`,
    # `_claim_for_removal`) — doing so here would serialise every table
    # operation behind one process's local disk write.
    #
    # The gap that leaves is real: `_install_waiters.pop` above can be
    # followed by an entirely fresh `_hold_install` for this same key — a
    # `load()` that raced our departure, ran `envinstall.start()`, found the
    # install still alive, and joined it — all before we reach the line
    # below. `envinstall.cancel` has no way to tell that apart from an install
    # nobody wants any more (see its docstring: it only refuses an already-
    # DONE record), so calling it unconditionally is what let a cancel-then-
    # reload kill the very install the reload just joined.
    #
    # `key in _install_waiters` is that check: `_hold_install` re-adds `key`
    # the instant it registers a new waiter, so its presence here means a
    # fresh claim already exists and this worker's departure is no longer the
    # last word on the install's fate — cancelling would be undoing someone
    # else's join, exactly the bug this closes. This does not shrink the
    # window to zero (a rehold landing in the few bytecodes between releasing
    # the lock above and re-acquiring it here would still slip through), but
    # it closes the one that mattered in practice: an entire `envinstall.start`
    # round trip's worth of time, not a handful of instructions.
    with _lock:
        if key in _install_waiters:
            return
    envinstall.cancel(key)


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
    # Cancelled only once nothing is waiting on it any more (see
    # `_install_waiters`): this used to cancel whatever key was recorded, so
    # shutting one worker down killed a build another worker of the same runner
    # was joined to. `unload_all` terminates every worker, so the last one
    # through here still ends the install — which is the property that matters at
    # shutdown.
    _release_install(worker)
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


def _mirror_ok(model: str) -> str:
    """The repo id `model` may name to the mirror, or `""` (SPEC AI-5l, AI-5m).

    A repo id rather than a yes/no, because the two are not the same answer for
    every runner: `llamacpp-text`'s catalog ids are bare `.gguf` filenames and
    the worker names the recipe's REPO, which is what `mirror.allowed` compares
    against. `catalog.mirror_id` does that translation; a permission carrying the
    filename would be refused by the client and the mirror would be off for every
    llama.cpp model without a single symptom.

    The decision has to happen HERE, in the server process, because `catalog` is
    unreachable from a runner's interpreter — a worker imports `worker_base` and
    `mirror` as bare modules with no `fused_render` package on `sys.path`. But
    the reason it must happen here is a privacy one rather than a mechanical
    one: the worker is told the answer for the ONE model it was sent to fetch
    and for nothing else, so our distribution is never asked about a model the
    user picked from Discover, and we cannot learn that they downloaded it.

    Best-effort: a catalog that cannot be read is "not suggested", which leaves
    the download on the Hub path exactly as it is today.
    """
    if not model:
        return ""
    try:
        from fused_render.ai import catalog

        return catalog.mirror_id(model)
    except Exception:  # noqa: BLE001 - no answer means the Hub, which always works
        return ""


def _child_env(token: str, model: str = "", capability: str = "") -> dict:
    """Environment for a worker process.

    The PYTHON* vars are stripped for the reason `local_chat/chat.py` documents
    and the macOS bundle proves: the packaged app exports `PYTHONHOME` pointing
    inside itself, and a venv interpreter that inherits it dies at startup with
    "Failed to import encodings module" before running a line. The origin is
    passed through so the worker can report its own download progress.

    **No Hub token is placed here, deliberately** (D402). A worker imports
    `huggingface_hub` and therefore finds the machine's token exactly where every
    other hf caller finds it — hf's own store, written by the Preferences login
    button (routers/hf_auth.py) — or an `HF_TOKEN` this process already inherited
    and passes on in the copy below. Nothing in this app holds a credential to
    inject, and manufacturing one here would assert something about an
    environment the caller was asked nothing about.

    **`FUSED_MODEL_MIRROR_OK` is the model mirror's permission** and carries the
    repo id the worker will NAME to the mirror rather than a bare flag, so a
    value that arrived some other way cannot licence a probe for whatever the
    next download happens to be. That id is not always what this app calls the
    model — a curated GGUF is a filename here and a repo id there (AI-5m) — and
    `_mirror_ok` is what translates it. It is
    also POPPED when the answer is no, because this environment is a copy of the
    server's: an operator (or a parent process) exporting it would otherwise hand
    every worker permission for every model. `FUSED_MODEL_MIRROR` itself is left
    alone — an operator pointing it at staging is the supported way to use this,
    and unset now means the shipped default (`mirror.DEFAULT_BASE`), not "no
    mirror" — this permission is what still keeps that default from widening
    anything: a base URL alone names no repo, and only a suggested model's id
    ever reaches `FUSED_MODEL_MIRROR_OK`.

    **`FUSED_AI_MEMORY_BUDGET_BYTES` carries `fit.available_budget_bytes()`
    across the identical process boundary** (SPEC AI-24 item 14's real
    wiring) — a worker's bare-module interpreter cannot import
    `fused_render.ai.fit`/`hw_detect` (see `formats.py`'s own top-of-file
    note on why it stays stdlib-only), so the one place this figure CAN be
    computed is here, server-side, on every spawn — never once and cached,
    since `hw_detect`'s own background refresh means the answer can
    genuinely change between one worker's bring-up and the next. `llama_
    text.py`'s curated-recipe resolver is the one reader today. POPPED when
    the computation answers `None` (RAM itself unreadable), for the same
    "this environment is a copy of the server's" reason `FUSED_MODEL_
    MIRROR_OK` is: a stale or operator-set value must not silently outlive
    the fresh computation that is supposed to produce it.
    """
    env = dict(os.environ)
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "PYTHONSTARTUP"):
        env.pop(name, None)
    env["FUSED_AI_WORKER_TOKEN"] = token
    permitted = _mirror_ok(model)
    if permitted:
        # The id the WORKER will name to the mirror, which is not always the id
        # this app calls the model — see `_mirror_ok` and `catalog.mirror_id`.
        env["FUSED_MODEL_MIRROR_OK"] = permitted
    else:
        env.pop("FUSED_MODEL_MIRROR_OK", None)
    budget = fit.available_budget_bytes()
    if budget is not None:
        env["FUSED_AI_MEMORY_BUDGET_BYTES"] = str(int(budget))
    else:
        env.pop("FUSED_AI_MEMORY_BUDGET_BYTES", None)
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
    env = _child_env(worker.token, worker.model, worker.capability)
    proc = subprocess.Popen(
        [python, runner.worker, "--model", worker.model, "--status", status, "--job", job],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=open(log, "w"),
        env=env,
        **_spawn_kwargs(runner.folder, env),
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


def _job_record(job_id: str, records: list[dict] | None = None) -> dict | None:
    """One row from the registry, by id, or None.

    Pass an already-fetched `records` list when the caller needs MORE than one
    id out of the same instant — `_wait_ready`'s merged tick looks up both the
    caller's own row (for `_cancel_state`, below) and the load's row (for the
    progress it mirrors) once per 0.5s tick, and a second `jobs.list_jobs()`
    call would mean a second `_sweep` and a second lock acquisition for
    information the first call already produced. Omit it (the common case,
    every OTHER caller of `_cancel_state`) and this fetches its own — see that
    function's own docstring for why the fetch is `mark_read=False`.
    """
    for record in (jobs.list_jobs() if records is None else records):
        if record["id"] == job_id:
            return record
    return None


def _cancel_state(job: str, records: list[dict] | None = None) -> bool | None:
    """Was the ✕ pressed — or is there no row to ask?

    Three answers, not two, because "no row" is not "no". A row can be EVICTED
    by the cap at any moment (`jobs._sweep`), and `cancel_requested` is server
    state that no report can restore — so a poller reading a missing row as
    False is not observing that the ✕ was not pressed, it is guessing. The
    guess is usually right and occasionally silently loses a cancel, which is
    the worst combination for something nobody will ever see.

    Callers that can act on the distinction take the tri-state; the rest keep
    the boolean below, whose behaviour is unchanged.

    Deliberately calls `jobs.list_jobs()` (via `_job_record`, unless `records`
    is already given) with its default `mark_read=False`: this is a poll of
    our own (`_CANCEL_CHECK_INTERVAL_S`, 0.5s, for the whole duration of every
    model load), not a person looking at the corner, and marking a terminal
    row read here would start its retention clock from a poll nobody ever saw
    — see `list_jobs`'s own docstring, which names this exact function as the
    reason `mark_read` defaults to False.
    """
    record = _job_record(job, records)
    return None if record is None else bool(record.get("cancel_requested"))


def _cancel_requested(job: str) -> bool:
    return _cancel_state(job) is True


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
    _report(job, state="running", kind="download", detail=f"Preparing {runner.short}…",
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
        #
        # `report_job=False`: this loop already mirrors the SAME install into
        # `job` (`_report` below, titled with the model) — without this,
        # `start()`'s own generic `sys:env-install:<key>` row would open
        # alongside it, and a model's first load would show two jobs-dock
        # entries for the one `uv sync` actually running.
        started = envinstall.start(runner.folder, report_job=False)
        key = started.get("key") or envinstall.venv_key_for(runner.folder)
        # Published on the worker — and counted — so that stopping this bring-up
        # can stop the install when it is the only thing left waiting on it, and
        # cannot when it is not (`_install_waiters`). During this phase the
        # install IS the work, and it belongs to a detached process that outlives
        # us unless something says otherwise.
        _hold_install(worker, key, bool(started.get("claimed")))
        # Whether the install is still going when this worker walks away from
        # it, which decides whether walking away means cancelling it. An
        # install that has finished — built OR failed — has nothing to stop.
        still_running = True
        try:
            while True:
                if worker.stopping or _cancel_requested(job):
                    # This row stops, and the install stops only if nobody else
                    # is waiting on it — `finally` below, so a raise from
                    # anywhere in this loop settles the count exactly once.
                    raise SupervisorError("cancelled")
                record = envinstall.progress(key) or {}
                if record.get("done"):
                    still_running = False
                    if record.get("error"):
                        # A GENUINE build failure — a resolver error, a missing
                        # wheel — and it is reported verbatim rather than
                        # retried, which is the whole point of PY-18. It can no
                        # longer be a cancellation somebody else's ✕ wrote into
                        # this shared record, because a cancel now only happens
                        # once this is the last waiter (`_release_install`).
                        raise SupervisorError(str(record["error"]))
                    break
                # Two different things happen in this loop and they have to READ
                # differently: the owner is building the environment, the joiner
                # is parked behind somebody else's build. See
                # `_JOINED_INSTALL_DETAIL`.
                #
                # `activity`/`bytes_done`/`bytes_total` are `_env_install_worker`'s
                # (its `_UvProgress`, streaming uv's own stderr) — `None` before
                # uv has printed its first `Downloading` line, or when the
                # record came from an older/other writer that never learned
                # these keys. Falling back to `record["stage"]` in that case is
                # exactly what this line did before the byte-level work landed,
                # so a build with nothing to report yet (or a python-bootstrap
                # round, which never gets a tracker) reads identically to
                # before.
                #
                # Bytes go on the OWNER's row only. A joiner's row is not doing
                # any work (`_JOINED_INSTALL_DETAIL` exists specifically so it
                # does not read as though it were), so it must not draw a bar
                # that implies otherwise — see that constant's own comment.
                activity = record.get("activity") if worker.install_owned else None
                bytes_done = record.get("bytes_done") if worker.install_owned else None
                bytes_total = record.get("bytes_total") if worker.install_owned else None
                _report(job, detail=(
                    f"Preparing {runner.short} — {activity or record.get('stage') or 'installing'}…"
                    if worker.install_owned
                    else _JOINED_INSTALL_DETAIL.format(short=runner.short)),
                    done=bytes_done, total=bytes_total,
                    unit="bytes" if bytes_total else "")
                time.sleep(0.5)
        finally:
            _release_install(worker, cancel=still_running)
        if envinstall.is_installed(runner.folder):
            # The loop above breaks on `record.get("done")` BEFORE ever
            # reporting the terminal record — the byte counters it may have
            # set (owner only) therefore survive on the job row exactly as
            # they stood on the last "still downloading" tick, done == total.
            # `_bring_up` reports "Starting the model process…" right after
            # this return with no reset of its own (mirroring the ENTRY point
            # above, which does `done=None, total=None` for the same reason),
            # so without this a finished venv build would leave a full
            # "3.4 GB / 3.4 GB" bar sitting under that sentence until the
            # runner's own first weight tick overwrote it.
            _report(job, done=None, total=None, unit="")
            return envinstall.venv_python_for(runner.folder)
    raise SupervisorError(f"the environment for {runner.short} did not build")


#: How often `_bring_up`'s health-poll loop checks `jobs.list_jobs()` for a
#: cancel, independent of the loop's own health-poll cadence. See the comment
#: where it is used.
_CANCEL_CHECK_INTERVAL_S = 0.5


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
        #
        # `_cancel_requested` is checked on its own WALL-CLOCK cadence
        # (`_CANCEL_CHECK_INTERVAL_S`), not every health-poll tick: it calls
        # `jobs.list_jobs()`, which takes the global jobs lock, runs a sweep,
        # and `asdict()`s up to `MAX_JOBS` records — cheap once, but tying it
        # to the health poll's own interval means a future change to THAT
        # (this loop's `time.sleep` below went 0.5s -> 0.1s for load latency,
        # nothing to do with cancel responsiveness) silently changes how often
        # this contends with every `_report` call from every other loading
        # worker. Expressed in seconds for the same reason `_ERROR_GRACE_S`
        # in benchmark.py is: a poll-count budget silently tracks whatever the
        # poll interval happens to be.
        last_cancel_check = 0.0  # forces a check on the very first iteration
        while True:
            # BOTH, and the second is the one a user actually presses. `stopping`
            # is set by an eviction or an explicit unload — things the server
            # decided, and reading it costs nothing (an in-memory attribute).
            # The ✕ on the download row sets `cancel_requested` on the JOB,
            # which the env-build loop above already honours; without it here,
            # pressing ✕ during the phase that actually takes the time — the
            # multi-GB fetch the worker is doing — did nothing at all, and the
            # download ran to completion under a row that said cancelled.
            if worker.stopping:
                raise SupervisorError("cancelled")
            now = time.monotonic()
            if now - last_cancel_check >= _CANCEL_CHECK_INTERVAL_S:
                last_cancel_check = now
                if _cancel_requested(job):
                    raise SupervisorError("cancelled")
            if not _alive(worker):
                raise SupervisorError("the model process exited while loading")
            health = _health(worker)
            if health:
                worker.state = str(health.get("state") or "loading")
                worker.detail = str(health.get("detail") or "")
                resident = health.get("resident_bytes")
                worker.resident_bytes = resident if isinstance(resident, int) else None
                footprint = health.get("os_footprint_bytes")
                worker.os_footprint_bytes = (
                    footprint if isinstance(footprint, int) else None)
                # Read on every poll rather than once at `ready`: a runner sets
                # it inside `load()`, and this loop is what is watching when
                # that happens.
                worker.device = str(health.get("device") or "")
                # Same "read every poll, coerce falsy to the sentinel" shape
                # as `device` just above — `_place` sets this alongside
                # `device` inside `load()`, and a runner that never calls it
                # must report `None`, not the empty string.
                placement = health.get("placement")
                worker.placement = placement if isinstance(placement, str) else None
                if worker.state == "ready":
                    worker.loaded_at = time.time()
                    # AI-13: the idle clock starts HERE, not at construction.
                    # `last_activity` is otherwise seeded once, in the
                    # `Worker` dataclass, at the moment `_start_resident`
                    # builds the object — before this loop's `uv sync`, pull
                    # and load even begin. A first-ever multi-GB download
                    # that takes longer than the idle window would then
                    # become `ready` already past it, and the reaper's very
                    # next tick (<=30s) would unload it before a single
                    # request had used it — `generate_text` looping
                    # ModelNotReady -> load -> ready -> reaped forever, and
                    # the image/transcript paths worse still, since
                    # `_wait_ready` would hand back a worker the reaper is
                    # about to kill out from under the request. Every
                    # generation path re-stamps on its own first touch
                    # anyway (`_in_use`), so this only matters for the
                    # window between becoming ready and someone asking —
                    # which is exactly the window a slow bring-up ate.
                    worker.last_activity = time.monotonic()
                    _report(job, state="done", detail="Model loaded")
                    return
                if worker.state == "error":
                    raise SupervisorError(str(health.get("error") or "the model failed to load"))
            # 0.1s: `_health` is a local loopback GET, not a real network
            # call, so tightening THAT part of this loop costs nothing — and
            # `benchmark.py`'s own `_LOAD_POLL_S` wait sits on top of this
            # one, so the two used to stack into up to a full second of extra
            # latency per load at the old 0.5s each. The cancel check above is
            # deliberately NOT tied to this cadence any more — see
            # `_CANCEL_CHECK_INTERVAL_S`.
            time.sleep(0.1)
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
        env = _child_env(stub.token, model)
        proc = subprocess.Popen(
            [python, runner.worker, "--model", model, "--job", job, "--download-only"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=open(log, "w"),
            env=env,
            **_spawn_kwargs(runner.folder, env),
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
            # **The ✕ is checked before the exit code, because the WORKER can
            # beat this loop to it.** Both sides watch for a cancel — this loop
            # every 0.5s, and the worker's own fetch tick every 1s, which learns
            # about it from the reply (`worker_base.report_or_cancel`). When the
            # worker notices first it raises `Cancelled` and exits non-zero, and
            # the `proc.poll() is None` guard above then drops us straight here
            # without ever asking about the ✕ — so a download the user cancelled
            # was reported as a FAILED one, with a traceback in the message.
            # The flag is the honest answer either way: it is server state that
            # only a ✕ sets, and it survives the worker's death.
            if _cancel_requested(job):
                raise SupervisorError("cancelled")
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
        # A fetch that stopped before its first file leaves a cache folder with
        # nothing in it but bookkeeping, and the AI Models page then has to draw
        # that folder as a partly downloaded model (D437) — it cannot tell "no snapshot
        # yet" from "no snapshot ever" any other way (D424). So the thread that
        # made the folder tidies it on its way out. Reads the FOLDER, never this
        # function's outcome: a successful fetch has a snapshot and the call is a
        # no-op, and a cancel with real bytes in it keeps every one of them
        # because that is what a resume picks up (D275).
        #
        # Imported at call time, the same way `hub_cache` imports THIS module
        # inside `_require_not_in_use`: the two modules ask each other one
        # question apiece, and neither may need the other to be importable.
        from fused_render.ai import hub_cache

        hub_cache.discard_empty_shell(model)


def _runner_or_raise(capability: str) -> registry.Runner:
    """The runner serving `capability`, or a SupervisorError saying why there isn't
    one — the machine's reason ("needs Apple Silicon") where a runner exists but
    cannot run here, and a bare "no runner provides …" where none is registered.

    The sentence comes from `registry.unavailable_reason` rather than being
    derived again here. It used to be derived here, and in `start_image`, and in
    the registry: three copies of "the first runner registered for this
    capability", which was one rule while every capability had one runner and
    three wrong answers the moment text generation had two — all three named the
    Apple Silicon runner on machines that were never going to use it.
    """
    runner = registry.for_capability(capability)
    if runner is None:
        raise SupervisorError(registry.unavailable_reason(capability)
                              or f"no runner provides {capability!r}")
    return runner


#: Disables the pre-spawn budget gate below (SPEC AI-16/AI-19's fit
#: arithmetic, reused rather than re-derived — see `_predicted_footprint`).
#: Same "set AND sane" precedence `torch_image._vram_headroom_bytes` uses:
#: unset, unparsable, or anything other than a recognised on/off spelling all
#: degrade to the documented default, which is the GATE ON. Nobody
#: overriding this has any way to be "a little wrong" about it — either the
#: knob plainly says off, or the machine keeps the protection.
_MEMORY_GATE_ENV = "FUSED_RENDER_AI_MEMORY_GATE"
_MEMORY_GATE_FALSE = {"0", "false", "no", "off"}
_MEMORY_GATE_TRUE = {"1", "true", "yes", "on"}


def _memory_gate_enabled() -> bool:
    """Whether `_start_resident` runs its pre-spawn budget check at all.

    Read fresh on every call, the same live-re-read discipline `idle_workers`
    already uses for `prefs.effective_ai_idle_unload_minutes` — an operator
    flipping this mid-session takes effect on the very next load, not the
    next restart.
    """
    raw = os.environ.get(_MEMORY_GATE_ENV)
    if not raw:
        return True
    normalized = raw.strip().lower()
    if normalized in _MEMORY_GATE_FALSE:
        return False
    if normalized in _MEMORY_GATE_TRUE:
        return True
    return True


#: Runner codes whose loader caches K/V at q8_0 rather than fp16 — mirrors
#: `ai_runtime._KV_DTYPE_RUNNERS` exactly (same loader, same claim: the
#: `llamacpp-text`/`llamacpp-text-vulkan` pair is the complete set). Kept as
#: its own copy rather than imported from `server.routers.ai_runtime`: that
#: module imports `supervisor` already (it drives loads through it), so the
#: reverse import would be circular.
_KV_DTYPE_RUNNERS = {"llamacpp-text", "llamacpp-text-vulkan"}

#: `hub_metadata.cached()`'s camelCase field names -> `fit.footprint_bytes`'s
#: snake_case kwargs, mirroring `ai_runtime._KV_GEOMETRY_FIELDS` field for
#: field so the two never name the KV-cache term differently.
_KV_GEOMETRY_FIELDS = {
    "numHiddenLayers": "num_hidden_layers",
    "numKeyValueHeads": "num_key_value_heads",
    "numAttentionHeads": "num_attention_heads",
    "headDim": "head_dim",
    "hiddenSize": "hidden_size",
    "layerTypes": "layer_types",
}


def _kv_geometry_kwargs(model: str, runner_code: str | None) -> dict:
    """`fit.footprint_bytes`'s `num_hidden_layers`.../`kv_dtype` kwargs for
    `model`, loaded on `runner_code` — the gate's own copy of
    `ai_runtime._kv_geometry_kwargs`, over the same `hub_metadata.cached()`
    (no network call) and the same q8_0-KV-cache rule. Kept in sync by
    mirroring, not by sharing code, because the router already imports this
    module (see `_KV_DTYPE_RUNNERS`'s own comment on why the reverse import
    is not available).
    """
    meta = hub_metadata.cached(model)
    kwargs = {
        snake: meta[camel]
        for camel, snake in _KV_GEOMETRY_FIELDS.items()
        if meta.get(camel) is not None
    } if meta else {}
    if runner_code in _KV_DTYPE_RUNNERS:
        kwargs["kv_dtype"] = "q8_0"
    return kwargs


def _predicted_footprint(capability: str, model: str, store: dict | None,
                         runner_code: str | None = None) -> tuple[float | None, str | None]:
    """The ONE footprint expression `describe()` and the pre-spawn budget gate
    both call, so a model's cost can never read differently on the AI Models
    page than it did to the code that decided whether to evict something for
    it (see `_start_resident`'s docstring on the same point).

    Threads the curated entry's `size_gb`/`resident_gb`/`params`/
    `quantization` into `fit.footprint_bytes` when `catalog.entry_for` finds
    one — an uncurated (self-downloaded) model has none of these and falls
    through to whatever `footprint_bytes` answers off measured data alone,
    same as before this helper existed. `runner_code` (when the caller has
    one — `_start_resident` always does, `describe()`'s loaded rows always
    do, only an uncurated `_worker_footprint_bytes` call with no cached
    metadata answers nothing) also threads the KV-cache geometry through
    `_kv_geometry_kwargs`, the SAME term `ai_runtime._fit_verdict` includes
    in the AI Models page's fit badge — omitting it here was the gap where a
    text model's badge and its gate footprint could name different numbers
    for the identical model; this closes it rather than leaving a comment
    that only claimed it was already closed.

    **This is an ESTIMATE, not a live reading.** `footprint_bytes`'s own
    precedence — measured (this model, this machine, a past run) > declared
    (a curator's `resident_gb`) > download (a guess built from `size_gb` or
    `params`) — means a model nobody has run here yet is judged on a GUESS,
    and nothing on this side of the process boundary reads live available
    RAM to sanity-check it (no psutil, no `/proc/meminfo` — see `fit.py`'s
    own note on why not). A model whose only footprint is a download-size
    guess can still be admitted wrong.
    """
    entry = catalog.entry_for(capability, model) or {}
    return fit.footprint_bytes(
        capability, model,
        size_gb=entry.get("size_gb"), resident_gb=entry.get("resident_gb"),
        footprint_store=store, quantization=entry.get("quantization"),
        params=entry.get("params"), **_kv_geometry_kwargs(model, runner_code))


def _worker_footprint_bytes(worker: Worker, store: dict | None) -> float:
    """What `worker` counts as, for the budget gate's arithmetic.

    **A `ready` worker with a live `resident_bytes` reading is charged THAT
    figure, not its predicted footprint.** `_predicted_footprint` answers off
    `footprints.py`'s PEAK-resident rung when one exists — the high-water
    mark a load-time spike left behind — which is honest for judging an
    INCOMING model (nothing else is known yet) but wrong for a worker
    already up: `refresh_memory()` polls `resident_bytes` continuously, so a
    worker that peaked at load and has since settled is sitting on a real,
    current number the estimate has no reason to override. Counting the peak
    instead systematically over-charges every warm worker, which is exactly
    the over-counting that blocked loads a steady-state machine had room
    for.

    A non-`ready` worker (still downloading/loading its own weights) has no
    settled RSS to trust yet, so it falls through to the predicted footprint
    like before. Below that, `resident_bytes` is the last resort for a
    worker with no predicted footprint at all — real RSS is a real floor even
    when nothing measured/declared/download could answer, and treating an
    unknown footprint as ZERO would let the gate admit a load it should have
    refused.
    """
    if worker.state == "ready" and worker.resident_bytes:
        return float(worker.resident_bytes)
    footprint, _basis = _predicted_footprint(worker.capability, worker.model, store,
                                             worker.runner_code)
    if footprint is not None:
        return footprint
    return float(worker.resident_bytes or 0)


def _select_budget_victims(capability: str, model: str, footprint: float | None,
                           budget: float | None, store: dict | None, *,
                           exclude_capability: str | None = None) -> list[Worker]:
    """Which IDLE workers to evict, LRU-first, to make room for `model` — or a
    SupervisorError if evicting every eligible one still would not be enough,
    or the empty list (no eviction, load proceeds) when there is nothing this
    gate could improve by refusing.

    Called from inside `_start_resident`'s `_lock` hold, over the CURRENT
    `_workers` table — on BOTH the same-capability replace path and the
    cross-capability spawn path, BEFORE either one has mutated `_workers` at
    all. `exclude_capability` is `capability` itself on a replace (there is
    always at most one worker per capability, so this excludes exactly the
    outgoing worker, never accidentally a bystander) and `None` on a fresh
    cross-capability spawn where there is nothing occupying the slot to
    exclude. Excluding it from `committed` is how its bytes are credited —
    they are being freed unconditionally by the caller regardless of what
    this function decides, so counting them against the incoming model would
    double them against a slot that is not going to hold both at once — while
    the INCOMING model, if bigger than what it replaces, still has to clear
    everything ELSE that is resident.

    **Deciding before mutating, not after, is what keeps a raise here leaving
    `_workers` untouched.** If the caller popped the outgoing worker first
    and this function then raised, the outgoing worker would already be
    gone from `_workers` and stranded in `_draining` with nothing left to
    terminate it — a leak, and a capability slot refused into having NOTHING
    resident, which is a worse outcome than the overcommit this exists to
    prevent. `exclude_capability` lets this be evaluated as a pure query
    over `_workers` exactly as it stands, so the caller's own pop (and any
    victims' pops) can wait until this returns successfully.

    **`in_flight > 0` is never a candidate — mirrors `_is_idle`'s own
    exemption rather than reusing it, because the two protect against
    different mistakes.** `_is_idle`'s in-flight exemption is about the
    REAPER not mistaking a slow answer for staleness; this one is about not
    killing a worker a request is BLOCKED on to make room for a load that
    request never asked for. Either way, an automatic eviction that tears
    down a worker mid-render is the regression this function exists to never
    cause — so a busy worker is filtered out before ranking even starts, not
    merely ranked last.

    **A candidate must also be idle BY THE SAME CLOCK `reap_idle` uses** —
    `now - last_activity >= prefs.effective_ai_idle_unload_minutes() * 60` —
    not merely `ready` with nothing in flight. A worker that answered a
    request ten seconds ago is not "idle" in any sense a user would recognize
    just because nothing is running on it this instant; evicting it to make
    room for a page's very next call is the chat-model/image-model ping-pong
    the module docstring's coexistence promise exists to prevent, trading a
    coexistence problem for a "everything reloads from cold on every switch"
    problem that is worse. Given a genuine choice between spawning into
    contention (finding 1's cross-capability case, refused when there is
    something to refuse against) and reaping a model a human is actively
    switching between, refusing — and telling the caller why — is the
    honest answer; evicting silently is not. When the idle-unload preference
    itself is disabled (`minutes <= 0`), nothing here is idle either, which
    matches `reap_idle`'s own "disabled means never" reading of the same
    preference.

    **All-or-nothing: if the FULL set of idle candidates would not free
    enough room, nothing is evicted at all.** Silently evicting a pile of
    idle models for a load that gets refused anyway would be pure loss —
    other capabilities go cold and the new model still does not spawn. The
    refusal is the honest answer when the gate (an ESTIMATE — see
    `_predicted_footprint`) says this machine cannot hold both; unloading
    something on purpose, or trying again once room is free, is the caller's
    to decide.

    **Stepping aside is not the same as fitting.** When evicting everything
    idle still would not cover the shortfall, refusing is only the right
    call if refusing could plausibly help — if there is SOMETHING this
    machine's operator could unload, or something already committed that
    made the difference. A `_workers` table that is empty, or holds nothing
    this gate counts as committed, is cross-capability contention with
    nothing on the other side of it: the runner's own oversized-model paths
    (`llama_text`'s mmap + `_offload_schedule`, diffusers group offload) were
    already the answer for a model bigger than steady-state RAM before this
    gate existed, and a gate that refuses a load it cannot improve by
    refusing is pure regression, not protection.
    """
    if footprint is None or budget is None:
        # Nothing to compare — see `_predicted_footprint`'s own docstring on
        # why an unmeasured, uncurated model has no rung left to answer from.
        # The gate cannot judge a footprint it does not have, so it steps
        # aside rather than guessing; this is the SAME "unknown is a dash,
        # never a guess" rule AI-11a already applies everywhere else.
        return []
    others = [w for w in _workers.values() if w.capability != exclude_capability]
    committed = sum(_worker_footprint_bytes(w, store) for w in others)
    shortfall = footprint - (budget - committed)
    if shortfall <= 0:
        return []
    from fused_render.shell import prefs

    minutes = prefs.effective_ai_idle_unload_minutes()
    idle_window = minutes * 60 if minutes > 0 else float("inf")
    now = time.monotonic()
    candidates = sorted(
        (w for w in others if w.state == "ready" and w.in_flight == 0
         and now - w.last_activity >= idle_window),
        key=lambda w: w.last_activity)
    victims: list[Worker] = []
    freed = 0.0
    for worker in candidates:
        if freed >= shortfall:
            break
        victims.append(worker)
        freed += _worker_footprint_bytes(worker, store)
    if freed < shortfall:
        if not candidates and committed <= 0:
            # Nothing evictable and nothing already committed: there is no
            # contention here for the gate to referee, only an incoming
            # model the ESTIMATE says might not fit on its own. That is
            # exactly the case the runner's own oversized-model paths exist
            # for (see the docstring above) — step aside and let the load
            # attempt run, rather than refuse a load this gate cannot make
            # any better by refusing it.
            return []
        need_gb = footprint / fit.GB_BYTES
        free_gb = max(0.0, budget - committed) / fit.GB_BYTES
        pending = next((w for w in others if w.state != "ready"), None)
        if pending:
            # A `downloading`/`loading` worker counts toward `committed`
            # (it sums every worker in the table) but can never be a
            # candidate above (only `ready` ones are) — so "unload
            # something" names a blocker the caller has no unload button
            # for. The honest ask is to wait for that load to finish, since
            # its own bytes free up (or its footprint firms up) the moment
            # it either lands or fails.
            raise SupervisorError(
                f"{model} needs about {need_gb:.1f}GB and only {free_gb:.1f}GB is "
                f"free even after unloading every idle model — {pending.model} is "
                "still loading and holding room; try again once it finishes, or "
                "set FUSED_RENDER_AI_MEMORY_GATE=0 to load anyway")
        raise SupervisorError(
            f"{model} needs about {need_gb:.1f}GB and only {free_gb:.1f}GB is "
            "free even after unloading every idle model — unload something "
            "first, or set FUSED_RENDER_AI_MEMORY_GATE=0 to load anyway")
    return victims


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
    # Computed OUTSIDE `_lock`, same discipline `_terminate`'s teardown
    # follows below: `footprints.load_store()` is a disk read, and
    # `fit.available_budget_bytes()` can touch `hw_detect.cached_hardware()`
    # — neither belongs inside the hold that every other supervisor call
    # queues behind. `gate_footprint`/`gate_budget` are None outright when
    # the gate is off, which is what routes `_select_budget_victims` to its
    # own "nothing to compare" no-op below without a second on/off check.
    store = footprints.load_store()
    gate_on = _memory_gate_enabled()
    gate_footprint, _gate_basis = (
        _predicted_footprint(capability, model, store, runner.code) if gate_on
        else (None, None))
    gate_budget = fit.available_budget_bytes() if gate_on else None
    with _lock:
        current = _workers.get(capability)
        if (current is not None and current.model == model
                and current.runner_code == runner.code
                and current.state != "error"):
            # Joining an in-flight bring-up hands back ITS record, so the second
            # caller watches the same thing the first one is watching.
            #
            # The RUNNER has to match as well as the model. One model id can be
            # published for two backends — `openai/whisper-large-v3` exists as
            # an MLX conversion and as a CTranslate2 one — so "the model already
            # loading is the model you asked for" does not answer "the worker
            # loading it is the one that should serve you". Without this, a load
            # placed after the preference moved joined the outgoing engine's
            # bring-up and the switch never happened. A mismatch falls through
            # to the eviction below, which is what a change of engine means.
            return {"jobId": job, "model": model, "state": current.state}, current
        evicting = current is not None
        # Budget check runs on BOTH paths now — a same-capability replace as
        # well as a cross-capability spawn — and runs BEFORE anything below
        # mutates `_workers`, `current` included: `exclude_capability`
        # credits the outgoing worker's bytes (it is being freed
        # unconditionally, whatever this decides) without this function ever
        # popping it, so a `SupervisorError` here still leaves `_workers`
        # completely untouched. Popping `current` FIRST and letting this
        # raise afterward was tried and rejected — it stranded the outgoing
        # worker in `_draining` with no `_terminate` call left to reach it
        # (that call is below, reached only once this returns) and left the
        # capability with NOTHING resident, which is worse than the
        # overcommit this whole gate exists to prevent. Skipping this check
        # entirely on the replace path (as if freeing one capability's own
        # slot were always enough) was the gap a bigger incoming model on
        # that same slot walked straight through — a 1GB image model
        # swapped for a 9GB one runs face-first into exactly the memory
        # storm this gate exists to catch, unless the OTHER capabilities'
        # resident models are checked too.
        lru_victims = _select_budget_victims(
            capability, model, gate_footprint, gate_budget, store,
            exclude_capability=capability if evicting else None)
        if evicting:
            # Eviction: the weights of the old model must be released BEFORE the
            # new one's process is spawned, or the machine holds both at once —
            # which on 16GB of unified memory is the difference between a load
            # and a swap storm. `current.stopping = True` and popping it out of
            # `_workers` happen HERE, in the same locked block that inserts the
            # new worker below — so no other thread ever reads the capability
            # slot as briefly empty (and mints a competing worker into it) or
            # sees two workers resident for one capability at once. What moves
            # outside the lock is only the actual teardown I/O below
            # (`_terminate`, ~9s worst case: a `/quit` POST, SIGTERM+wait,
            # SIGKILL+wait, `proc.wait`) and the new worker's `_bring_up`
            # thread, which this function deliberately does not start until
            # AFTER that teardown returns — so the memory-overlap invariant
            # holds even though the lock is no longer what's enforcing the
            # ordering. `RLock`, not a plain `Lock`, so `_terminate`
            # re-acquiring `_lock` for its own bookkeeping below is not a
            # deadlock either way; this was always a latency problem
            # (everything else queued behind the ~9s teardown), not a
            # correctness one.
            current.stopping = True
            _workers.pop(capability, None)
            # Made visible to `unload_all` here — the SAME lock hold that pops
            # it from `_workers` — so there is no instant where the old worker
            # exists in neither table (see `_draining`'s own comment).
            _draining[current.token] = current
        for victim in lru_victims:
            victim.stopping = True
            _workers.pop(victim.capability, None)
            _draining[victim.token] = victim

        worker = Worker(model=model, capability=capability, runner_code=runner.code,
                        token=secrets.token_urlsafe(24))
        _workers[capability] = worker
        _worker_tokens.add(worker.token)

    if evicting:
        try:
            _terminate(current)
        except Exception:  # noqa: BLE001 - best-effort; see below
            # `_terminate` is best-effort internally (its own `/quit` call is
            # guarded), but not blanket-guarded: `_release_install` ->
            # `envinstall.cancel` and `_cleanup_files`'s callees can still
            # raise. Every OTHER caller of `_terminate` pops its target from
            # `_workers` BEFORE calling it, so a raise there never poisons a
            # live slot. This is the one call site where the NEW worker is
            # already published into `_workers[capability]` by the time this
            # runs — an uncaught raise here would leave that worker resident
            # in the table with its `_bring_up` thread never started, so
            # every later `load()` for this capability takes the join branch
            # above, hands back that permanently-"starting" record, and
            # `_wait_ready` blocks for `LOAD_WAIT_TIMEOUT_S` (an hour). The
            # eviction's job — releasing the OLD worker's resources — is done
            # as well as it can be; a failure in that best-effort cleanup
            # must not also break the NEW load it was clearing room for.
            logger.exception("failed to terminate evicted worker %r", current.model)
        finally:
            with _lock:
                _draining.pop(current.token, None)

    if lru_victims:
        # Same shape as the `evicting` teardown above — the I/O runs OUTSIDE
        # `_lock`, one victim at a time, best-effort per victim so a raise on
        # one does not strand the rest still marked `_draining`.
        reason = f"Unloaded to make room for {model}"
        for victim in lru_victims:
            try:
                _terminate(victim)
            except Exception:  # noqa: BLE001 - best-effort; see `evicting` above
                logger.exception("failed to terminate budget-evicted worker %r",
                                victim.model)
            finally:
                with _lock:
                    _draining.pop(victim.token, None)
            # `title=victim.model`: the victim's own job row, from whenever
            # IT loaded, is long since terminal (or pruned) by the time it
            # gets evicted here — `jobs.upsert` requires a `title` on the
            # FIRST report for an id (see its own docstring), and `_report`
            # swallows that `JobError` as best-effort, so without this the
            # download manager silently got no row at all for an eviction
            # that just happened. Passing the title makes the very report
            # this branch exists to send actually land, and land
            # identifiable, rather than fail invisibly the same way the
            # same-capability `evicting` path above deliberately does not
            # even attempt (that path already has nothing to report against
            # a model the user is actively replacing).
            _report(job_id_for(victim.model), title=victim.model, state="done",
                   detail=reason)

    _report(job, title=model, model=model, state="running", kind="download",
            cancellable=True, detail="Preparing…", done=None, total=None)
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
    _report(job, title=model, model=model, state="running", kind="download",
            cancellable=True, unit="bytes", detail="Preparing…", done=None,
            total=None)
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


def _start_render(capability: str, model: str, request: dict, job: str,
                   generate, *, noun: str, thread_name: str) -> None:
    """Open `job` and render `generate(model, request, job)` on a thread.
    Raises before starting if it cannot.

    Shared by `start_image` and `start_video`, which were near-byte-copies
    of this body differing only in the capability, which `generate` to call,
    and the noun in the terminal "Saved …" detail and the thread's name — a
    genuine format, not two things that happened to look alike once.

    The runner check happens HERE, synchronously, so a request asked of a
    machine with no runner for `capability` answers with the reason instead
    of opening a job row that immediately fails — the caller gets an error
    it can show, rather than a progress bar it has to watch die.
    """
    # `_runner_or_raise`, not a third copy of the same lookup — which is what
    # this was, and it drifted the moment a capability grew a second runner.
    _runner_or_raise(capability)
    _require_build_tools()

    title = str(request.get("prompt") or model).strip() or model
    # `model` rides as its own field (jobs.py `Job.model`), a dimmed suffix
    # JobRow draws after the title — never folded into `title` (that's the
    # prompt) or `detail` (that's the worker's progress ticks, which would
    # overwrite a model name concatenated there on the very next tick).
    _report(job, title=title[:80], model=model, state="running", kind="task",
            cancellable=True, unit="", detail="Preparing…", done=None, total=None)

    def run() -> None:
        try:
            result = generate(model, request, job)
        except BaseException as e:  # noqa: BLE001 - top of a thread; see _bring_up
            message = _failure_text(e)
            if message == "cancelled":
                _report(job, state="cancelled")
            else:
                _report(job, state="error", message=message)
            return
        _report(job, state="done", done=result.get("steps"), total=result.get("steps"),
                detail=f"Saved {os.path.basename(result.get('path') or noun)}")

    threading.Thread(target=run, name=thread_name, daemon=True).start()


def start_image(model: str, request: dict, job: str) -> None:
    """Open `job` and render an image on a thread. See `_start_render`."""
    _start_render(registry.IMAGE_GENERATION, model, request, job, generate_image,
                  noun="image", thread_name="ai-image")


#: What a queued transcription's row says while it waits.
_QUEUED_DETAIL = "Queued behind another transcription…"

#: What a download's row says while it waits for a runner environment ANOTHER
#: download is building (`_ensure_venv`, and only for a joiner —
#: `Worker.install_owned` is False). Same argument as `_QUEUED_DETAIL` above: a
#: wait a person can see is a wait the row has to name. Both rows used to read
#: "Preparing <runner> — <stage>…", so a download parked behind someone else's
#: multi-GB `uv sync` looked exactly like the one doing the work — and exactly
#: like one that had died. There is deliberately no percentage with it: nothing
#: here knows how far another worker's install has got, and inventing a number
#: is what `ModelProgress` refuses to do for precisely this phase.
_JOINED_INSTALL_DETAIL = "Waiting for the {short} environment — another download is building it…"


def transcribe_row_fields(title: str, model: str = "") -> dict:
    """Everything a report must carry for a transcription row to survive being
    RE-CREATED — the row's identity, as opposed to its progress.

    **Any row can be rebuilt from scratch at any tick.** `jobs._sweep` drops the
    least recently updated running row once `MAX_JOBS` (64) bites, and a queue
    of transcriptions is precisely what produces more than 64 rows — so a
    reporter whose tick omits a field does not update a row missing it, it
    creates one where that field is `Job`'s DEFAULT. `title` is the extreme
    case (without it `upsert` raises and the row never comes back at all), but
    it is only the loudest: `cancellable` defaults to False, which hides the ✕
    on a row the manager then draws with a DISMISS cross instead — operable
    looking and inert; `unit` defaults to "" and reverts the seconds clock to a
    bare pair of numbers; and `state` is what lets a `_forget`-ten row reopen
    at all, since a tick that does not say `running` is answered as a late tick
    from work the user already closed.

    So this is a PAYLOAD rather than a list of fields to remember, and it is
    passed to the worker in the request body rather than re-spelled there —
    every reporter on a transcription's lifecycle (this module's opening
    report, its queue ticks, its terminal report, and all four of the worker's)
    restates the same thing, and none of them can drift from it.

    `state` is deliberately NOT here: the terminal report needs `done`/`error`/
    `cancelled` and would have to override it. Callers say their own.

    `model` rides along the same way, for the same reason: a dimmed suffix on
    the title row (jobs.py `Job.model`) that a rebuilt row must not lose any
    more than it may lose its title.
    """
    return {"title": title, "model": model, "kind": "task", "cancellable": True,
            "unit": "s"}


def _transcribe_row(title: str, detail: str, model: str = "") -> dict:
    """`transcribe_row_fields` plus the progress of a row that has none yet."""
    return {**transcribe_row_fields(title, model), "state": "running",
            "done": None, "total": None, "detail": detail}


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


def text_job_id(uid: str) -> str:
    """The download-manager row for one text completion. See `image_job_id`."""
    return TEXT_JOB_PREFIX + "".join(c for c in uid if c.isalnum() or c in "._-")


def text_row_fields(title: str, model: str = "") -> dict:
    """Everything a report must carry for a text-generation row to survive
    being REBUILT — see `transcribe_row_fields`'s docstring for the full
    argument (a row can be recreated from scratch on any tick, so every
    reporter restates its identity rather than a mutable field somewhere
    holding it).

    Driven from `server/ai.py`'s `_local_relay`, not from this module: that
    is the caller that already turns a done-frame/error/cancellation into a
    verdict for both the streaming and non-streaming shapes, and it is the
    one that mints ids for the other kinds too (`image_job_id`,
    `transcribe_job_id`). `generate_text` itself stays fail-fast with no job
    of its own — see its own docstring — so there is nothing here shaped
    like `_transcribe_row`'s in-progress variant; the caller builds its own
    opening/tick/terminal payloads directly off this one.

    `unit="tokens"`: the row counts chunks emitted, not bytes or seconds —
    unlike a transcription's `"s"` or a download's `"bytes"`, the useful
    number here is how much has been said so far.
    """
    return {"title": title, "model": model, "kind": "task", "cancellable": True,
            "unit": "tokens"}


def start_transcribe(model: str, request: dict, job: str) -> None:
    """Open `job` and transcribe on a thread. Raises before starting if it cannot.

    The runner check is synchronous here for the reason `start_image` explains:
    a request a machine cannot serve should answer with the reason, not open a
    row that immediately dies.
    """
    _runner_or_raise(registry.SPEECH_TO_TEXT)
    _require_build_tools()

    # `unit="s"` from the first tick: the row is drawn before the worker knows
    # the duration, and a bar that starts unitless and acquires seconds later
    # relabels itself under the user. The payload is shared with the queue
    # ticks so an evicted row is rebuilt as the same row, not a partial one.
    title = _transcribe_title(request, model)
    _report(job, **_transcribe_row(title, "Preparing…", model))
    # The worker reports to this same row for the whole decode, so it needs the
    # row's identity to restate — it is a different PROCESS, and a tick of its
    # that arrives after an eviction would otherwise be dropped outright
    # (`upsert` refuses a first report with no title) and take the ✕, the
    # progress and the terminal state with it. Sent rather than re-spelled
    # there, so the two cannot disagree about what this row is.
    request = {**request, "row": transcribe_row_fields(title, model)}

    def run() -> None:
        # Every terminal report carries the identity too: the row may have been
        # evicted at any point during a decode that ran for hours, and a bare
        # `state="done"` would be refused, leaving the page watching a row that
        # never finishes for a transcript that is already on disk.
        fields = transcribe_row_fields(title, model)
        try:
            result = generate_transcript(model, request, job)
        except BaseException as e:  # noqa: BLE001 - top of a thread; see _bring_up
            message = _failure_text(e)
            if message == "cancelled":
                _report(job, **fields, state="cancelled")
            else:
                _report(job, **fields, state="error", message=message)
            return
        duration = result.get("duration")
        _report(job, **fields, state="done", done=duration, total=duration,
                detail=f"Saved {os.path.basename(result.get('output') or 'transcript')}")

    threading.Thread(target=run, name="ai-transcribe", daemon=True).start()


def _claim_for_removal(match) -> list[Worker]:
    """Atomically find-and-pop every worker `match(worker)` accepts.

    The shared atomic core `unload()` and `reap_idle()` both build on: the
    SELECTION and the POP happen inside one `_lock` hold, so whatever
    `match` decided stays true for every worker in the returned list — no
    caller re-scans `_workers` by name afterward, which is what would let a
    DIFFERENT worker that has since claimed the same capability be swept up
    by mistake, or let a worker's `in_flight`/`last_activity` change between
    being decided and being removed (see `reap_idle`'s docstring for the
    concrete failure that gap caused).

    Deliberately does NOT terminate or report here: `_terminate` does network
    I/O (a `/quit` POST, a process wait) and must run OUTSIDE `_lock`, or
    reaping one idle worker would block every other table operation for as
    long as that teardown takes.
    """
    with _lock:
        targets = [w for w in _workers.values() if match(w)]
        for worker in targets:
            worker.stopping = True
            _workers.pop(worker.capability, None)
        return targets


def _remove(targets: list[Worker], reason: str) -> None:
    """Tear down every worker in `targets` — already popped from `_workers` by
    `_claim_for_removal`, so this runs outside `_lock` with nothing left to
    race: `_terminate`'s I/O and `_report`'s job-row write."""
    for worker in targets:
        _terminate(worker)
        _report(job_id_for(worker.model), state="done", detail=reason)


def unload(model: str | None = None, capability: str | None = None,
          reason: str = "Unloaded") -> bool:
    """Stop a resident worker. True if there was one to stop.

    `reason` lands verbatim in the job row's `detail`, so every caller — a
    page's explicit unload, `evict_stale_engines`, a newer load claiming the
    capability, shutdown, and the idle reaper (AI-13) — can say which of them
    it was, without a parallel teardown for each.
    """
    targets = _claim_for_removal(
        lambda w: (model is None or w.model == model)
        and (capability is None or w.capability == capability))
    _remove(targets, reason)
    return bool(targets)


def evict_stale_engines() -> list[str]:
    """Unload any resident model whose capability now resolves to a DIFFERENT
    runner. Returns the models that were stopped.

    Called when an engine preference changes (D302). One capability holds one
    resident model (see the module docstring), and that model belongs to the
    backend that loaded it — a Whisper model resident in the CTranslate2 worker
    is not usable by the MLX one, they hold different formats and different
    weights. So switching a capability's engine while a model is loaded leaves a
    process holding gigabytes for a backend nothing will route to again: the
    memory stays spent, the AI Models page shows it as the resident model for
    that capability, and the next transcription starts a second worker beside
    it. Eviction is what makes the switch mean something.

    **Stated as a reconciliation rather than as "undo what that PUT did"**, and
    that is deliberate: the caller then needs no before/after bookkeeping, the
    call is idempotent, and it is correct for every other way the resolution can
    move under a resident model — a runner folder finishing its build, a
    preference edited into prefs.json by hand.

    Deliberately NOT touching `_downloads`: a weights-only fetch holds no memory
    and evicts nothing, and the bytes it is pulling stay useful — a user who
    switches engines mid-download almost certainly wants the download.
    """
    with _lock:
        stale = []
        for worker in list(_workers.values()):
            resolved = registry.for_capability(worker.capability)
            if resolved is not None and resolved.code != worker.runner_code:
                stale.append(worker)
    for worker in stale:
        unload(model=worker.model, capability=worker.capability)
    return [worker.model for worker in stale]


#: How often the reaper thread wakes up to evaluate `reap_idle` (AI-13). The
#: promise is "about five minutes" (`DEFAULT_AI_IDLE_UNLOAD_MINUTES`, 10 ->
#: 5), not a deadline, so a coarse tick is the right trade: worst case is one
#: tick of overshoot, and the job row's "Unloaded after N min idle" detail is
#: worded so an overshoot never reads as a crash. Kept well under any
#: plausible idle window so a `1`-minute window does not wait half its own
#: length to fire.
_REAPER_TICK_S = 30.0

_reaper_thread: threading.Thread | None = None


#: Margin added to a call's own request timeout before a still-positive
#: `in_flight` counter counts as LEAKED rather than busy. Generous on purpose:
#: this only has to be bigger than the slop between "the worker replied" and
#: "the supervisor finished decrementing", not tight.
_LEAK_CEILING_MARGIN_S = 300.0


def _leak_ceiling(capability: str, window: float) -> float:
    """How stale `last_activity` must be, for a WORKER WITH `in_flight > 0`,
    before it counts as leaked rather than busy (see `idle_workers`).

    Derived from the request timeout that actually bounds a call on this
    capability — `TRANSCRIBE_TIMEOUT_S` for `SPEECH_TO_TEXT`, `VIDEO_TIMEOUT_S`
    for `VIDEO_GENERATION`, `GENERATE_TIMEOUT_S` for text and image otherwise —
    plus a margin: past that bound `_worker_request` itself has already
    raised, so a counter still reading positive cannot be a slow answer, only
    a leaked one.

    `max(window, …)` rather than the timeout alone: a hand-set idle window
    already longer than the request timeout (someone dialling the reaper out
    to hours) must not be SHORTENED for a busy worker by this rule — the
    ceiling for a busy worker is never tighter than the ceiling for an idle
    one.
    """
    if capability == registry.SPEECH_TO_TEXT:
        timeout = TRANSCRIBE_TIMEOUT_S
    elif capability == registry.VIDEO_GENERATION:
        timeout = VIDEO_TIMEOUT_S
    else:
        timeout = GENERATE_TIMEOUT_S
    return max(window, timeout + _LEAK_CEILING_MARGIN_S)


def _is_idle(worker: Worker, now: float, window: float) -> bool:
    """Pure predicate: is `worker` past ITS OWN idle bound at `now`, given a
    window already resolved to seconds?

    Shared by `idle_workers` (a read-only report over a locked snapshot) and
    `reap_idle` (which must decide and remove in the SAME lock hold — see its
    docstring) so the two can never quietly diverge on what "idle" means.

    **Only `state == "ready"` is eligible.** A `starting` / `venv` /
    `downloading` / `loading` worker is not holding a finished model yet — a
    40-minute `uv sync` or an 8GB pull is activity, holds little memory, and
    killing it mid-build is hostile, not a memory win. `_fetch_workers`
    (weights-only downloads, which never enter `_workers` at all — see its
    docstring) are untouched for the same reason, simply by never appearing
    in what this is called over.

    **`in_flight > 0` DOES exempt a worker past its own idle window, up to a
    separate leak ceiling — this is NOT collapsible into one predicate.** The
    tempting simplification is "every chunk re-stamps `last_activity`, so a
    live call is never stale, so `in_flight` needs no exemption at all" — true
    for `generate_text`, and **only** for `generate_text`. `generate_image`
    and `generate_transcript` are single blocking `_worker_request` calls:
    `_in_use` stamps once on entry and nothing ticks again until the reply
    comes back, which can be up to `GENERATE_TIMEOUT_S` (900s) or
    `TRANSCRIBE_TIMEOUT_S` (4h) later. Collapsing the predicate reaps a
    90-minute transcription at the 10-minute mark, mid-decode — `_terminate`
    kills the very process the request is waiting on, which is exactly the
    failure `generate_transcript`'s lock-ordering comment is written to avoid
    ("lost its transcript, failed its row with 'the transcription process did
    not answer'"). So a busy worker is spared until `_leak_ceiling` — well
    past any legitimate call's own timeout — and only THEN does a
    still-positive `in_flight` mean a leaked stream (an abandoned
    `generate_text` iterator) rather than a slow answer.
    """
    if worker.state != "ready":
        return False
    return now - worker.last_activity >= _idle_bound(worker, window)


def _idle_bound(worker: Worker, window: float) -> float:
    """How stale `worker.last_activity` must be, in seconds, before it counts
    as idle — `_leak_ceiling` for a busy worker, the bare window otherwise.

    Split out of `_is_idle` so `describe()` can compute the SAME bound for
    `unloadsInSeconds`: a countdown computed against the bare window alone
    would count a busy transcription down to "unloads in under a minute" and
    then leave it sitting there, wrongly promising an unload the reaper's own
    predicate will not perform.
    """
    return _leak_ceiling(worker.capability, window) if worker.in_flight > 0 else window


def idle_workers(now: float) -> list[Worker]:
    """Ready workers the idle window (AI-13) says to unload, evaluated against
    `now`. A REPORT, not a decision anything acts on directly — see
    `reap_idle` for why the reaper does not call this and then `unload()`.

    Pure and side-effect-free: `now` is a caller-supplied `time.monotonic()`
    reading, never read internally, so a test can drive it with a synthetic
    clock and the reaper thread can drive it with the real one — no sleeping,
    no clock freezing, none of the timing-dependent flakes this repo's
    scheduling tests have a history of.

    The preference is read fresh on every call, not cached — a window edited
    mid-session, or an env override that comes and goes, applies on the very
    next tick.
    """
    from fused_render.shell import prefs

    minutes = prefs.effective_ai_idle_unload_minutes()
    if minutes <= 0:
        return []
    window = minutes * 60
    with _lock:
        return [w for w in _workers.values() if _is_idle(w, now, window)]


def reap_idle(now: float) -> list[str]:
    """Unload every worker the idle window (AI-13) names, evaluated against
    `now`. Returns the models stopped.

    **Decides and removes under ONE lock hold — deliberately NOT
    `idle_workers(now)` followed by a separate `unload()`.** That two-call
    shape has a real race: between `idle_workers` releasing `_lock` and
    `unload()` re-acquiring it to pop, the table is briefly unlocked, and a
    request can call `ready_worker()`, enter `_in_use()` and start a
    90-minute transcription on the very worker the reaper just condemned.
    `unload()` matches by model+capability alone and never re-checks
    `in_flight` or a fresher `last_activity`, so it would terminate the
    process that request is now waiting on — the exact failure the
    `in_flight` exemption exists to prevent, arriving back through the gap
    between deciding and acting rather than through the predicate itself.

    Holding `_lock` across the read AND the pop closes that gap by
    construction: `_in_use`'s entry also takes `_lock`, so nothing else can
    change `in_flight` or `last_activity` while this loop is deciding — a
    worker is either evaluated and removed atomically here, or a concurrent
    `_in_use` call finished first (blocking this call until it releases the
    lock) and this loop sees the fresh, post-increment state and spares it.
    There is no window for a third outcome.

    Built on the same `_claim_for_removal`/`_remove` pair `unload()` uses,
    not on `unload()` ITSELF: `unload()` re-scans `_workers` by NAME
    (model/capability), which would reopen a narrower version of the same
    hole if called a second time after this loop's own decision — it could
    match a DIFFERENT worker that has since claimed the same capability, and
    pin "Unloaded after N min idle" on a model that was never idle at all.
    `_claim_for_removal` takes a PREDICATE instead, evaluated once, atomically,
    over the exact snapshot this loop already decided against — so the
    workers reaped here are precisely the ones `_is_idle` said were idle,
    never a re-lookup that could answer a different question by the time it
    runs.
    """
    from fused_render.shell import prefs

    minutes = prefs.effective_ai_idle_unload_minutes()
    if minutes <= 0:
        return []
    window = minutes * 60
    reason = f"Unloaded after {minutes} min idle"
    targets = _claim_for_removal(lambda w: _is_idle(w, now, window))
    _remove(targets, reason)
    return [worker.model for worker in targets]


def start_reaper() -> None:
    """Start the idle-reaper thread, once per process.

    Idempotent via a module-level handle rather than a lock-guarded flag: the
    startup hook that calls this (server/app.py) can run more than once across
    the test suite's many `create_app` calls in one process, and a second
    thread ticking the same table is pure waste, not a correctness bug — but
    a waste that compounds by one thread per app instance created in a long
    test session.

    The body is `sleep` then `reap_idle(time.monotonic())` — no wall clock, so
    a laptop that sleeps mid-tick loses no window (Key decisions: the whole
    feature is built on the monotonic clock never advancing across a suspend).
    """
    global _reaper_thread
    if _reaper_thread is not None and _reaper_thread.is_alive():
        return

    def run() -> None:
        while True:
            time.sleep(_REAPER_TICK_S)
            try:
                reap_idle(time.monotonic())
            except Exception:  # noqa: BLE001 - a tick must never kill the loop
                logger.exception("idle-reaper tick failed")

    _reaper_thread = threading.Thread(target=run, name="ai-idle-reaper", daemon=True)
    _reaper_thread.start()


#: How often the background hardware-detection thread re-probes once it has
#: probed at least once (SPEC AI-18, D519; wiring per code review — the
#: probe had no caller in production, so `hw_detect.cached_hardware()`
#: always answered None and `fit._select_pool`/`speed._uncalibrated` always
#: took their no-GPU-known branch). Unlike `_REAPER_TICK_S`'s 30s (evaluating
#: something that changes by the minute), this is generous: a machine's
#: VRAM/GPU does not change while it is running, under ordinary use — this
#: interval exists mainly to notice an eGPU plugged in mid-session, not to
#: track something that moves often, and `hw_detect.detect_hardware` is a
#: real subprocess spawn (50-500ms) that has no business running often.
_HARDWARE_REFRESH_INTERVAL_S = 6 * 60 * 60  # 6 hours

_hardware_refresh_thread: threading.Thread | None = None


def _hardware_refresh_tick() -> None:
    """One probe-and-cache cycle — split out of `start_hardware_refresh`'s
    loop so a test can drive it directly with `hw_detect.refresh_hardware`
    monkeypatched, the same way `reap_idle(now)` is tested without ever
    starting `start_reaper`'s thread (see `tests/conftest.py`'s
    `_no_ai_idle_reaper_thread`, which documents why no test asserts a
    THREAD gets spawned)."""
    hw_detect.refresh_hardware(ram_gb=fit.machine_ram_gb())


def start_hardware_refresh() -> None:
    """Start the background GPU/VRAM-detection thread, once per process —
    the missing wiring `hw_detect.py`'s own docstring assumes exists:
    `detect_hardware()`/`refresh_hardware()` are a slow subprocess probe
    (`nvidia-smi`/`rocm-smi`/a PowerShell WMI+registry query/`sysctl`) that
    must never run on the verdict path, so `fit.py` and `speed.py` only ever
    read `hw_detect.cached_hardware()` — but until SOMETHING calls
    `refresh_hardware`, that cache never gets written, and both modules
    silently take their "no hardware known" branch forever. This is that
    something.

    Idempotent via a module-level handle, for the identical reason
    `start_reaper` is: the startup hook that calls this (`server/app.py`)
    can run more than once across the test suite's many `create_app` calls
    in one process.

    **One probe fires immediately**, unlike the reaper's sleep-then-tick
    shape — a fit verdict on the very first catalog request after server
    startup should not have to wait `_HARDWARE_REFRESH_INTERVAL_S` for a
    number to exist at all. The thread then sleeps and re-probes on that
    interval, forever. A failed tick (no vendor tool found, a hung spawn
    past `hw_detect._PROBE_TIMEOUT_S`, an `OSError` writing the cache) is
    logged and never kills the loop — the next tick tries again.
    """
    global _hardware_refresh_thread
    if _hardware_refresh_thread is not None and _hardware_refresh_thread.is_alive():
        return

    def run() -> None:
        while True:
            try:
                _hardware_refresh_tick()
            except Exception:  # noqa: BLE001 - a tick must never kill the loop
                logger.exception("hardware-refresh tick failed")
            time.sleep(_HARDWARE_REFRESH_INTERVAL_S)

    _hardware_refresh_thread = threading.Thread(
        target=run, name="ai-hardware-refresh", daemon=True)
    _hardware_refresh_thread.start()


#: How often the background Hub-metadata-warming thread re-sweeps the
#: curated id list (code review finding 1). Deliberately much shorter than
#: `_HARDWARE_REFRESH_INTERVAL_S`: unlike VRAM, `hub_metadata`'s own TTLs
#: (13 days positive, `NEGATIVE_TTL_SECONDS` — 1 hour — negative) are what
#: actually bound the network cost of a sweep, so a tight tick here costs
#: nothing extra for an already-fresh entry (`hub_metadata.get` returns
#: instantly without touching the network) while keeping a newly-expired
#: negative entry from sitting un-refreshed for hours. 20 minutes: shorter
#: than the negative TTL (so a repo that started publishing a `config.json`
#: is noticed inside one negative-TTL window) and long enough that a sweep
#: of the curated list is a rare event on the wire, not a busy loop.
_HUB_METADATA_REFRESH_INTERVAL_S = 20 * 60  # 20 minutes

_hub_metadata_refresh_thread: threading.Thread | None = None


def _hub_metadata_refresh_tick() -> None:
    """One sweep of `catalog.all_suggested_ids()` through `hub_metadata.get`
    — split out for the same testability reason `_hardware_refresh_tick` is
    (a test drives this directly, `hub_metadata.get` monkeypatched, without
    ever starting the thread).

    Every curated id, not only `text-generation` ones: `hub_metadata.get`
    is cheap to call for an id whose harvest nothing currently reads (a
    future caller — item 5's KV-cache term threading `hub_metadata` in, per
    that module's own docstring — should not need a second sweep wired up
    to start reading it), and the alternative (importing `registry`'s
    capability constants here to filter) buys nothing this module needs
    today. One repo's failure (a `get()` call that raises past its own
    `except Exception` — should not happen, but this loop must survive it
    regardless) is logged and does not stop the sweep for the rest.
    """
    for repo_id in catalog.all_suggested_ids():
        try:
            hub_metadata.get(repo_id)
        except Exception:  # noqa: BLE001 - one repo's failure must not stop the sweep
            logger.exception("hub-metadata refresh failed for %s", repo_id)


def start_hub_metadata_refresh() -> None:
    """Start the background Hub-metadata-warming thread, once per process —
    the request-path half of code review finding 1's fix.

    `ai_runtime._accepts_image`/`_capability_tags` used to call
    `hub_metadata.get(model_id)` directly from `describe_catalog`, which
    `hub_metadata.py`'s own module docstring is explicit is the wrong side
    of exactly the split `hw_detect.py` already drew for the identical
    reason: `get()` is a synchronous `urllib` GET with an 8-second timeout,
    and `describe_catalog` backs a route the picker polls. This mirrors
    `start_hardware_refresh`'s shape exactly — idempotent via a module-level
    thread handle, one sweep fires immediately so the first catalog request
    after startup already has warm entries rather than waiting a full
    interval, then the thread sleeps and re-sweeps forever. `ai_runtime.py`
    now calls `hub_metadata.cached()` only, which is a plain disk read and
    never touches the network — this thread is the only writer.
    """
    global _hub_metadata_refresh_thread
    if _hub_metadata_refresh_thread is not None and _hub_metadata_refresh_thread.is_alive():
        return

    def run() -> None:
        while True:
            try:
                _hub_metadata_refresh_tick()
            except Exception:  # noqa: BLE001 - a tick must never kill the loop
                logger.exception("hub-metadata refresh tick failed")
            time.sleep(_HUB_METADATA_REFRESH_INTERVAL_S)

    _hub_metadata_refresh_thread = threading.Thread(
        target=run, name="ai-hub-metadata-refresh", daemon=True)
    _hub_metadata_refresh_thread.start()


#: How long `unload_all` waits for an in-progress eviction's `_terminate` to
#: clear `_draining` before giving up on it. Generous over the ~9s worst case
#: (a 2s `/quit`, SIGTERM + 3s wait, SIGKILL + 3s wait, `proc.wait` + 1s) —
#: this only ever fires during the narrow shutdown-during-eviction race, and a
#: shutdown that gives up a little late is a much smaller failure than one
#: that walks away from a worker mid-teardown.
_DRAIN_WAIT_TIMEOUT_S = 15.0


def _wait_for_draining(timeout: float = _DRAIN_WAIT_TIMEOUT_S) -> None:
    """Block until no `_start_resident` eviction is mid-teardown.

    An evicted worker is popped from `_workers` (so a new load for its
    capability never joins it) before its `_terminate` call, which can take
    ~9s, runs OUTSIDE `_lock` — so for that window it exists in neither
    `_workers` nor anywhere else `unload_all` would find it, unless it is
    made visible here (see `_draining`'s own comment). Polling rather than
    re-terminating what it finds: the thread already mid-eviction is already
    calling `_terminate` on that exact `Worker`, and a second, concurrent
    call from here racing the first is its own hazard, not a fix.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _lock:
            if not _draining:
                return
        time.sleep(0.05)


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

    Waits for `_draining` to clear FIRST: a worker mid-eviction is invisible to
    `unload()`'s `_workers` walk (it was already popped so its replacement
    could take the slot), so shutting down inside that ~9s window used to leave
    the outgoing process running with nothing left tracking it — the same
    orphan-holding-gigabytes failure this function's own weights-only-fetch
    handling below exists to prevent.
    """
    _wait_for_draining()
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


def _drop_gone(worker: Worker) -> None:
    """Forget a worker whose PROCESS has exited: an `error` row that says so,
    and an empty slot the next request fills with a fresh load.

    Shared by `refresh_memory()` (the sidebar's poll, which decides with
    `_alive`) and `ready_worker()` (every generation request, which decides
    with the stricter `_exited`) so the two agree on what "gone" leaves behind.
    """
    worker.state = "error"
    worker.error = "the model process is gone"
    with _lock:
        if _workers.get(worker.capability) is worker:
            del _workers[worker.capability]


def _exited(worker: Worker) -> bool:
    """True only when the worker's process has DEFINITELY exited: a Popen is
    attached and `poll()` answered a real exit code.

    Stricter than `not _alive(worker)` on purpose, because this is asked on the
    request path. `_alive` reads a missing handle as dead — right for the
    reaper, which is tidying a table — but here "cannot tell" must not cost a
    caller its model: a worker planted without a Popen (every fixture in the
    tests, an adopted process) has nothing to poll, and a test double whose
    `poll()` returns a stand-in object is not reporting an exit code. Only an
    `int` is.
    """
    proc = worker.proc
    if proc is None:
        return False
    code = proc.poll()
    return isinstance(code, int) and not isinstance(code, bool)


def ready_worker(capability: str, model: str | None = None) -> Worker | None:
    """The resident, READY worker for a capability — what generation needs.

    **The ENGINE is part of the question, not only the capability and the
    model.** A worker belongs to the backend that started it, and resolution can
    move underneath one that is already loaded: `preferred_code` re-reads
    prefs.json on EVERY resolution with no cache, so a file edited by hand,
    restored from a backup or synced into the home directory changes the answer
    with no endpoint ever running. Serving the old worker anyway is the failure
    the whole feature exists to remove — the Preferences page saying CTranslate2
    while every transcription is answered by the resident MLX process.

    So a mismatch EVICTS rather than merely declining. Returning None alone
    would leave a worker nothing can ever route to holding its gigabytes until
    the next PUT, which may never come; `evict_stale_engines` is exactly that
    reconciliation, and calling it here rather than copying it keeps one
    definition of "stale". Idempotent, so the second request pays nothing.

    The resolution happens OUTSIDE `_lock` — it reads prefs.json off disk, and
    that is not something to do while holding the table every other thread
    wants. It costs one small JSON read per generation request, which is the
    price of the preference meaning something between PUTs.
    """
    with _lock:
        worker = _workers.get(capability)
        if worker is None or worker.state != "ready":
            return None
        if model is not None and worker.model != model:
            return None
    # **The 502-forever check.** A worker that died while idle — a SIGSEGV out
    # of a native library, a memory kill — used to stay in the table as `ready`,
    # so every request was proxied to a dead port and answered `the model
    # process did not answer`, indefinitely: nothing re-checked the process, so
    # nothing respawned it until an unload or the idle reaper cleared the slot.
    # Measured at 46 seconds of 502s on a photo-search app before its caller
    # gave up, against a ~10s respawn once somebody notices. Polling here turns
    # that first failed request into the `ModelNotReady` callers already wait
    # on. Outside `_lock`, like the resolution below: `poll()` is a syscall.
    if _exited(worker):
        _drop_gone(worker)
        return None
    resolved = registry.for_capability(capability)
    if resolved is not None and resolved.code != worker.runner_code:
        evict_stale_engines()
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

    with _in_use(worker):
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
                    event = json.loads(line.decode())
                except ValueError:
                    continue
                _touch(worker)
                yield event


def generate_embed(model: str, body: dict) -> dict:
    """One `{vectors, dim, model}` reply from the resident embedding model.

    The same fail-fast shape as `generate_text`, not the wait-inside-a-job shape
    `generate_image` and `_wait_ready` use: an embed call answers in
    milliseconds once the model is resident, so there is no job for a cold load
    to hide inside the way a multi-minute render has one already. A cold model
    therefore raises `ModelNotReady` — the load STARTS, its job id comes back on
    the exception, and the caller is meant to watch it and ask again, exactly as
    `/api/ai` already does for text.

    Blocking, and cheap to block on: unlike an image or a transcription this is
    one forward pass through a small tower, so holding the request open for it
    costs nothing the caller was not already waiting on.
    """
    worker = ready_worker(registry.EMBEDDINGS, model)
    if worker is None:
        with _lock:
            current = _workers.get(registry.EMBEDDINGS)
        if current is not None and current.model == model:
            raise ModelNotReady(
                f"{model} is still loading ({current.state})", job_id_for(model))
        started = load(model, registry.EMBEDDINGS)
        raise ModelNotReady(f"{model} is loading now", started["jobId"])

    try:
        response = _worker_request(worker, "/generate", body=body,
                                   timeout=GENERATE_TIMEOUT_S)
    except (OSError, ValueError) as e:
        raise SupervisorError(f"the model process did not answer: {e}") from e
    with response:
        try:
            payload = json.loads(response.read().decode() or "{}")
        except ValueError as e:
            raise SupervisorError("the model process sent a malformed reply") from e
    if not payload.get("ok"):
        raise SupervisorError(str(payload.get("error") or "the embedding failed"))
    return payload.get("result") or {}


def _wait_ready(model: str, capability: str, job: str,
                row: dict | None = None) -> Worker:
    """Make `model` resident, reporting the wait to `job`. Blocking.

    `row` is the caller's row IDENTITY, restated on every tick of the wait —
    see `transcribe_row_fields`. Optional because the image path does not yet
    supply one, and its ticks behave exactly as before without it; but a wait
    for a COLD model is the longest-running reporter in this module (a
    multi-GB pull), so it is the likeliest of all of them to be the tick that
    has to re-create an evicted row rather than update it. Without an identity
    it cannot: `upsert` refuses a first report with no title, `_report`
    swallows that, and the row stays gone for the whole download — no
    progress, no ✕, and a page told the transcription failed.

    Text generation cannot do this — a chat box must not hang for the minutes a
    cold load takes, so `generate_text` fails fast with the job id instead. An
    image CAN: the caller already has a job to watch, because rendering a
    picture is minutes of work whether or not the weights were already in
    memory. So the wait is part of the job rather than a second failure the
    caller has to orchestrate around.

    **The rows are MERGED while the wait lasts, not doubled.** The load still
    reports to its OWN row (`sys:ai-model:<repo>`, with the download's byte
    counts) — the AI Models page joins repo cards onto exactly that id, so it
    still has to exist and still has to be truthful even when nobody is
    waiting on it. But two rows both saying "waiting for FLUX to load" is the
    same fact told twice under different titles (SPEC §36 — one row per unit
    of work), so for as long as this wait holds, the CALLER's row mirrors the
    load row's own `detail`/`done`/`total`/`unit`/`total_scope` verbatim and
    sets `waiting_for` to the load's job id — a client-side filter
    (`jobs.ts` `mergedRows`) then hides the load row while `waiting_for`
    names it and it is still running, so the manager draws one row instead of
    two. The load row is never deleted or hidden on the SERVER: hiding it here
    instead would break that join.

    Deliberate non-goal: cancelling the MERGED row does not cancel the shared
    load. A render's ✕ stopping THIS wait must not stop a load another
    waiter (or the AI Models page's own download) may depend on — cancelling
    the merged row only ends this waiter, and the load's row simply
    reappears, with its own ✕, once nothing else is `waiting_for` it.

    The merge is cleared on EVERY exit from the wait — ready, error, evicted,
    cancelled, or timed out (the `finally` below) — because if the LOAD then
    fails, the load row must not still be hidden: D266's promise that both
    rows can show a real failure only holds for as long as the merge does not
    outlive the wait it describes. Two rows for two failures is right; two
    rows for one wait is not. The clearing tick restores the caller's own
    `unit` (`(row or {}).get("unit", "")` — the image/video opening report
    uses `unit=""`, a queued transcription's `transcribe_row_fields` pins
    `"s"`) and clears `done`/`total`, but deliberately leaves `detail` alone:
    a flash of blank detail between this tick and the caller's next one is
    worse than one stale line for a moment.

    Both rows have to be able to say the same failure regardless of the merge,
    which is what `_start_resident` returning the record is for (D266).
    """
    started, pending = _start_resident(model, capability)
    deadline = time.monotonic() + LOAD_WAIT_TIMEOUT_S
    unreadable = False
    try:
        while time.monotonic() < deadline:
            worker = ready_worker(capability, model)
            if worker is not None:
                return worker
            # Every read in ONE critical section, because `_bring_up`'s failure
            # path writes them in one: it stamps the error on the record AND
            # drops the record from the table without releasing the lock
            # between. Read apart, a waiter could catch the table already
            # emptied and the error not yet written, and report a phantom
            # eviction for a load that failed with a real message — the bug
            # this ordering exists to make impossible.
            with _lock:
                state, error, detail = pending.state, pending.error, pending.detail
                evicted = _workers.get(capability) is not pending
            if state == "error":
                raise SupervisorError(error or "the model failed to load")
            if evicted:
                # Genuinely taken away rather than broken: another model claimed
                # the capability, or an unload landed. The record we hold never
                # errored, so there is no better answer than what happened to it.
                raise SupervisorError(f"{model} was unloaded before it could be used")
            # ONE `jobs.list_jobs()` scan answers both lookups this tick needs
            # (see `_job_record`'s own doc) — the caller's row, for the cancel
            # check, and the load's row, for the progress this tick mirrors
            # onto it.
            records = jobs.list_jobs()
            cancel = _cancel_state(job, records)
            if cancel:
                raise SupervisorError("cancelled")
            if cancel is None and not unreadable:
                # No row to ask. Said once rather than every half-second, and NOT
                # treated as a cancel: a cold load is minutes of legitimate work
                # and aborting it on capacity pressure would be a worse failure
                # than the one this guards. The tick below rebuilds the row when
                # the caller gave us its identity, so the blind window is one
                # iteration rather than the whole download.
                unreadable = True
                logger.warning(
                    "job row %s is gone while waiting for %s; a cancel requested "
                    "now cannot be read until the row is rebuilt", job, model)
            load_row = _job_record(started["jobId"], records)
            # Built as a dict, not chained `**` unpacks, because the mirrored
            # keys (`unit` in particular) can already be present in `row`
            # (`transcribe_row_fields` pins `unit: "s"`) — a literal keyword
            # after `**row` for the same name is a `TypeError`, not an
            # override. Assignment order here IS the override: `row`'s
            # identity first, the mirrored progress after, so a caller row
            # that pins its own `unit` still shows the load's bytes while
            # this wait holds.
            tick = {**(row or {})}
            if row:
                tick["state"] = "running"
            if load_row is not None:
                # Verbatim — no "Waiting for <model> — " prefix and no extra
                # "…" appended. The model already renders as a dimmed suffix
                # via the `model` field, and the load's own detail already
                # ends in its own ellipsis; concatenating either doubled it.
                tick["detail"] = load_row["detail"] if load_row.get("detail") else (detail or state)
                tick["done"] = load_row.get("done")
                tick["total"] = load_row.get("total")
                tick["unit"] = load_row.get("unit")
                tick["total_scope"] = load_row.get("total_scope")
            else:
                # Best-effort: a missing load row (evicted between the check
                # above and here, or simply never read back yet) just means no
                # mirrored progress THIS tick, never an exception.
                tick["detail"] = detail or state
            tick["waiting_for"] = started["jobId"]
            _report(job, **tick)
            time.sleep(0.5)
        raise SupervisorError(
            f"{model} did not finish loading in time (watch {started['jobId']})")
    finally:
        # Every exit path — the `return` above, every `raise` above, and the
        # timeout `raise` at the end of the loop — passes through here, which
        # is the whole point: the merge must not outlive the wait it
        # describes (see the docstring's D266 paragraph).
        final = {**(row or {}), "waiting_for": "", "done": None, "total": None,
                 "unit": (row or {}).get("unit", "")}
        _report(job, **final)


def video_job_id(uid: str) -> str:
    """The download-manager row for one render. See `image_job_id`."""
    return VIDEO_JOB_PREFIX + "".join(c for c in uid if c.isalnum() or c in "._-")


def start_video(model: str, request: dict, job: str) -> None:
    """Open `job` and render a video on a thread. See `_start_render`.

    Raises before starting if it cannot — a request this machine cannot
    serve (no Apple Silicon) answers with the reason instead of opening a
    row that immediately dies.
    """
    _start_render(registry.VIDEO_GENERATION, model, request, job, generate_video,
                  noun="video", thread_name="ai-video")


def _generate_via_worker(capability: str, model: str, request: dict, job: str,
                          *, timeout: float, noun: str) -> dict:
    """Render one item through the resident `capability` worker. Blocking —
    call it on a thread, never on the loop.

    Shared by `generate_image` and `generate_video`, which were near-byte-
    copies of this body differing only in the capability, the request
    timeout, and the noun in each error sentence.

    Loads the model first if it is not resident, which is the difference from
    the text path (see `_wait_ready`). The worker writes the file itself and
    reports its own progress straight to `job`, so nothing here polls: this
    function's whole job is to hold the request open and turn a dead worker
    into an error somebody can read.
    """
    worker = ready_worker(capability, model)
    if worker is None:
        worker = _wait_ready(model, capability, job)

    with _in_use(worker):
        try:
            response = _worker_request(worker, "/generate", body={**request, "job": job},
                                       timeout=timeout)
        except (OSError, ValueError) as e:
            raise SupervisorError(f"the {noun} process did not answer: {e}") from e
        with response:
            try:
                payload = json.loads(response.read().decode() or "{}")
            except ValueError as e:
                raise SupervisorError(f"the {noun} process sent a malformed reply") from e
    if payload.get("cancelled"):
        raise SupervisorError("cancelled")
    if not payload.get("ok"):
        raise SupervisorError(str(payload.get("error") or f"the {noun} failed to render"))
    return payload.get("result") or {}


def generate_image(model: str, request: dict, job: str) -> dict:
    """Render one image. See `_generate_via_worker`."""
    return _generate_via_worker(registry.IMAGE_GENERATION, model, request, job,
                                timeout=GENERATE_TIMEOUT_S, noun="image")


def generate_video(model: str, request: dict, job: str) -> dict:
    """Render one video. See `_generate_via_worker`.

    `VIDEO_TIMEOUT_S` rather than `GENERATE_TIMEOUT_S`, because a
    high-resolution video render can run for far longer than any image
    request.
    """
    return _generate_via_worker(registry.VIDEO_GENERATION, model, request, job,
                                timeout=VIDEO_TIMEOUT_S, noun="video")


def _await_turn(job: str, title: str, model: str = "") -> None:
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
        _report(job, **_transcribe_row(title, _QUEUED_DETAIL, model))
        warned = False
        next_tick = time.monotonic() + _QUEUE_TICK_S
        # POLLED often, REPORTED rarely — and rebuilt ON DETECTION, which is
        # the part that makes the two cadences safe to differ.
        while not _TRANSCRIBE_LOCK.acquire(timeout=_QUEUE_POLL_S):
            # ONE pass over the rows answers both questions: was the ✕ pressed,
            # and is there still a row to press it on.
            cancel = _cancel_state(job)
            if cancel:
                raise SupervisorError("cancelled")
            if cancel is None:
                # **The row is GONE, so rebuild it now rather than at the next
                # scheduled tick.** The write cadence is a heartbeat, never the
                # mechanism for the row's survival: waiting `_QUEUE_TICK_S` to
                # notice would leave the row absent for ten seconds, and
                # `fused.watchJob` gives up after five consecutive misses —
                # about 3.5s — so the page would be told a transcription that is
                # merely QUEUED and about to succeed had stopped reporting.
                # Detection costs nothing: this poll has just read the list the
                # answer is in.
                #
                # `cancel_requested` cannot come back with it — it is server
                # state no report may set — so a ✕ pressed in the window before
                # the eviction is genuinely lost. Said out loud once rather than
                # silently believed, because reading False off a record we just
                # created is not an observation.
                #
                # NOT treated as a cancel: eviction under capacity pressure is
                # the ANTICIPATED case here (a folder of recordings is what
                # produces more than `MAX_JOBS` rows), so aborting on it would
                # fail the feature's main scenario to guard a one-poll window.
                if not warned:
                    logger.warning(
                        "transcription row %s was evicted while queued; rebuilt, "
                        "but a cancel requested just before that is lost", job)
                    warned = True  # once per wait; the sweep may do this often
                _report(job, **_transcribe_row(title, _QUEUED_DETAIL, model))
                next_tick = time.monotonic() + _QUEUE_TICK_S
                continue
            if time.monotonic() < next_tick:
                continue
            # The idle heartbeat, and only that. The bar does not move — there
            # is nothing to say but "still waiting" — which is exactly what a
            # heartbeat is (AI-5h). Deliberately slower than the running row so
            # the cap sheds queued rows first; see `_QUEUE_TICK_S`.
            _report(job, **_transcribe_row(title, _QUEUED_DETAIL, model))
            next_tick = time.monotonic() + _QUEUE_TICK_S
    # Guarded, because this runs while we HOLD the lock and before any caller's
    # `finally` exists to release it: `_cancel_state` walks `jobs.list_jobs()`,
    # and an exception escaping here would leave `_TRANSCRIBE_LOCK` — a module
    # global that is never re-created — held for the life of the process. Every
    # later transcription would then block in this function forever, showing
    # "Queued behind another transcription…" with nothing running. Low
    # probability; permanent and process-wide if it happens.
    try:
        cancelled = _cancel_requested(job)
    except BaseException:
        _TRANSCRIBE_LOCK.release()
        raise
    if cancelled:
        _TRANSCRIBE_LOCK.release()
        raise SupervisorError("cancelled")


@contextlib.contextmanager
def _transcribe_turn(job: str, title: str, model: str = ""):
    """`_await_turn` as a `with`, so the acquire and the release are one thing.

    The release used to live in a `try/finally` the CALLER opened after
    `_await_turn` returned, which left a window — the post-acquire cancel check
    — where the lock was held with no `finally` in scope. Pairing them here is
    the shape that cannot regress: there is no way to take this turn without
    also giving it back, and a future caller cannot forget.
    """
    _await_turn(job, title, model)
    try:
        yield
    finally:
        _TRANSCRIBE_LOCK.release()


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
    with _transcribe_turn(job, _transcribe_title(request, model), model):
        worker = ready_worker(registry.SPEECH_TO_TEXT, model)
        if worker is None:
            # The row identity travels into the wait too — it is the longest
            # reporter on this path, so it is the one most likely to meet an
            # evicted row.
            worker = _wait_ready(model, registry.SPEECH_TO_TEXT, job,
                                 row=request.get("row"))
        with _in_use(worker):
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

    **Also writes `peak_resident_bytes` into `footprints.py`** (SPEC AI-16a,
    D497) — this is the ONE place a load's peak becomes a durable "measured"
    footprint `fit.py` can read back later. Written here rather than at
    `_bring_up`'s own "ready" transition because a load's OWN peak can still
    be climbing after that moment (a first generation is often where a
    pipeline actually faults in the rest of its weights), and this function
    already re-polls `/health` on the cadence the rest of the app relies on —
    a second poll timer just to catch the peak would duplicate this one.
    """
    with _lock:
        current = list(_workers.values())
    for worker in current:
        if worker.state != "ready":
            continue
        if not _alive(worker):
            # The one thing the supervisor knows better than the worker does.
            _drop_gone(worker)
            continue
        health = _health(worker)
        if health and isinstance(health.get("resident_bytes"), int):
            worker.resident_bytes = health["resident_bytes"]
        # THE LIVE OS FOOTPRINT HAS TO BE RE-READ HERE (D599). It was assigned
        # only in the LOAD loop, which exits the moment the worker reaches
        # `ready` — so the value froze at whatever the last load-time poll saw,
        # before MLX had faulted in its Metal buffers. Measured on a live FLUX
        # worker: the row showed 436 MB against a real `phys_footprint` of
        # 24 GB, and 436 MB was a genuine reading of a worker that had not yet
        # allocated its GPU pool. This is the ONE function that keeps polling a
        # ready worker, so a figure that changes after load has to be read
        # here or it is never read again.
        #
        # ASSIGNED WHENEVER THE WORKER ANSWERED, including with None — unlike
        # the two `isinstance`-gated fields around it. Those two feed
        # `footprints.record` -> `fit.py`'s durable "measured" rung, where
        # holding the last known number through a failed poll is right. This
        # one is display-only and describes RIGHT NOW, so a worker that
        # answers "I have no such counter" (the non-Darwin fallback) must
        # clear the cell rather than leave a stale number standing next to a
        # live one. A poll that FAILED OUTRIGHT (`health` falsy) still leaves
        # the previous value alone, which is the transient case.
        if health:
            footprint_now = health.get("os_footprint_bytes")
            worker.os_footprint_bytes = (
                footprint_now if isinstance(footprint_now, int) else None)
            # Same display-only, re-read-every-poll treatment as the OS
            # footprint just above — `_place` only runs once, at `load()`,
            # but re-reading it here rather than trusting the load loop's
            # last value keeps this worker's copy honest if a future runner
            # ever calls `set_state(placement=...)` again after `ready`.
            placement_now = health.get("placement")
            worker.placement = placement_now if isinstance(placement_now, str) else None
        if health and isinstance(health.get("peak_resident_bytes"), int):
            worker.peak_resident_bytes = health["peak_resident_bytes"]
            # Best-effort (code review): `describe()` calls this
            # unconditionally on every `GET /api/ai/runtime`, and a footprint
            # is a nice-to-have observation for a LATER fit verdict, not
            # something worth 500ing a status route over. `footprints.record`
            # writes through `storage.write_json`, which can raise `OSError`
            # (a full disk, a permissions problem, a home directory that went
            # away mid-session) — `worker.peak_resident_bytes` above is
            # already set and is what this route actually exists to report,
            # so that assignment must survive a write failure that comes
            # after it.
            try:
                footprints.record(worker.capability, worker.model,
                                  health["peak_resident_bytes"])
            except OSError:
                logger.exception(
                    "failed to record the measured footprint for %r/%r",
                    worker.capability, worker.model)


def resident_models() -> set[str]:
    """Which models are HELD right now — the weights in memory, ready to answer.

    `describe()` answers the same question far better (state, device, bytes) and
    charges a health request per live worker for it, which is the right trade for
    the sidebar and the wrong one for `/api/ai/catalog`: that route is a slow
    inventory a picker polls, and it only needs the boolean. Split out rather than
    folded into `describe()` so the cheap answer stays cheap.

    **`state == "ready"`, not merely "in the table".** A Worker is inserted at
    `starting` and passes through `venv`, `downloading` and `loading` on its way —
    which for a first-ever bring-up is a multi-minute `uv sync` followed by a
    multi-GB fetch. Reporting every row would have flipped a picker's "loaded" mark
    the instant the button was pressed and left it lit through the whole download,
    which is the opposite of what the mark promises.

    **And still running.** `_alive` is `proc.poll()` — no signal, no request, and it
    reaps the zombie as it asks — so a worker that crashed after reaching `ready`
    reads as gone here rather than as loaded forever. Without it this path would be
    the one place in the supervisor that trusts `state` alone: `refresh_memory()`
    reaps on exactly this check, and skipping the probe is not a licence to skip the
    liveness too.
    """
    with _lock:
        return {w.model for w in _workers.values()
                if w.state == "ready" and _alive(w)}


def describe() -> dict:
    """The runtime as the API reports it."""
    refresh_memory()
    # Read once for the whole snapshot rather than per row: the reaper's own
    # window (AI-13), so "unloads in…" on the page is the same countdown that
    # actually fires, not a second copy of the precedence rule.
    from fused_render.shell import prefs

    minutes = prefs.effective_ai_idle_unload_minutes()
    window = minutes * 60 if minutes > 0 else None
    now = time.monotonic()
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
                # Which rung of `torch_image._place()`'s ladder the weights
                # actually landed on — "all-gpu" | "group-offload" |
                # "offload". None from a runner that never calls `_place`.
                # Rides through the API the same way `device` below does, but
                # `AiLoadedModel` (`frontend/src/platform/lib/api.ts`) has no
                # `placement` field yet, so no page reads this key.
                "placement": w.placement,
                # The OS's "right now" figure (D597) — what a user's system
                # monitor shows, which `residentBytes` does NOT on Apple
                # Silicon (Metal buffers are charged to `phys_footprint`, not
                # RSS). Null where no counter could be read; never coerced.
                "osFootprintBytes": w.os_footprint_bytes,
                # What the weights actually landed on. None from a runner that
                # does not report one, which the page renders as nothing rather
                # than as a guess.
                "device": w.device or None,
                "loadedAt": w.loaded_at,
                "startedAt": w.started_at,
                "jobId": job_id_for(w.model),
                # How long since anything used this worker, and — when the idle
                # window is on — how much longer it has. Null rather than a
                # number that never counts down: a page must not draw a
                # countdown for a window that is disabled.
                "idleSeconds": max(0.0, now - w.last_activity),
                # Against `_idle_bound` — the SAME bound `_is_idle` reaps
                # by — never the bare window: a busy worker (`in_flight > 0`)
                # is spared until `_leak_ceiling`, and counting down against
                # `window` alone would run a 90-minute transcription's card
                # to "unloads in under a minute" and leave it sitting there
                # for the rest of the hour, wrongly promising an unload the
                # reaper will not perform.
                "unloadsInSeconds": (
                    None if window is None
                    else max(0.0, _idle_bound(w, window) - (now - w.last_activity))
                ),
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
    # WHAT EACH MODEL ACTUALLY COSTS, and the ceiling it has to fit under
    # (D594). `residentBytes` above is the worker process's RSS — real, but
    # "not the model's size" (its own field comment says so), which is why the
    # summed version was removed from the status-bar chip. The honest per-model
    # figure is `_predicted_footprint`, the SAME expression `_start_resident`'s
    # pre-spawn budget gate calls — reused rather than re-derived so a model's
    # cost here and the number the gate weighed it against can never drift
    # apart (see that function's own docstring).
    #
    # ONE `load_store()` for the whole response, passed into every call — the
    # store is a disk read plus a machine-identity check, and doing it per row
    # is the cost `footprint_bytes`' own `footprint_store` parameter exists to
    # avoid (SPEC AI-16).
    store = footprints.load_store()
    for row in loaded:
        footprint, basis = _predicted_footprint(row["capability"], row["model"], store,
                                                row["runner"])
        # NULL IS NOT ZERO: a model with nothing measured and nothing declared
        # has NO cost figure, and the page must fall back to RSS alone rather
        # than colour a guess or print 0.
        row["footprintBytes"] = footprint
        row["footprintBasis"] = basis
    return {
        "runners": registry.describe(),
        "loaded": loaded,
        "downloading": downloading,
        "totalResidentBytes": total or None,
        # THE DENOMINATOR, once per payload rather than per row — it is a
        # per-machine constant. The WIRED limit where it applies, not raw total
        # RAM: on Apple Silicon that is the real ceiling a model has to fit
        # under (`fit._DEFAULT_WIRED_FRACTION`), and colouring against total
        # RAM would call a model comfortable while it is about to swap. Total
        # RAM is the fallback off Darwin, and None when neither can be read —
        # in which case there is no ceiling to colour against and the page
        # shows the figure uncoloured.
        "memoryCeilingBytes": _memory_ceiling_bytes(),
    }


def _memory_ceiling_bytes() -> float | None:
    """What a model has to fit under on this machine, in bytes, or None.

    The wired limit first (`fit._wired_limit_bytes` — Apple Silicon's hard
    ceiling, which is what actually bounds a model there), then total physical
    RAM, then nothing. Both rungs come from `fit`, so this adds no new
    measurement of its own — it only chooses between two numbers `fit` already
    computes, and returns None rather than a guess when neither is readable.
    """
    ram_gb = fit.machine_ram_gb()
    if ram_gb is None:
        return None
    wired = fit._wired_limit_bytes(ram_gb)
    return wired if wired is not None else ram_gb * fit.GB_BYTES


def reset() -> None:
    """Tests only: drop the table without touching real processes."""
    with _lock:
        _workers.clear()
        _downloads.clear()
        # A stray count would silently disable the next cancel — `_release_install`
        # would think somebody was still waiting on a key nobody holds.
        _install_waiters.clear()
        # A stray entry here would make the NEXT test's `unload_all` (or any
        # direct `_wait_for_draining` call) block for the full drain timeout
        # waiting on a `Worker` that no longer exists.
        _draining.clear()
