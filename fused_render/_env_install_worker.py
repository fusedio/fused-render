"""Detached worker that builds one project venv, spawned by envinstall.start().

Run as:  python _env_install_worker.py <key> <progress_dir> <project_dir>
                                      <venv_dir> <uv_cache_dir>
                                      <python_executable> <acquire_python>

Every path arrives in argv rather than being derived here, because this file
must stay free of any `fused_render` import (D152 — importing the package in a
detached child is a bootstrap that broke once already) and so cannot call
`projectenv`. Re-deriving the venv directory would also be a second derivation
of a cache key, which is how a loader ends up filling a directory no run reads.

`<python_executable>` is the base interpreter the environment is built on, and it
must be the value `envinstall._python_executable()` returned — the backend runs
the code, so its interpreter and the environment's have to be one choice. argv
cannot carry None, so the EMPTY STRING stands for it; `install` is the one place
that mapping happens, and it maps to this worker's OWN `sys.executable` (see
`_PINNED_PYTHON_VERSION` for why not a version string).

`<acquire_python>` (same empty-string idiom) switches this worker to its OTHER job:
DOWNLOAD that Python version, report it, and stop without building anything (D214).
The two cannot be one run — the interpreter is reported under
`envinstall.PYTHON_BOOTSTRAP_KEY` and the packages under the project's own key,
and one worker reports under one key.

Reports through `<progress_dir>/progress.json` — the same
`{stage, pct, detail, done, error, pid, ts}` record
`fused_render/templates/docs/install_worker.py` writes for the typst download,
so the page shell polls one shape.

Three deliberate choices:

**It builds with `uv sync`, in the project directory.** The declaration is the
folder's `pyproject.toml`; `uv sync` is the command that turns one into an
environment, resolves it, and writes the `uv.lock` the user commits. It is
pointed at a venv OUTSIDE the folder through `UV_PROJECT_ENVIRONMENT` (see
`projectenv` for why derived state never lands in the user's tree) and at a
cache on the same filesystem through `UV_CACHE_DIR`, which is what lets uv
hardlink wheels instead of silently copying them. `UV_LINK_MODE` is deliberately
left UNSET — uv's default already prefers hardlinks and falls back on its own.

**The ready marker and the source sidecar are written HERE, in that order.**
The sidecar records what the venv was built from and is what makes a later
declaration edit detectable; writing it after the marker would leave a window in
which the venv reads as installed but cannot say what it holds. An unmarked
directory is half-built and is removed before syncing, which is what makes
D212's repair a real replacement rather than a reconcile in place.

**Its error text is uv's, unedited.** uv's stderr goes into `progress.json`
verbatim, because a resolver failure ("no wheels with a matching platform tag
for imagecodecs") is the actual answer the user needs — the whole reason this
install is a visible flow instead of a 30-second timeout inside /api/run.

Stdlib only: no `fused_render` import, and (since the switch to `uv sync`) no
`fused` import either. It runs on whatever `sys.executable` the server used.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time

# Stages and their percentages, kept in step with fused_render/envinstall.py's
# STAGES/STAGE_PCT. Duplicated rather than imported because this file must stay
# importable-free of the package (it is spawned as a plain script, and
# `import fused_render` in a detached child is exactly the bootstrap that broke
# once already — see D152).
_PYTHON_PCT = 5
_CREATE_PCT = 10
_INSTALL_PCT = 25

# How often the install stage re-writes its record while uv runs (D213). The whole
# download happens inside ONE `ensure_requirements_venv` call behind
# `capture_output=True`, so nothing about its internals is observable from here;
# what this buys is proof of LIFE. The client polls every 500ms, so ~2s is well
# under the rate at which a repaint could look stale, and it is four orders of
# magnitude cheaper than the download it reports on.
_HEARTBEAT_S = 2

# How long the terminal write waits for the heartbeat to stop. Generous relative to
# the beat (which wakes immediately on the Event) because the only thing it protects
# against is a beat parked inside its own `_write`; the latch in `install` is what
# makes correctness independent of this number.
_HEARTBEAT_JOIN_S = 5

# Kept in step with fused_render/envinstall.READY_MARKER and
# projectenv.SIDECAR_NAME. Duplicated for the same reason the stage percentages
# are: this file must stay importable-free of the package (D152).
_READY_MARKER = ".openfused-ready"
_SIDECAR_NAME = ".fused-source.json"

# NOT a fallback interpreter — deliberately not used as one, and kept only to
# document why.
#
# An empty interpreter slot means the backend's `python_executable` was None, and
# None has always meant "the backend's own interpreter", never a version. It is
# also the COMMON case: `envinstall._resolve_script_python` answers `(None, True)`
# whenever the server is already on the pinned version, which is every packaged
# build (the DMG's `python@3.12`, the AppImage's and the Windows installer's
# `uv python install 3.12`) and every `scripts/dev.sh` checkout since D214.
#
# Translating that None into the literal "3.12" was a real bug: `uv sync --python
# 3.12` then resolves against PATH and uv's managed registry rather than the
# bundled app interpreter, and with uv's default download behaviour it fetches a
# managed CPython the app never uses as its base — so the venv is built on one
# interpreter and the code runs on another. `install` maps the empty slot to
# `sys.executable`, which IS the server's interpreter because `envinstall._spawn`
# launches this worker with it.
#
# The pin itself still exists and still matters; it lives at
# `envinstall.SCRIPT_PYTHON_VERSION` (D214), where it is what
# `_resolve_script_python` probes FOR and what the bootstrap round downloads.
_PINNED_PYTHON_VERSION = "3.12"

#: Stripped from every `uv` invocation below. uv is a native binary and does not
#: care, but the PYTHON PROCESSES IT STARTS do: a source-built dependency is
#: compiled by a build backend running in an interpreter uv creates, and that
#: interpreter inherits this process's environment.
#:
#: Inside the macOS .app, py2app's launcher exports `PYTHONHOME=<App>/Contents/
#: Resources`, so those build interpreters resolved their stdlib and site
#: out of the BUNDLE instead of out of the build environment. The bundle still
#: ships setuptools' `_distutils_hack` shim (py2app collects it; `build_dmg.sh`
#: prunes setuptools itself and used to leave the shim behind), so a fresh
#: setuptools' `import _distutils_hack.override` got the app's stale frozen copy,
#: which hijacked the distutils bootstrap and died with
#: `ModuleNotFoundError: No module named 'jaraco.text'`. Every source build in
#: the packaged app failed that way — reported to the user as a runner
#: environment that "did not build" (D266).
#:
#: The union of what the two child-environment scrubbers in the package already
#: strip — `engine._child_env` (`PYTHONPATH`, `PYTHONHOME`, `VIRTUAL_ENV`,
#: `PYTHONSTARTUP`, read off `fused`'s own `python_compute`) and
#: `supervisor._child_env` (those minus `VIRTUAL_ENV`, plus `PYTHONEXECUTABLE`,
#: which the macOS framework build sets). A union rather than a pick: each name
#: is on one of those lists because it redirects an interpreter somewhere it
#: should not go, and uv's children are interpreters.
#:
#: `VIRTUAL_ENV` was already being popped for `uv sync` on its own account — uv
#: warns about it and can target the server's own venv — which is now this one
#: line's job for every uv call rather than that one's.
#:
#: RESTATED rather than imported because this worker must not import
#: `fused_render` at all (D152 — a detached child that bootstraps the package is
#: a failure mode that already shipped once). A test holds the two in step.
_STRIPPED_ENV_VARS = ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE",
                      "PYTHONSTARTUP", "VIRTUAL_ENV")


def _uv_env(**overrides):
    """This process's environment, minus what would poison uv's child pythons."""
    env = dict(os.environ)
    for name in _STRIPPED_ENV_VARS:
        env.pop(name, None)
    env.update(overrides)
    return env


def _write(progress_dir, stage, pct, detail="", done=False, error=None):
    # Unique temp name, not a shared `progress.json.tmp`: the server writes this
    # same file (envinstall._write) and two writers racing on one temp means the
    # first os.replace consumes the second's file, whose replace then fails.
    #
    # Pid AND thread id, matching `envinstall._write`. The pid alone stopped being
    # unique when the heartbeat arrived: two writers now live in THIS process, and
    # a shared temp name between them is the same race with the same outcome — a
    # crashed installer whose venv was actually built fine.
    path = os.path.join(progress_dir, "progress.json")
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"stage": stage, "pct": pct, "detail": detail, "done": done,
                   "error": error, "pid": os.getpid(), "ts": time.time()}, f)
    os.replace(tmp, path)


def _elapsed(seconds):
    """`43s` / `2m14s` — an elapsed time a user can compare against their patience."""
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return "%dm%02ds" % (minutes, secs) if minutes else "%ds" % secs


def _acquire_python(version):
    """Download a uv-managed CPython `version`. Raises with uv's own stderr.

    `shutil.which`, because `envinstall._worker_env()` has already put the bundled
    uv on this process's PATH — the same route `fused`'s own builder finds it by, so
    there is one answer to "which uv" rather than two.

    No uv means no download is possible, and saying so beats a `FileNotFoundError`
    from the spawn: on a machine with no uv the server would not have asked for this
    interpreter in the first place (`envinstall._resolve_script_python` degrades to
    the running one), so reaching here without uv means something moved underneath
    us and the message should say which thing.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError(
            "cannot download Python %s: no uv on PATH. Install uv "
            "(https://docs.astral.sh/uv/), or start the server on Python %s."
            % (version, version)
        )
    # close_fds=False to get posix_spawn rather than fork()+exec, matching the spawn
    # discipline `venvs.py` documents at module level: a forked child runs PROJ's
    # pthread_atfork handler, which closes an inherited-but-invalid proj.db sqlite
    # handle and SIGSEGVs before exec — a bare returncode -11 with no stderr. `uv` is
    # dir-qualified here (it comes from `shutil.which`), which posix_spawn also
    # requires; a bare command name forks despite the flag.
    # This worker runs detached with no console of its own, so Windows would
    # otherwise pop a fresh one for a console-subsystem child like uv.exe.
    proc = subprocess.run([uv, "python", "install", version], env=_uv_env(),
                          capture_output=True, text=True, close_fds=False,
                          creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    if proc.returncode != 0:
        # Verbatim, exactly like the requirements install below: uv's own text names
        # the real problem (an offline machine, a proxy refusing the download, no
        # build for this platform), and that is the answer the user needs.
        raise RuntimeError(
            "Failed to download Python %s:\n%s"
            % (version, (proc.stderr or proc.stdout).strip())
        )


def _venv_python(venv_dir):
    """Where a venv keeps its own interpreter, on this OS.

    Kept in step with `envinstall._venv_python`; duplicated rather than imported
    for the same reason the stage percentages are (D152).
    """
    if os.name == "nt":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def _state_digest(project_dir):
    """sha256 of `pyproject.toml` — the declaration this environment was built from.

    The manifest only, never `uv.lock`: the lock is an OUTPUT of the sync, so
    folding it in would make the environment's own side effect a reason to
    rebuild it. That also means this no longer has to be read at any particular
    moment relative to `uv sync` — the sync does not touch the manifest.

    Byte-identical to `projectenv._compute_state_digest`, which READS what this
    writes. A divergence is not a subtle bug: every request would read its own
    just-built venv as stale and ask to rebuild it, forever. Duplicated rather
    than imported because this file must stay free of any `fused_render` import
    (D152).
    """
    try:
        with open(os.path.join(project_dir, "pyproject.toml"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def _build(project_dir, venv_dir, uv_cache_dir, python_executable):
    """`uv sync` the project into `venv_dir`; returns that venv's interpreter.

    An UNMARKED but existing venv directory is removed first. That is the D212
    repair, and it has to be a removal rather than a reconcile: the failure it
    exists for is a venv whose recorded base prefix does not exist, which
    `uv sync` would happily leave in place because the packages inside it are
    already correct. The marker's absence is the only signal that the directory
    is not to be trusted, and `envinstall.is_installed` is what unlinks it.

    Environment, not flags, for the two directories, because uv reads both itself
    and a flag would only cover the invocation we remember to put it on:

      UV_PROJECT_ENVIRONMENT  the venv lives in the home dir, never in the user's
                              folder (MD-7). Without it uv writes `<project>/.venv`,
                              which for a core template would be destroyed by the
                              release-time re-stage and cost a full re-download of
                              numpy/pyproj/imagecodecs on every upgrade.
      UV_CACHE_DIR            a sibling of the venv store, so cache and target are
                              on ONE filesystem and uv's hardlinks actually dedupe.
                              Across filesystems uv silently falls back to full
                              copies and every project pays for numpy again.

    `UV_LINK_MODE` is deliberately NOT set: uv already prefers hardlinks and
    degrades on its own, and pinning it here would override a user who had a
    reason to choose otherwise.

    A bare `uv sync`, with no `--frozen`. That is not a relaxation of
    reproducibility — uv uses an existing `uv.lock` as-is whenever it still
    matches the manifest, and re-resolves only the parts a manifest edit actually
    moved. Which is exactly the required behaviour: nothing changed means the
    committed versions, and a dependency added to `pyproject.toml` is picked up
    automatically. `--frozen` was here at first and had to go: it turns a
    manifest edit into a hard "the lockfile is out of date" error instead of
    reconciling it, and the whole point of the folder rule is that a user never
    has to run `uv sync` by hand (doing so would create an in-folder `.venv` and
    diverge from the home-dir store). Without a lock at all uv resolves and
    WRITES one, which is how a folder gains reproducibility by being run once.
    """
    uv = shutil.which("uv")
    if uv is None:
        # Plainly, because this is a supported configuration losing a capability
        # rather than a transient failure (D231): uv IS the builder, so without it
        # a folder that declares dependencies cannot get an environment at all.
        # Everything else still works — a folder with no pyproject.toml runs on
        # the app's own interpreter (PY-17) and needs nothing installed — so the
        # message says which half is affected and how to get it back.
        raise RuntimeError(
            "cannot build an environment for %s: uv is not installed, and uv is "
            "what builds project environments (`uv sync`).\n\n"
            "Install uv (https://docs.astral.sh/uv/getting-started/installation/) "
            "and try again. Until then, scripts in folders WITHOUT a "
            "pyproject.toml still run normally on this app's own interpreter — "
            "only folders that declare their own dependencies are affected.\n\n"
            "(The packaged macOS, Windows and Linux builds ship uv, so this only "
            "happens in a source checkout.)" % project_dir
        )
    if os.path.isdir(venv_dir) and not os.path.exists(os.path.join(venv_dir, _READY_MARKER)):
        shutil.rmtree(venv_dir, ignore_errors=True)

    # `--no-default-groups` because PY-16 makes `[project].dependencies` the whole
    # declaration, and without it uv also installs the default dependency-groups
    # (`[dependency-groups] dev`, which `uv init` and `uv add --dev` write). That
    # would put packages in the venv that `applicable_dependencies_of` never
    # reported — so the loader's "not installed yet: …" list and the environment
    # it builds would describe different things, and the marker/`app_satisfies`
    # fast path would be deciding against an incomplete list. One declaration, one
    # place. A folder whose dependencies live only in a group installs nothing,
    # which is the same answer PY-16 already gives it.
    cmd = [uv, "sync", "--no-default-groups", "--python", python_executable]

    # `_uv_env` scrubs PYTHON* and VIRTUAL_ENV: without the first, every
    # dependency uv has to BUILD rather than download as a wheel failed inside
    # the packaged macOS app (D266); without the second, uv warns and can target
    # the server's own venv.
    env = _uv_env(UV_PROJECT_ENVIRONMENT=venv_dir, UV_CACHE_DIR=uv_cache_dir)

    os.makedirs(os.path.dirname(venv_dir), exist_ok=True)
    os.makedirs(uv_cache_dir, exist_ok=True)
    # close_fds=False for posix_spawn rather than fork()+exec — the same discipline
    # every other spawn in this codebase follows; see `_acquire_python` above.
    proc = subprocess.run(cmd, cwd=project_dir, env=env,
                          capture_output=True, text=True, close_fds=False,
                          creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    if proc.returncode != 0:
        # Verbatim: uv's own text names the real problem (no wheel for this
        # platform, a bad pin, no network, a lock that no longer matches the
        # manifest), and that is the answer the user needs.
        raise RuntimeError(
            "Failed to build the environment for %s:\n%s"
            % (project_dir, (proc.stderr or proc.stdout).strip())
        )

    venv_python = _venv_python(venv_dir)
    if not os.path.exists(venv_python):
        raise RuntimeError(
            "`uv sync` reported success for %s but left no interpreter at %s"
            % (project_dir, venv_python)
        )

    # Sidecar BEFORE the marker. The marker means "installed"; the sidecar is what
    # a later request compares the declaration against. Marking first would leave
    # a window in which the venv reads as ready and cannot say what it holds, and
    # `is_installed` would call it stale and rebuild it immediately.
    tmp = os.path.join(venv_dir, _SIDECAR_NAME + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"path": os.path.abspath(project_dir),
                   "digest": _state_digest(project_dir)}, f)
    os.replace(tmp, os.path.join(venv_dir, _SIDECAR_NAME))
    with open(os.path.join(venv_dir, _READY_MARKER), "w", encoding="utf-8") as f:
        f.write("")
    return venv_python


def install(key, progress_dir, project_dir, venv_dir, uv_cache_dir,
            python_executable=None, acquire_python=None):
    os.makedirs(progress_dir, exist_ok=True)
    summary = os.path.basename(os.path.abspath(project_dir)) or project_dir
    # None means "the backend's own interpreter", and this worker was spawned
    # with it (`envinstall._spawn` uses `sys.executable`), so our own is the
    # faithful translation. See `_PINNED_PYTHON_VERSION` for why a version
    # string here would build the environment on the wrong python.
    python_executable = python_executable or sys.executable

    # Every record goes through this, and a terminal one LATCHES the file shut.
    #
    # Not politeness — the ordering is the feature. The heartbeat thread and the
    # terminal write both `os.replace` onto `progress.json`, and a beat landing
    # afterwards puts `done: false` back on the wire: the client polls a finished
    # install forever, which is the very "stuck" symptom the heartbeat exists to
    # cure, made permanent. The Event + `join` below normally stop the beat before
    # the terminal write, but `join` has a timeout and a beat can be parked inside
    # its own `_write` on a slow filesystem — so the guarantee lives here, in a lock
    # and a latch, rather than in the hope that the join won the race. (Same
    # reasoning as D181: an ordering that matters is enforced, not asserted.)
    write_lock = threading.Lock()
    finished = []

    def write(stage, pct, detail="", done=False, error=None):
        with write_lock:
            if finished:
                return  # a terminal record is already on disk; nothing may follow it
            _write(progress_dir, stage, pct, detail, done, error)
            # Latched only once the record is actually ON DISK. Latching before the
            # write would make a FAILED terminal write shut the file anyway, and the
            # `except` path's error record — the one carrying the reason — would
            # silently no-op, leaving `done: false` on the wire forever: the same
            # stuck poll this whole mechanism exists to prevent, reached by the
            # opposite route. The lock is what keeps this safe: `_write` and the
            # latch are one atomic step, so no beat can slip between them.
            if done:
                finished.append(True)

    def with_heartbeat(stage, pct, detail, work):
        """Run `work()` while `stage` beats liveness onto the wire; returns its result.

        `pct` STAYS put for the whole step and the stage never changes: neither long
        step in here has a computable percentage — upstream captures uv's output for
        the packages, and `_acquire_python` captures it for the interpreter — and a
        bar creeping upward on invented numbers is worse than an honest one that does
        not move, because the number is the thing a waiting user trusts most. What
        the beat refreshes is the elapsed time and `ts`, which is the only evidence
        of liveness that reaches the wire. The client renders these stages as
        indeterminate bars (runtime.js), so "alive but unquantified" is expressible
        without lying.

        One helper for both steps rather than two copies of the thread: the beat's
        correctness is subtle (the daemon flag, the `finally`, the interaction with
        the latch above), and two copies of subtle is two things to keep right.
        """
        write(stage, pct, detail)
        stop = threading.Event()
        started = time.time()

        def heartbeat():
            # `Event.wait`, never `sleep`: setting the event wakes it at once, so
            # shutdown costs microseconds instead of up to a full interval — and a
            # beat that fires during teardown is exactly what the latch above exists
            # to absorb.
            while not stop.wait(_HEARTBEAT_S):
                write(stage, pct, "%s (%s)" % (detail, _elapsed(time.time() - started)))

        # Daemon: a heartbeat wedged in a write must never keep this process alive
        # after its record says done, or `_pid_alive` reads the installer as still
        # running and the page polls a corpse.
        beat = threading.Thread(target=heartbeat, name="env-install-heartbeat",
                                daemon=True)
        beat.start()
        try:
            return work()
        finally:
            # In a `finally`, so the failure path stops the beat too — and it runs as
            # the exception propagates, i.e. BEFORE the `except` below writes the
            # error record. Both terminal writes are therefore behind the join.
            stop.set()
            beat.join(_HEARTBEAT_JOIN_S)

    try:
        if acquire_python:
            # Interpreter-only run (D214), and it deliberately stops here rather than
            # going on to build the venv: the venv belongs under a key that folds in
            # the interpreter just fetched, which is NOT the key this worker was
            # spawned under (`envinstall.PYTHON_BOOTSTRAP_KEY`). Building anyway would
            # fill a directory `is_installed` never looks at, and the page would
            # install, retry, and be told to install again. The server re-resolves
            # once this lands and starts the real install under the real key.
            with_heartbeat(
                "python", _PYTHON_PCT,
                "downloading Python %s (needed by %s)" % (acquire_python, summary),
                lambda: _acquire_python(acquire_python),
            )
            write("done", 100, "downloaded Python %s" % acquire_python, done=True)
            return

        # `create` and `install` are reported as one call because that is the
        # truth: `uv sync` does both behind capture_output=True, so the
        # transition between them is not observable from out here. The two stages
        # exist so the UI can say "preparing" before the long wait, not to imply
        # progress inside it.
        write("create", _CREATE_PCT, f"preparing the environment for {summary}")
        # `python_executable` is the server's own `_python_executable()`, handed
        # over rather than re-decided: the backend runs the code, so a different
        # interpreter here builds an environment the run cannot use.
        venv_python = with_heartbeat(
            "install", _INSTALL_PCT,
            f"resolving and installing the dependencies of {summary}",
            lambda: _build(project_dir, venv_dir, uv_cache_dir, python_executable),
        )
        write("done", 100, f"installed into {os.path.dirname(os.path.dirname(venv_python))}",
              done=True)
    except BaseException as e:  # noqa: BLE001
        # Verbatim: upstream's message already carries uv's/pip's stderr, which
        # names the real problem (a platform with no wheel, a bad pin, no
        # network). Only the exception class is prefixed, so the page can tell a
        # resolver failure from a disk-quota RuntimeError.
        write("error", 100, "", done=True, error=f"{type(e).__name__}: {e}")
        raise


def _detach():
    """Lead our own session, so this install outlives the request that began it.

    The DETACHMENT is done here rather than by the spawner, and that is not a
    style choice: `subprocess.Popen(start_new_session=True)` forces CPython off
    `posix_spawn` onto `fork()+exec`, and the spawner is the SERVER process,
    where PROJ is resident — its `pthread_atfork` child handler closes a stale
    SQLite handle and the forked child dies of SIGSEGV before it ever reaches
    this file (D277's crash; see `envinstall._spawn`). Called from the child,
    a few milliseconds later, it buys exactly the same thing with no fork.

    First statement of the run, before any record is written and long before uv
    is started, because it is `envinstall._kill`'s `killpg` that reaches that uv
    — and `_kill` only signals a group whose leader is this pid.

    EPERM means we are already a process-group leader, which is the same end
    state; anything else here is not worth failing an install over, since the
    only thing lost is the tidiness of the teardown.
    """
    if os.name == "nt" or not hasattr(os, "setsid"):
        return  # Windows detaches at spawn time (DETACHED_PROCESS) and never forks
    try:
        os.setsid()
    except OSError:
        pass


def main(args):
    """`<key> <progress_dir> <project_dir> <venv_dir> <uv_cache_dir>
    <python_executable> <acquire_python>`

    The empty string means None in BOTH optional slots (argv cannot carry it):
    translated here and nowhere else, so `install` receives the real values. Read as
    the literal `""` instead, the last slot would have this worker try to download a
    Python version called nothing on every ordinary install.
    """
    _detach()
    if len(args) < 7:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    (key, progress_dir, project_dir, venv_dir, uv_cache_dir,
     python_executable, acquire_python) = args[:7]
    install(key, progress_dir, project_dir, venv_dir, uv_cache_dir,
            python_executable or None, acquire_python=acquire_python or None)


if __name__ == "__main__":
    main(sys.argv[1:])
