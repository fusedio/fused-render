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

# venv directory -> "does its own python actually run" (D206). Populated by
# `_venv_is_usable`, which is the ONLY reader/writer, and dropped by
# `is_installed` when the marker goes away. In-process and never persisted: the
# thing it remembers is a property of THIS machine's filesystem as it is right
# now, and a persisted verdict would need its own invalidation story to avoid
# becoming the very stale-forever fact the marker already was.
_VALIDATED: dict[str, bool] = {}

# venv directory -> how many times its verdict has been discarded, i.e. which
# GENERATION of that directory the cache is allowed to describe. Bumped by
# `_discard_verdict`, the only way an entry leaves `_VALIDATED`.
#
# This exists because the probe runs outside the lock (see `_venv_is_usable`), so a
# verdict can be measured against a venv that is destroyed before it is stored. The
# invariant it buys is small and absolute: **a verdict is only ever cached against
# the generation it judged.** Without it the cache could hold an answer about a
# directory that no longer exists, attached to whatever replaced it under the same
# key — the same defect class as caching an inconclusive probe, where the cache ends
# up answering a question nobody asked it and nothing ever re-asks.
#
# Deliberately not leaning on `_REBUILD_ATTEMPTED` to absorb that instead: the
# rebuild budget is a POLICY (D206 records it as one, and policies get tuned), and
# quietly making it double as the staleness backstop means a later change to the
# budget resurfaces staleness somewhere unrelated to the change that caused it.
_GENERATION: dict[str, int] = {}

# Bumped by `reset_venv_validation_cache`, and folded into every generation read
# (`_generation_of`). A per-directory counter alone cannot express "invalidate
# everything", because a probe on its first look at a venv has no entry to bump —
# and that is the majority of in-flight probes. One epoch covers them all.
_EPOCH = 0

# venv directories whose ready marker this process has already discarded once
# (D206). The bound on the repair: one rebuild per venv per process, because for
# the cohort this whole mechanism exists for the rebuild is guaranteed to reproduce
# the same breakage, and re-downloading hundreds of MB on every page reload is
# worse than the error it is trying to avoid. See `is_installed`.
#
# Deliberately NOT cleared when the marker goes absent — the marker's absence is
# the *expected* state immediately after we unlink it, so clearing the bound there
# would mean the bound could never engage at all.
_REBUILD_ATTEMPTED: set[str] = set()

# venv directories whose bound has already been announced, so the warning is
# emitted once per venv per process instead of once per request. Kept separate from
# `_REBUILD_ATTEMPTED` because they answer different questions — "may I still
# repair this" vs "have I already said this out loud" — and folding the second into
# the first would make the log line depend on the repair policy.
_BOUND_LOGGED: set[str] = set()

# Guards all three of the above together: they are read and written as one
# decision, and (since the fix for the unmark race) so is the marker's existence.
_validated_lock = threading.Lock()

# The probe is `<venv>/bin/python -c ""` on the local filesystem: a working
# interpreter answers in well under a second, and a broken one fails immediately.
# Small on purpose — it is paid on the request path of the first PEP 723 run in a
# process, so a generous budget would turn a pathological interpreter (one that
# hangs rather than exits) into a page that looks hung. Same 5s, and the same
# reasoning, as `engine._PROBE_TIMEOUT_S`: cold start on a slow volume is why it
# is not 1s, not why it would be 60.
_VENV_PROBE_TIMEOUT_S = 5


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


def _venv_python(venv_dir: str) -> str:
    """Where a venv keeps its own interpreter, on this OS."""
    if os.name == "nt":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def _venv_runs(venv_dir: str) -> bool | None:
    """Can this venv's own interpreter start at all? One subprocess, no imports.

    THREE answers, not two, because the caller destroys state on a False:

      True  — the probe completed and the interpreter exited 0.
      False — DEFINITE evidence there is no usable interpreter here: the probe
              completed with a non-zero exit, or the spawn failed on the exe
              itself (`FileNotFoundError`/`PermissionError`/`NotADirectoryError`).
      None  — INCONCLUSIVE: we could not complete the probe. A timeout, or any
              other `OSError` — `EAGAIN` (cannot fork under load), `EMFILE` (the
              server is out of descriptors), `EINTR`. None of those are facts
              about the venv, and the caller must not act on them.

    The two-valued version of this was a real hazard, not a hypothetical:
    `subprocess.TimeoutExpired` IS a `SubprocessError` and the budget is 5s, so a
    handful of concurrent `/api/run` calls on a loaded machine was enough to
    unlink the ready marker of a perfectly healthy venv and charge the user a full
    uv re-download for it. `engine._probe` is the precedent — it reports a failed
    probe and destroys nothing.

    `-c ""` deliberately: the question is not "are the requirements importable"
    (that is upstream's job and would cost a real import) but the far more basic
    "does this interpreter reach the point of executing a program". A venv whose
    `home`/base prefix no longer exists dies before that — in the DMG case with
    `ModuleNotFoundError: No module named 'encodings'` or a bare fatal error — so
    the emptiest possible program is a complete test of the property that broke.

    Run with `PYTHONHOME`/`PYTHONPATH`/`VIRTUAL_ENV`/`PYTHONSTARTUP` scrubbed,
    borrowed from `engine._child_env()` rather than restated here: that is derived
    from `python_compute._STRIPPED_ENV_VARS` when fused is importable, so the probe
    cannot drift from the environment the child actually gets. Running it with OUR
    env is the whole reason the bug was invisible — inside the .app, PYTHONHOME is
    set and makes a broken venv look fine (build_dmg.sh's smokes made the same
    mistake, which is why one of them now strips the env too).
    """
    exe = _venv_python(venv_dir)
    try:
        from fused_render.engine import _child_env

        proc = subprocess.run(
            [exe, "-c", ""],
            capture_output=True, text=True,
            timeout=_VENV_PROBE_TIMEOUT_S, env=_child_env(),
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError) as e:
        # About the PATH we asked to execute, so evidence about the venv. Listed
        # before the generic OSError below, which they are all subclasses of.
        logger.warning("script venv %s cannot run its own python: %s", venv_dir, e)
        return False
    except (OSError, subprocess.SubprocessError) as e:
        # Deliberately distinct wording from every "cannot run its own python"
        # message above: an operator reading the log has to be able to tell "the
        # venv is broken" (state was destroyed, a rebuild is coming) apart from
        # "we never got an answer" (nothing was touched).
        logger.warning(
            "could not complete the readiness probe of script venv %s (%s: %s) — "
            "leaving it alone; the run will surface any real failure itself",
            venv_dir, type(e).__name__, e,
        )
        return None
    if proc.returncode != 0:
        logger.warning(
            "script venv %s cannot run its own python (exit %s): %s",
            venv_dir, proc.returncode, (proc.stderr or proc.stdout or "").strip(),
        )
        return False
    return True


def _marker_stamp(marker: str) -> tuple[int, int] | None:
    """Which ready marker this is — `(st_ino, st_mtime_ns)` — or None if absent.

    The marker's IDENTITY, because its existence is not enough: the install worker is
    a different PROCESS, and inside one probe window it can un-mark a venv, rebuild
    it and write a fresh marker. `os.path.exists` reads True before and after that
    and so cannot see it, while inode + nanosecond mtime changes on any rewrite,
    replace or recreate. Named to match openfused's `_marker_stamp`, which closes the
    same hole on its side, so the two read alike.

    None (absent) is a value like any other here: comparing a captured None against a
    later stamp correctly reports "a marker appeared", and vice versa.
    """
    try:
        st = os.stat(marker)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns)


def _generation_of(venv_dir: str) -> tuple[int, int]:
    """Which generation of `venv_dir` the cache may currently describe.

    The pair, never either half alone: `_GENERATION` ends one directory's generation
    (a discard), `_EPOCH` ends all of them at once (a reset). Caller holds
    `_validated_lock`.
    """
    return (_EPOCH, _GENERATION.get(venv_dir, 0))


def _discard_verdict(venv_dir: str) -> None:
    """Forget `venv_dir`'s cached verdict and END the generation it described.

    The ONLY way an entry may leave `_VALIDATED`, so that "the verdict is gone" and
    "the generation moved" can never disagree — a bare `pop` elsewhere would silently
    re-open the window `_GENERATION` exists to close. The caller must already hold
    `_validated_lock`: both dicts are one piece of state.
    """
    _VALIDATED.pop(venv_dir, None)
    _GENERATION[venv_dir] = _GENERATION.get(venv_dir, 0) + 1


def _venv_is_usable(venv_dir: str) -> bool | None:
    """`_venv_runs`, memoized per venv directory **per process**.

    The ceiling this guarantees: at most ONE probe per venv per server process,
    and NEVER one per request. `/api/run`'s pre-flight calls `is_installed` on
    every run of every PEP 723 script, so an unmemoized probe would put a
    subprocess spawn on the request path of the app's hottest execution path —
    which is precisely the cost the install loader exists to remove.

    Double-checked locking: read the cache under the lock, probe OUTSIDE it, then
    re-acquire to store. The lock is module-global, so holding it across the probe
    (as this first did, copying `engine._app_interpreter_lock`'s shape) serialized
    the first probes of completely UNRELATED venvs — and with a 5s budget, one venv
    whose directory sits on a wedged network mount then blocked every
    `is_installed` in the process for up to five seconds, cached hits for healthy
    venvs included. Clicking through eight PEP 723 pages is eight first-probes, and
    the tail is what hurts, not the ~15-30ms each.

    The cost of the looser lock is that two threads racing on the SAME venv can
    both probe. That is accepted deliberately: the probe is a read-only `-c ""`
    with no side effects, so a duplicate costs one extra spawn in a rare race —
    a much better trade than a global stall. If the other thread stored a verdict
    while we were probing, theirs wins and ours is discarded, so every caller in
    the race still sees ONE answer.

    Probing outside the lock also means the venv can be DESTROYED mid-probe, so the
    generation is read before the probe and re-checked after (`_generation_of`), and
    the invariant is absolute: **a verdict is only ever cached against the generation
    it judged.** If it moved, this returns its own measurement to its own caller and
    stores nothing — no window in which the cache describes a venv that is gone, and
    nothing for `_REBUILD_ATTEMPTED` to have to absorb.

    Negative verdicts are cached too — and `is_installed` drops the entry whenever
    the marker is absent, which is what stops a cached "no" from outliving the
    venv it judged. See the comment there.

    An INCONCLUSIVE probe (`None`) is deliberately not cached: it says nothing
    about the venv, so there is nothing to remember, and caching it would turn one
    unlucky fork under load into a permanently unvalidated venv. `_VALIDATED` maps
    to real booleans only, which is also why `.get()` returning None can safely
    mean "not probed yet".
    """
    with _validated_lock:
        cached = _VALIDATED.get(venv_dir)
        generation = _generation_of(venv_dir)
    if cached is not None:
        return cached
    verdict = _venv_runs(venv_dir)
    if verdict is None:
        return None  # inconclusive: nothing to store, nothing to agree about
    with _validated_lock:
        if _generation_of(venv_dir) != generation:
            # The venv we judged was discarded while we were judging it, so this
            # verdict describes a directory that is gone. Return it to OUR caller —
            # it is what we genuinely measured, and answering it is strictly better
            # than a second probe on the request path — but store nothing, so the
            # next call re-probes the venv that now stands here. No attempt to
            # decide which verdict is "fresher": there is no evidence either way,
            # and the cheap, always-correct move is to cache neither.
            return verdict
        # Same generation, so whatever landed while we probed describes the same
        # venv and either answer is right. `setdefault` makes "store only if still
        # absent, and tell me which won" one atomic step, so every caller in the
        # race returns the SAME verdict rather than each its own.
        return _VALIDATED.setdefault(venv_dir, verdict)


def reset_venv_validation_cache() -> None:
    """Forget every venv verdict AND every rebuild attempt, so the next
    `is_installed` re-probes and is willing to repair again.

    A test seam (mirroring `engine.reset_app_interpreter_cache`). Nothing in the
    server needs it: the verdict cache self-invalidates through the marker, and the
    rebuild bound is meant to last exactly as long as the process. All three sets
    are cleared together on purpose — resetting only the verdicts would leave a
    bound that silently suppresses the repair the re-probe just asked for, and
    leaving `_BOUND_LOGGED` behind would silence the announcement of a bound that
    can now engage again.

    Bumps `_EPOCH` rather than only emptying the dicts, so every generation actually
    ENDS — including those of venvs that have no `_VALIDATED` entry yet, which is
    every probe currently in flight on its FIRST look at a venv. A bare clear would
    be undone by exactly the calls it means to invalidate: such a probe measured the
    pre-reset world, would find its key absent afterwards, and would insert that
    pre-reset verdict. With the epoch moved, `_GENERATION` can be emptied too — the
    epoch already invalidates everything it held.
    """
    global _EPOCH

    with _validated_lock:
        _EPOCH += 1
        _VALIDATED.clear()
        _GENERATION.clear()
        _REBUILD_ATTEMPTED.clear()
        _BOUND_LOGGED.clear()


def is_installed(requirements: list[str]) -> bool:
    """True iff the venv for `requirements` exists, is complete AND can run.

    The ready marker is the INDEX of installed venvs — the only thing consulted to
    find one, and its absence is final — but since D206 it is treated as a *claim*
    that gets verified once per process rather than as proof. The macOS DMG is why:
    its bundled interpreter could not self-locate without PYTHONHOME, which
    `python_compute` strips from every child, so a venv built from it recorded a
    base prefix that does not exist on the user's machine and every child of that
    venv died. And the venv cache key folds in only the interpreter's path and
    version — both constants inside the .app — so upgrading the app did not change
    the key, nothing ever revalidated, and the breakage was permanent with no
    repair action anywhere in the UI. `Contents/lib -> Resources/lib`
    (`scripts/build_dmg.sh`) stops NEW venvs from being built that way; this stops
    an EXISTING bad one from being trusted forever.

    Two guarantees bound what that costs when it is wrong, both pinned by tests:
    the probe is three-valued, so an INCONCLUSIVE result (`_venv_runs` -> None:
    a timeout, a fork that failed under load) answers True and touches nothing;
    and the repair is attempted at most ONCE per venv per process, so a venv that
    cannot be fixed by rebuilding stops asking to be rebuilt. Both are contracts,
    not defensive padding — see the comments at each branch below.

    And ONE rule covers every branch that acts on a verdict: **a verdict may only be
    acted on while the generation it judged is still current, and a "ready" answer
    requires the marker to still be there at the moment we answer.** The probe is a
    subprocess, so between stamping the marker and answering, the install worker (a
    different PROCESS) can un-mark this venv, rebuild it, and re-mark it — none of
    which touches `_GENERATION`/`_EPOCH`, because those only know what THIS process
    did. That asymmetry is why both mechanisms exist: the counters keep the cache
    honest about our own discards, and the marker stamp keeps us honest about
    everyone else's. So the stamp is captured before the probe and re-checked, by
    IDENTITY not existence, before either answer that depends on it.
    """
    if not requirements:
        return True  # nothing to install; the bare/interpreter paths handle it
    venv_dir = venv_dir_for(requirements)
    marker = os.path.join(venv_dir, READY_MARKER)
    stamp = _marker_stamp(marker)
    if stamp is None:
        # No marker at all, so there is no claim to validate — and this is the common
        # case by count (every first open of a PEP 723 script), which stays a single
        # stat rather than a spawn. Also a place a cached verdict is dropped: a
        # marker that is absent now means the directory is being (or is about to be)
        # rebuilt, and the rebuild must not inherit the failed verdict of what stood
        # there before. Without this, deleting the marker below would trade a permanently
        # bad venv for a permanently negative answer — the loader would install
        # successfully and `is_installed` would still say no, forever.
        with _validated_lock:
            _discard_verdict(venv_dir)
        return False
    verdict = _venv_is_usable(venv_dir)
    if verdict is None:
        # The probe could not be completed (a timeout, a fork that failed under
        # load). That is a fact about this server at this instant, not about the
        # venv, so the marker's claim stands unexamined and the answer is yes: the
        # run proceeds and surfaces whatever really happens, exactly as it did
        # before any of this validation existed. Destroying a multi-hundred-MB venv
        # requires evidence, and "I could not look" is not evidence.
        #
        # No stamp re-check on this path: it neither answers FROM a verdict nor acts
        # on one. It says "I learned nothing", and that is true whatever the marker
        # has done in the meantime.
        return True
    # Both remaining answers act on the verdict — one says "ready", the other spends
    # the repair budget and may unlink — so both are gated on the marker still being
    # the SAME one that was judged. Shared, not duplicated per branch: the rule is
    # one rule (see this function's docstring), and two copies of it would be two
    # places to get the identity comparison subtly different.
    #
    # A definite failure means the repair is allowed exactly ONCE per venv dir per
    # process. Beyond that, the download is known-futile: for the cohort D206 exists
    # for (a pre-symlink `.app`) the rebuild reproduces the identical breakage,
    # because the property that failed is the bundled interpreter's own base prefix
    # and the venv key folds in only that interpreter's path and version — both
    # constants inside the bundle. `runtime.js` bounds the retry WITHIN one
    # `runPython` call, but nothing bounded it across calls, so every page reload,
    # every `watchPath` auto-reload and every param change would pay another full
    # download. Before this validation existed that cohort got one instant permanent
    # error; an unbounded rebuild would be strictly worse than the bug.
    #
    # So the second and later failures leave the marker in place and report
    # installed: the script runs and the user sees the interpreter's own stderr,
    # which is the truthful outcome. Per PROCESS, deliberately — a user who installs
    # a fixed DMG gets a fresh server, hence an empty set, hence one rebuild that
    # actually works.
    with _validated_lock:
        # Re-stamp the marker HERE, inside the critical section, before anything is
        # answered or spent. "stamp the marker -> probe -> decide -> unlink" is not
        # atomic, in two independent ways:
        #
        #   * ANOTHER THREAD of this server. The endpoints are sync `def` running in
        #     FastAPI's threadpool, so two pre-flights for one script interleave: A
        #     stamps, probes, records the attempt and unlinks, while B — which
        #     stamped BEFORE A's unlink — arrives here AFTER A's add. Ungated, B
        #     reads `already_tried`, announces a rebuild that has not happened (A's
        #     has not even been requested yet) and returns True, running a venv known
        #     to be broken instead of joining the install A just asked for.
        #   * ANOTHER PROCESS. The install worker can un-mark this venv and start
        #     rebuilding (so a True verdict must NOT be answered — /api/run would
        #     execute against a directory being rebuilt underneath it), or finish a
        #     rebuild and write a FRESH marker (so a False verdict must NOT be acted
        #     on — the first-failure branch would unlink the marker of a just-rebuilt,
        #     possibly healthy venv and force another multi-hundred-MB download).
        #
        # Compared by IDENTITY, `(st_ino, st_mtime_ns)`, not by existence: the
        # un-mark-and-re-mark case leaves a marker present the whole time this side
        # can observe, so a boolean `exists()` sees nothing at all. This is what
        # `_marker_stamp` is for, and openfused's copy carries the same name so the
        # two read alike.
        #
        # Either way the conclusion is the same, which is why one branch serves both:
        # this verdict is about a venv that is no longer the one on disk, so drop it
        # and answer not-installed. The caller reports `needs_install` and joins the
        # install already in flight (`start()` joins rather than duplicating). The
        # bound question, likewise, only makes sense against the marker it was asked
        # about — "did the rebuild of THIS marked venv already fail".
        #
        # A stat under the lock is a deliberate, bounded cost: it is the same syscall
        # this function already makes unguarded on entry, so if that one returned, the
        # filesystem was answering microseconds ago. Not a cross-process lock, for the
        # reasons above — this module's only mutual exclusion is `threading.Lock`, and
        # a file lock would need a stale-lock policy of its own.
        if _marker_stamp(marker) != stamp:
            _discard_verdict(venv_dir)  # that generation is over
            return False
        if verdict is True:
            return True
        already_tried = venv_dir in _REBUILD_ATTEMPTED
        _REBUILD_ATTEMPTED.add(venv_dir)
        if already_tried:
            # Warn on the TRANSITION only. Past this point `is_installed` answers
            # from the cached verdict on every call — every page reload, every
            # `watchPath` auto-reload, every param change — so warning each time
            # would repeat this multi-line message forever and bury the one
            # occurrence that matters in the log of exactly the incident it exists
            # to diagnose. Demoted to debug afterwards rather than dropped: the
            # state is still abnormal, and a debug run should still show it.
            first_bound_hit = venv_dir not in _BOUND_LOGGED
            _BOUND_LOGGED.add(venv_dir)
        else:
            first_bound_hit = False
            # See the marker-absent branch above: the verdict must not outlive the
            # generation it judged. Kept on the already-tried path, so repeated
            # calls answer from the cache instead of spawning a probe apiece.
            _discard_verdict(venv_dir)
    if already_tried:
        (logger.warning if first_bound_hit else logger.debug)(
            "script venv %s still cannot run its own python after a rebuild, so "
            "the ready marker is being LEFT in place: rebuilding it again cannot "
            "help (the interpreter it was built from is the problem). The script "
            "will run and report the interpreter's own error.", venv_dir,
        )
        return True
    # The marker MUST go before we answer False, and this is not tidying up.
    # Upstream's `ensure_requirements_venv`/`ensure_bare_venv` short-circuit on
    # `if marker.exists(): return` — rebuilding only when it is ABSENT. So a
    # "not installed" answer with the marker left in place would make `/api/run`
    # reply `needs_install`, the page POST /api/env/install, the worker call
    # upstream, upstream see the marker and return without doing anything, and the
    # page be told to install again: a loop with no exit, against an unchanged
    # `fused`. Removing the marker is what makes upstream rmtree the directory and
    # build it again, which is the actual repair.
    #
    # (The cached verdict was dropped just above, for the same reason the
    # marker-absent branch drops it: deleting the marker ends this venv's
    # generation, and the directory that appears under the same key next is a
    # different venv that has to be judged on its own.)
    try:
        os.unlink(marker)
        logger.warning(
            "discarded the ready marker of script venv %s: its interpreter does "
            "not run, so the venv will be rebuilt on the next install", venv_dir,
        )
    except OSError as e:
        # Only worth saying out loud. A marker we cannot delete (read-only volume,
        # a race with the rebuild that already removed it) leaves the caller's
        # answer correct — this venv is not usable — and the next call re-reads
        # the filesystem, so nothing here is latched.
        logger.warning("could not remove the ready marker of %s: %s", venv_dir, e)
    return False


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
    spawn window is a no-op that reports False.

    That False is a real answer and the client MUST act on it: runtime.js's
    `onCancel` reads `cancelled === false`, says the installer could not be
    stopped, and leaves the button live so a second press reaches the pid once the
    record carries one — and its resolve path honours the user's intent even if
    the install finishes anyway, so a dropped cancel can never end in the script
    running. It used to be dropped silently: "cancelling…" was overwritten by the
    next poll's detail and the cancelled run executed.
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
