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
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

logger = logging.getLogger(__name__)

# `venv_key` returns sha256(...)[:16], so every real key is 16 lowercase hex
# characters. Matched with `fullmatch`, not `match` against `^...$`: `$` also
# matches just before a trailing newline, so "<key>\n" would validate and reach
# os.path.join as a different progress directory than "<key>". No traversal, but
# the anchoring this value's whole safety story rests on has to be real.
_KEY_RE = re.compile(r"[0-9a-f]{16}")

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

# How long a claim with no progress record yet is assumed to belong to a caller
# still inside `Popen` (see _claim_is_stale). Normally microseconds; this only has
# to exceed a slow spawn. Short, because the window it also covers — the server
# dying between claiming and writing — should self-heal rather than wedge the key.
_CLAIM_GRACE_S = 30


# Worker pids THIS process spawned and has not yet reaped — the only pids
# `_pid_alive` may `waitpid` on. See `_pid_alive` for why a pid out of
# `progress.json` is not enough: it can name a recycled pid now belonging to
# another of the server's children, and reaping that one is how a live child
# comes to report exit status 0.
#
# In-process and deliberately NOT persisted alongside the record. A persisted
# owner pid would still need a boot identity to survive server-pid reuse, and it
# would still be answering the wrong question: what makes a reap safe is that the
# pid is OUR child, and a pid stops being that the moment this process exits (the
# kernel reparents its orphans to init, which reaps them). So the in-memory set
# is not an approximation of provenance — it is exactly it.
_SPAWNED: set[int] = set()

# Backend attributes the loader reads to stay in step with it. Named here so
# `test_the_backend_attributes_this_module_reads_still_exist` can pin them.
BACKEND_ATTRS = ("_venvs_path", "_python_executable")


def _backend_attr(name: str):
    """Read `name` off the live backend, or fail saying what broke.

    Deliberately NOT `getattr(backend, name, <default>)`. A default here is the
    worst kind of fallback in this module: an upstream rename would silently
    yield `~/.openfused/venvs` / `None`, the loader would fill a directory no run
    ever reads, and the user would install the same packages forever — the exact
    "permanent double download with no error anywhere" this module's docstring
    warns about for the venv key. There is no safe guess, so there is no guess.
    """
    from fused_render.engine import get_backend

    backend = get_backend()
    try:
        return getattr(backend, name)
    except AttributeError:
        raise RuntimeError(
            f"this fused build's {type(backend).__name__} has no {name!r}, so the "
            "install loader cannot tell where its script venvs live or which "
            "interpreter they are keyed on. Guessing would build a venv no run "
            "ever reads. Pin a fused version that provides it."
        ) from None


def venvs_path() -> str:
    """Where the backend keeps its script venvs.

    Read off the live backend instance rather than restating its default, so a
    server constructed with a different `venvs_path` cannot drift from the
    loader. Monkeypatched by tests to a tmp dir.
    """
    return _backend_attr("_venvs_path")


def _python_executable() -> str | None:
    """The base interpreter the backend builds venvs from (None = ours).

    Folded into the venv key by `python_identity`, so the loader has to use the
    same value the backend will.
    """
    return _backend_attr("_python_executable")


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


def valid_key(key) -> bool:
    """Is `key` shaped like a venv key this module could have produced?

    Every real key is `venv_key`'s output: 16 lowercase hex characters, matched
    end to end (`fullmatch`, see `_KEY_RE`). Anything
    else is rejected before it can reach the filesystem, because `key` arrives
    straight off the wire (`/api/env/progress?key=`, `/api/env/cancel`) and
    `progress_dir` joins it onto a path.

    `_require_fused` is NOT a containment boundary here — its own comment says it
    "only blocks blind cross-origin POSTs", and every HTML page this app renders
    is same-origin while rendering arbitrary local HTML is the whole product. So
    `../../../..` in a key would otherwise read any `progress.json` on the disk,
    and — much worse — `/api/env/cancel` would take the `pid` out of that
    attacker-chosen file and hand it to `_kill`, which escalates to `os.killpg`
    for a group leader. Validated here rather than at each endpoint so a future
    caller cannot skip it.
    """
    return isinstance(key, str) and _KEY_RE.fullmatch(key) is not None


def progress_dir(key: str) -> str:
    """Where a given install's `progress.json` and worker log live.

    Under the shell's home dir (so FUSED_RENDER_HOME redirects it for tests and
    per-branch state nests correctly), NOT inside the venv dir — a failed
    install deletes the venv dir, and the error is the one thing that must
    survive that.

    Raises ValueError for a key that is not `valid_key`: this is the function
    that turns a key into a path, so it is the right place to refuse.
    """
    if not valid_key(key):
        raise ValueError(
            f"not a valid install key: {key!r} (expected 16 lowercase hex characters)"
        )
    from fused_render.shell.storage import home_dir

    return os.path.join(home_dir(), "cache", "_env_install", key)


def _progress_path(key: str) -> str:
    return os.path.join(progress_dir(key), "progress.json")


def _claim_path(key: str) -> str:
    return os.path.join(progress_dir(key), "claim")


def _claim_within_grace(claim: str) -> bool:
    """Is `claim` young enough to still count as a live install?

    The ONE definition of "a claim that still counts", read by both `progress()`
    (which reports such a claim as in flight) and `_claim_is_stale` (which refuses
    to take it over). Two independent age rules for one fact would drift, and the
    drift would be invisible: they would disagree only inside a window measured in
    milliseconds.

    A claim that cannot be stat'd (already taken over, or gone) is not within
    grace — there is nothing to be behind it.
    """
    try:
        age = time.time() - os.path.getmtime(claim)
    except OSError:
        return False
    return age <= _CLAIM_GRACE_S


def _write(key: str, record: dict) -> None:
    """Atomically replace `progress.json` — a poll must never read a half-write.

    The temp name carries pid+thread id. A single shared `progress.json.tmp` is
    not merely untidy: two concurrent writers race, the first `os.replace`
    consumes the tmp file the second had just created, and the second dies with
    `FileNotFoundError` — a 500 out of /api/env/install. That is reachable, since
    the endpoints run in FastAPI's threadpool and the worker writes to the same
    file from another process. Unique temp + `os.replace` keeps the swap atomic
    without the shared name.
    """
    os.makedirs(progress_dir(key), exist_ok=True)
    path = _progress_path(key)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f)
    os.replace(tmp, path)


def _write_if_absent(key: str, record: dict) -> dict | None:
    """Create `progress.json` with `record`, or None if one already exists.

    The parent's `spawn` record exists ONLY to fill the gap before the worker's
    first write lands, so it must never be able to displace a record the worker
    already wrote — and `_spawn` returns the moment `Popen` does, with the worker
    already running. A resolver that fails on its first import genuinely can write
    its `done` record first; replacing that with `done: False` plus an
    already-exited pid makes `_recorded_progress` synthesise "the installer exited
    unexpectedly" for an install that had already reported its real outcome, and
    runtime.js turns that into a hard failure. Asserting the parent wins the race
    is what produced D180, so the ordering is guaranteed here instead.

    `O_CREAT|O_EXCL` is the guarantee: the OS makes the create-or-fail atomic, so
    the worker (which writes through `_write`'s replace, and therefore always
    overwrites) is the unconditional winner and the parent only ever fills an
    actual absence. That is also why the record must be written straight into
    `progress.json` rather than temp-then-replace — `os.replace` cannot refuse.
    The payload is one small `os.write`, and a reader that catches a torn read
    degrades to "no record yet", which for a claimed key is already reported as
    in flight (`progress()`).

    Whoever wins the claim clears any previous attempt's record first (see
    `start`), so an absence here really does mean "this install has said nothing
    yet" and not "the last attempt's failure is still lying around".
    """
    os.makedirs(progress_dir(key), exist_ok=True)
    try:
        fd = os.open(_progress_path(key), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    try:
        os.write(fd, json.dumps(record).encode())
    finally:
        os.close(fd)
    return record


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
        )
        return str(pid) in (out.stdout or "")
    # Reap it first if WE spawned it. `start_new_session=True` does not reparent
    # the worker — it stays our child until someone waits on it — and a ZOMBIE
    # answers `os.kill(pid, 0)` successfully. So a worker that died before writing
    # `done` (a bad import, a kill) would read as "still running" forever, and
    # `progress()` would never reap it into an error: the page polls a corpse and
    # any bounded waiter waits out its entire timeout. Nothing else waits on a
    # worker pid — `_spawn` discards the Popen — so reaping ours is safe, and it
    # is what makes "the installer exited unexpectedly" detectable at all.
    #
    # Gated on `_SPAWNED` rather than on `ChildProcessError`, which only tells us
    # the pid is not a child of ours AFTER the reap has already happened. `pid`
    # comes out of `progress.json`, a not-`done` record survives a server crash
    # mid-install, and that pid can since have been recycled onto a child of the
    # CURRENT server — an rclone rcd, a template tile daemon, a pyramid build
    # worker. Reaping one of those makes its owner's `poll()`/`wait()` fail with
    # ECHILD, which subprocess reports as **exit status 0**: a crashed or
    # still-needed child read as "finished successfully", and every one of those
    # owners branches on that status.
    if pid in _SPAWNED:
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                _SPAWNED.discard(pid)  # reaped: the pid is now free to be reused
                return False
        except ChildProcessError:
            _SPAWNED.discard(pid)  # already reaped elsewhere; never reap it again
        except OSError:
            pass
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


def _read_record(key: str) -> dict | None:
    """The install's `progress.json` as written, or None if unreadable.

    No liveness interpretation — that is `progress()`'s job, and separating the
    two is what lets it re-read after reaping without recursing.
    """
    try:
        with open(_progress_path(key), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _recorded_progress(key: str) -> dict | None:
    """The install's state **as far as `progress.json` knows**, or None.

    Everything `progress()` used to be. Split out because `progress()` now also
    reports a claim with no record as in flight, and `_claim_is_stale` must NOT see
    that: it decides whether to take a claim over, so a claim-derived "in flight"
    would make the claim its own alibi and nothing would ever be stealable — one
    crash would wedge the key forever, the exact failure `_claim_is_stale` exists
    to prevent. This is the record-only view, so the two cannot become circular.

    A record that is not `done` but whose pid is gone is a crash, and is
    reported as finished-with-an-error — the same liveness check
    `templates/docs/docs.py` does, and for the same reason: otherwise the page
    polls a dead installer forever.

    An invalid key reads as "never started" rather than raising: no such install
    can exist, and the endpoint rejects the shape separately.
    """
    if not valid_key(key):
        return None
    data = _read_record(key)
    if data is None:
        return None
    if not data.get("done") and not _pid_alive(data.get("pid", -1)):
        # Re-read before calling it a crash. A finished worker writes its final
        # record and THEN exits, so "the record I read says not-done" and "the
        # pid is gone" is also what SUCCESS looks like through a stale read —
        # and the read above is stale by construction, since `_pid_alive` waits
        # on the pid and so returns only after the worker is already gone. The
        # window is small but the consequence is not: runtime.js turns an error
        # record into a hard install failure, so a spurious one aborts an
        # install whose venv is sitting there complete.
        fresh = _read_record(key)
        if fresh is not None and fresh.get("done"):
            return fresh
        data["done"] = True
        data["error"] = data.get("error") or (
            "the installer exited unexpectedly — see worker.log in "
            + progress_dir(key)
        )
    return data


def progress(key: str) -> dict | None:
    """The install's current state, or None when it was never started.

    A claim and a progress record are two facts about ONE state, and this function
    owns the question. The claim is created before `_spawn`, and the parent's first
    `_write` lands only after `Popen` returns — a fork/exec of a Python
    interpreter. For that whole window "claim present, no record" is what a
    perfectly healthy install looks like, and answering None for it means "never
    started": runtime.js treats a null record as a hard failure ("the installer
    left no progress record"), so the first open of a PEP 723 template could fail
    while the install it was waiting on ran to completion.

    Fixed here rather than in `start()`'s join branch because the poll is a
    SEPARATE request: a synthetic record in one response body does nothing for the
    GET /api/env/progress that follows it, nor for a reloaded page that polls
    without ever having POSTed.

    Past `_CLAIM_GRACE_S` a claim with no record is not "starting" any more — the
    server died between claiming and writing — and saying "starting" forever would
    wedge the key with a poll that never ends. So it resolves as
    done-with-an-error: the installer never got off the ground, which the caller
    can show and retry (and the retry's `_claim` takes the stale claim over). The
    grace window is `_claim_within_grace`'s, shared with `_claim_is_stale` so the
    two agree by construction.
    """
    prog = _recorded_progress(key)
    if prog is not None:
        return prog
    if not valid_key(key):
        return None
    claim = _claim_path(key)
    if not os.path.exists(claim):
        return None
    if _claim_within_grace(claim):
        return {"stage": "spawn", "pct": STAGE_PCT["spawn"],
                "detail": "an installer for these packages is starting",
                "done": False, "error": None, "pid": None, "ts": time.time()}
    return {"stage": "done", "pct": 100, "detail": "the installer never started",
            "done": True, "pid": None, "ts": time.time(),
            "error": "the installer never started — see worker.log in "
                     + progress_dir(key)}


def _in_flight(key: str) -> bool:
    prog = progress(key)
    return bool(prog) and not prog.get("done")


def _claim(key: str) -> bool:
    """Win the exclusive right to spawn the installer for `key`.

    `progress()` then `_spawn()` is a check-then-act: two callers can both see
    "not running" and both spawn. That is not theoretical here — the endpoints
    are sync `def`, so FastAPI runs them in a threadpool, genuinely
    concurrently — and two workers building one venv directory is exactly the
    race `fused`'s in-process lock cannot cover: the loser dies on a half-built
    `<venv>/bin/python`.

    So the claim is an `O_CREAT|O_EXCL` create, which the OS makes atomic (the
    same primitive `warm_fused_backend_venv` uses in the test suite). A claim
    left behind by a finished or dead installer is taken over (`_claim_is_stale`),
    and if someone else wins that takeover we join them rather than spawn a
    second worker.
    """
    os.makedirs(progress_dir(key), exist_ok=True)
    claim = _claim_path(key)
    for attempt in (1, 2):
        try:
            fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if attempt == 2:
                return False  # another caller took it over first — join them
            if not _claim_is_stale(key, claim):
                return False
            try:
                os.unlink(claim)
            except OSError:
                return False
            continue
        except OSError:
            return False
        try:
            os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
        finally:
            os.close(fd)
        return True
    return False


def _claim_is_stale(key: str, claim: str) -> bool:
    """May we take over an existing claim?

    Deliberately NOT just `not _in_flight(key)`. The claim is created *before*
    the installer's first progress record exists, so "claim present, no record"
    is the normal state for the microseconds a competing caller spends inside
    `Popen` — reading that as stale is what let sixteen concurrent callers spawn
    six workers while the O_EXCL create was working perfectly. (Measured; that is
    how this function came to exist.)

    So: with a record, the RECORD decides — `_recorded_progress` has already reaped
    a dead worker into `done`. Without one, only age can distinguish "mid-spawn"
    from "the server died between claiming and writing", and the grace window is
    short enough that a genuine crash self-heals rather than wedging the key.

    Deliberately `_recorded_progress`, not `progress()`: `progress()` reports a
    fresh claim as in flight, so consulting it here would let a claim vouch for
    itself and no claim could ever be taken over. Same grace window either way —
    `_claim_within_grace` is the single definition — but the evidence for "someone
    is behind this claim" must come from something other than the claim's own
    existence.
    """
    prog = _recorded_progress(key)
    if prog is not None:
        return bool(prog.get("done"))
    return not _claim_within_grace(claim)


def uv_bin() -> str | None:
    """Path to the uv binary the venv builder should use, or None.

    Same resolution order as `shell.mounts.rclone_bin`, and for the same reason —
    a packaged build must not depend on the user's PATH:

      1. FUSED_RENDER_UV_BIN, if it points at a real file (the Linux/Windows
         supervisors already set an equivalent for rclone);
      2. the interpreter's OWN directory — where the Linux AppImage
         (`usr/python/bin/uv`, build_linux_appimage.sh:88) and the Windows
         installer (`<PythonRoot>/uv.exe`, .ps1:185) put it;
      3. `Contents/Resources/bin/uv`, the macOS bundle's separate `bin` dir
         (build_dmg.sh), which is not beside the interpreter;
      4. whatever is on PATH (dev checkout).

    Steps 2 and 3 are both needed because the three packaged builds disagree on
    the layout. Probing only the macOS one meant the uv that Linux and Windows
    already ship went unused unless its directory happened to be on PATH — not a
    crash there (those builds carry a real CPython with `venv` and `pip`, so the
    fallback works) but a silently-unused bundled tool.

    This matters more than a convenience wrapper: `fused`'s venv builder calls
    `shutil.which("uv")` and falls back to `<python> -m venv`, and the macOS
    bundle contains **no `venv`, `ensurepip` or `pip` module at all** — measured
    on an installed DMG, the fallback fails with "No module named venv". Without
    uv on the worker's PATH the install loader cannot build anything on macOS.

    Step 2 is a plain path probe and deliberately does NOT gate on
    `sys.frozen == "macosx_app"` the way `shell.mounts.rclone_bin` does. py2app's
    boot script is what sets `sys.frozen`, so anything that reaches this code
    without going through the app launcher — a subprocess, a smoke test, a future
    entry point — would silently miss the bundled uv and fall back to a `venv`
    module the bundle does not contain. A stat costs nothing and cannot be wrong
    about whether the file is there; this exact failure cost a debugging cycle.
    """
    override = os.environ.get("FUSED_RENDER_UV_BIN")
    if override and os.path.isfile(override):
        return override
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    name = "uv.exe" if os.name == "nt" else "uv"
    candidates = (
        os.path.join(exe_dir, name),                                    # Linux, Windows
        os.path.join(os.path.dirname(exe_dir), "Resources", "bin", name),  # macOS .app
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("uv")


def _worker_env() -> dict:
    """Environment for the installer worker, with the bundled uv reachable.

    `fused` finds uv via `shutil.which`, i.e. PATH — so a uv that ships inside
    the .app has to be put ON the PATH rather than merely located. Prepended, so
    the bundled one wins over an older system uv.
    """
    env = dict(os.environ)
    uv = uv_bin()
    if uv:
        env["PATH"] = os.path.dirname(os.path.abspath(uv)) + os.pathsep + env.get("PATH", "")
    return env


def _spawn(key: str, requirements: list[str]) -> int:
    """Launch the detached worker; returns its pid.

    Detached (`start_new_session` / DETACHED_PROCESS) so the build outlives the
    request that started it and any page reload — exactly docs.py's spawn.
    `sys.executable`, deliberately: `python_identity` keys the venv on the
    interpreter, so the worker must build from the same one the server would.

    That is also why the backend's `_python_executable()` travels in argv (slot 4,
    before the requirements) instead of the worker deciding for itself: the venv
    key folds that value in, so a worker that assumed None while the backend had
    one set would build a venv `is_installed()` never finds — the page would
    install, retry, be told to install again, forever. argv cannot carry None, so
    the empty string stands for it; `_env_install_worker.main` maps it back.
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
            [sys.executable, worker, key, d, venvs_path(),
             _python_executable() or "", *requirements],
            stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
            env=_worker_env(), **detach,
        )
    # Recorded before the pid reaches anyone else: `_pid_alive` reaps only pids in
    # here, so a worker missing from the set would never be reaped and a dead one
    # would read as alive forever (the zombie trap `_pid_alive` describes).
    _SPAWNED.add(child.pid)
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
    if not _claim(key):
        # Someone else owns this install — join it, and report exactly what a poll
        # would see. No synthetic record here any more: `progress()` covers the
        # instant between their claim and their first write, and it is the function
        # the client's NEXT request calls, so a second record shape written only
        # into this response body could disagree with the poll that follows it —
        # which is how "the installer left no progress record" reached a user whose
        # install was running fine. Non-None by construction: every path where
        # `_claim` returns False leaves a claim in place, and `progress()` reports a
        # claim.
        joined = progress(key)
        if joined is None:
            # Neither claimed nor joinable: `_claim` only gets here when the
            # filesystem refused the create/unlink. Said out loud rather than
            # returned as None — the endpoint turns a RuntimeError into a message
            # the user can read, and None would reach runtime.js as the very
            # "installer left no progress record" this function stopped producing.
            raise RuntimeError(
                "could not start or join an installer for these packages: "
                f"{progress_dir(key)} is not writable"
            )
        return joined
    # This attempt owns the key now, so the PREVIOUS attempt's record must go.
    # `_write_if_absent` below deliberately cannot overwrite a record, so a failed
    # attempt's error left in place would become this attempt's answer: the loader
    # would show the old resolver failure the instant it opened while the new
    # worker downloaded fine behind it. Unlinked before `_spawn`, so the worker
    # cannot have written yet and nothing real is lost.
    try:
        os.unlink(_progress_path(key))
    except OSError:
        pass
    pid = _spawn(key, list(requirements))
    # Written by the PARENT, before the worker's first write lands, so the very
    # first poll after the click shows "starting" instead of "never started" —
    # and so `_in_flight` is true immediately, closing the double-click window.
    # It also carries the pid, which is what makes a worker that died before
    # writing anything (a failed exec) detectable at once rather than after the
    # claim's grace window.
    #
    # Create-if-absent, NOT a write: a fast worker's record is strictly better
    # than this one (it is the install's real state, and its pid is its own), so
    # it wins by construction rather than by hoping the parent got there first.
    # See `_write_if_absent`.
    record = {"stage": "spawn", "pct": STAGE_PCT["spawn"],
              "detail": f"starting installer for {len(requirements)} package(s)",
              "done": False, "error": None, "pid": pid, "ts": time.time()}
    if _write_if_absent(key, record) is None:
        # The worker beat us to it. Report what a poll would report — the same
        # rule as the join branch above, and for the same reason: a record shape
        # written only into this response body could disagree with the GET that
        # follows it.
        return progress(key) or record
    return record


def cancel(key: str) -> bool:
    """Kill the recorded installer; True if there was a live one to kill.

    The half-built venv dir is left as-is on purpose: it has no ready marker, so
    `ensure_requirements_venv` removes and rebuilds it on the next attempt. The
    record is marked done-with-an-error so the poller stops and the page can say
    what happened rather than falling silent.

    An invalid key kills nothing. This is the endpoint that would otherwise read
    a `pid` out of an attacker-chosen file and signal it (see `valid_key`).

    Deliberately `_recorded_progress`, not `progress()`: a claim with no record yet
    has no pid to signal, and a "cancelled" record written into that window would
    be overwritten moments later by the spawner's own `_write` — the page would
    show cancelled and then watch the install continue. So cancelling inside the
    spawn window is a no-op that reports False, and the poll keeps showing
    "starting"; the user can cancel again once there is an installer to kill.
    """
    if not valid_key(key):
        return False
    prog = _recorded_progress(key)
    if not prog or prog.get("done"):
        return False
    pid = prog.get("pid", -1)
    killed = _kill(pid) if _pid_alive(pid) else False
    prog.update(done=True, error="the install was cancelled", stage="done",
                pct=100, ts=time.time())
    _write(key, prog)
    return killed
