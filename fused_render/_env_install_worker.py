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

**It builds with `uv sync`, in the project directory** (or, when that directory
cannot be written to, in a manifest-only mirror of it beside the venv — see
`_sync_root`, and note that the bundled AI runner folders are read-only on the
packaged builds). The declaration is the
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
import collections
import hashlib
import json
import os
import re
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

# How often the install stage re-writes its record while uv runs (D213).
#
# This USED to be proof of LIFE and nothing more: the whole download ran
# inside one `subprocess.run(capture_output=True)`, so nothing about uv's
# internals was observable from here, and the beat's only job was to keep
# `ts` moving so a client polling every 500ms could tell "still going" from
# "wedged". That premise is gone — `_build` now streams uv's own stderr
# through a `_UvProgress` (see below), so by the time this fires there is
# usually real byte-level news to report, not just a keepalive. The interval
# itself is unchanged: ~2s is still well under the rate at which a repaint
# could look stale, and it is still four orders of magnitude cheaper than the
# download it is reporting on — a live signal does not mean an instant one.
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


def _write(progress_dir, stage, pct, detail="", done=False, error=None,
           activity=None, bytes_done=None, bytes_total=None):
    # Unique temp name, not a shared `progress.json.tmp`: the server writes this
    # same file (envinstall._write) and two writers racing on one temp means the
    # first os.replace consumes the second's file, whose replace then fails.
    #
    # Pid AND thread id, matching `envinstall._write`. The pid alone stopped being
    # unique when the heartbeat arrived: two writers now live in THIS process, and
    # a shared temp name between them is the same race with the same outcome — a
    # crashed installer whose venv was actually built fine.
    #
    # `activity`/`bytes_done`/`bytes_total` are ADDITIVE: every existing key keeps
    # its current meaning and format (`fused_render/engine.py` and
    # `runtime.js`'s `paintInstall` read this same record for the non-AI
    # "Preparing my-app" path, and `tests/test_server_env_install.py` asserts on
    # those strings), and every writer that has nothing to say about bytes — the
    # `python`/`create`/`done`/`error` stages, and `install` before uv has
    # printed its first `Downloading` line — leaves them `None`, which is
    # indistinguishable from the record this function wrote before they existed.
    path = os.path.join(progress_dir, "progress.json")
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"stage": stage, "pct": pct, "detail": detail, "done": done,
                   "error": error, "pid": os.getpid(), "ts": time.time(),
                   "activity": activity, "bytes_done": bytes_done,
                   "bytes_total": bytes_total}, f)
    os.replace(tmp, path)


def _elapsed(seconds):
    """`43s` / `2m14s` — an elapsed time a user can compare against their patience."""
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return "%dm%02ds" % (minutes, secs) if minutes else "%ds" % secs


# --------------------------------------------------------------------------
# uv's own progress, parsed rather than invented.
#
# `_build` used to run `uv sync` behind `subprocess.run(capture_output=True)`
# and treat the whole call as one opaque, unobservable pause — see the old
# text of the comment above `_HEARTBEAT_S`. That premise turned out to be
# wrong: pointed at a non-tty PIPE, uv writes plain-text progress to STDERR,
# line-buffered, in real time. A probe against this exact concern —
# `subprocess.Popen([..], stderr=PIPE)` on a small (~34MB) package, with a
# wall-clock timestamp printed per line as it was READ — showed the
# `Downloading` line at t=0.6s and the two `Downloaded` confirmations arriving
# at t=7s and t=11s, not bunched at process exit. uv does not block-buffer to
# a pipe, so streaming instead of capturing is safe:
#
#   Using CPython 3.13.13
#   Creating virtual environment at: .venv
#   Resolved 3 packages in 1.01s
#   Downloading numpy (15.9MiB)
#   Downloading scipy (33.7MiB)
#    Downloaded numpy
#    Downloaded scipy
#   Prepared 2 packages in 13.55s
#   Installed 2 packages in 6ms
#
# uv's default concurrency is 50, so for a runner venv with dozens of
# dependencies essentially every `Downloading` line — and so every SIZE —
# appears within seconds of the sync starting, well before the one huge wheel
# (torch, for the ROCm/CUDA runners) finishes.
_DOWNLOADING_RE = re.compile(r"^Downloading (\S+) \(([\d.]+)\s*(B|KiB|MiB|GiB)\)$")
_DOWNLOADED_RE = re.compile(r"^\s*Downloaded (\S+)$")
_PREPARED_RE = re.compile(r"^Prepared \d+ packages? ")
_INSTALLED_RE = re.compile(r"^Installed \d+ packages? ")

#: uv reports binary units; multiplying by these gives bytes.
_UNIT_BYTES = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}


def _format_bytes(n):
    """`1.2 GB` / `340 MB` — BINARY steps (1024), labelled with the decimal
    names, matching `formatSize` in `frontend/src/platform/lib/format.ts`
    exactly (that function also divides by 1024 while calling the units
    "KB"/"MB"/"GB"). This phrase and `ModelProgress`'s own byte readout — which
    calls that JS function on the very `bytes_done`/`bytes_total` this worker
    writes — can appear side by side on one row, and two different numbers for
    the same install would look like a bug even though both would be "correct"
    under a different labelling convention. Deliberately NOT uv's own
    Ki/Mi/GiB spelling, for the same reason: one vocabulary end to end."""
    n = float(n)
    for unit, div in (("GB", 1024.0 ** 3), ("MB", 1024.0 ** 2), ("KB", 1024.0)):
        if n >= div:
            return "%.1f %s" % (n / div, unit)
    return "%d B" % int(n)


class _UvProgress:
    """Tracks one `uv sync`'s stderr, line by line, into numbers a heartbeat
    tick can report without inventing anything uv did not say.

    **Locked, because two threads genuinely touch this concurrently**:
    `_build`'s thread calls `feed()` as it reads uv's stderr, and the
    heartbeat thread calls `snapshot()` on its own timer. An earlier version
    of this class argued that was safe WITHOUT a lock because `feed` "only
    ever adds" to `_sizes`/`_downloaded` — that argument is wrong, not just
    incomplete: CPython raises `RuntimeError` for a mutation racing an
    iteration of the SAME container, and `snapshot` iterates both
    (`sum(self._sizes.values())`, the `for name in self._downloaded` sum, and
    the `self._sizes.items()` comprehension while picking the biggest pending
    package) — confirmed empirically (`RuntimeError('Set changed size during
    iteration')` within a second of a real install). That exception escaped
    the heartbeat thread, which killed it permanently: `progress.json` (`ts`
    included) then froze for the rest of a multi-GB install — the exact
    "stuck installer" symptom this feature exists to remove, reached by a new
    path. `fused_render.envinstall._remember_ending` documents the same
    hazard for `_ENDINGS`: a dict `pop`/iterate pair is not atomic just
    because each single dict operation is. The fix is the same house pattern
    — one lock across every read AND write, including copies (`list(...)`/
    `frozenset(...)` still iterate the source, so the copy has to happen
    UNDER the lock too, not instead of it).

    **The announced total is a LOWER BOUND that can only grow**, never shrink
    or get corrected downward: uv prints `Downloading` lines as it starts
    each fetch, not all at once, so a later line can raise the total after an
    earlier tick already reported one. That is the same argument
    `with_heartbeat`'s docstring makes against inventing a percentage — an
    honest number that occasionally jumps up beats a smooth one that is lying
    — and it is safe here specifically because `bytes_done` is a sum over the
    same dict `bytes_total` is: a size cannot be counted as done before it is
    counted as announced, so done can never exceed total (still asserted
    below, defensively, rather than trusted).

    Package-level only — measuring the in-flight bytes of a single large
    download (the ROCm torch wheel is one ~3.4GB step here) was investigated
    and dropped; see the module the feature shipped from for why (uv's cache
    directory grows by UNPACKED size, several times the compressed download,
    with no single file to stat as "bytes so far"). A package mid-download
    therefore contributes nothing to `bytes_done` until it is confirmed
    landed — the difference this class makes is at the AGGREGATE level, where
    a 40-package venv's small packages landing one by one is now visible
    progress instead of the single flat "installing…" it used to be.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sizes = {}          # package name -> announced bytes
        self._downloaded = set()  # names uv has confirmed landed
        #: resolving -> downloading -> preparing -> installing -> installed.
        #: Stays "resolving" forever for a sync where every wheel is already
        #: cached (no `Downloading` line ever prints), which is exactly the
        #: case `snapshot` below answers with "nothing new to say". NOT
        #: monotonic across `resolving`/`downloading`/`preparing`: a
        #: `Downloading` line seen while `preparing` moves back to
        #: `downloading` (see `feed`) — only `installing`/`installed`, reached
        #: from uv's own `Prepared`/`Installed` lines, are one-way.
        self.phase = "resolving"

    def feed(self, line):
        """One line of uv's stderr. Never raises: an unrecognised line (a
        warning, a future uv version's new wording) is simply not progress,
        not a reason to lose the ones already parsed."""
        m = _DOWNLOADING_RE.match(line)
        if m:
            name, value, unit = m.groups()
            with self._lock:
                self._sizes[name] = float(value) * _UNIT_BYTES[unit]
                if self.phase in ("resolving", "preparing"):
                    # `preparing` too, not just `resolving`: uv's concurrency
                    # cap (50) means a `Downloading` line can arrive AFTER a
                    # moment where everything announced so far had already
                    # landed (which is what latched `preparing` in the first
                    # place — see below). Without this, a late-announced
                    # torch sitting behind 50 smaller packages would freeze
                    # the phase at `preparing` — a full bar, reporting
                    # "preparing packages" — for the entire multi-GB torch
                    # download that follows it.
                    self.phase = "downloading"
            return
        m = _DOWNLOADED_RE.match(line)
        if m:
            with self._lock:
                self._downloaded.add(m.group(1))
                if self.phase == "downloading" and self._sizes and \
                        set(self._sizes) <= self._downloaded:
                    # Every size uv has ANNOUNCED so far has also been
                    # confirmed landed. uv may still announce MORE later —
                    # the `feed` branch above is what un-latches this — but
                    # nothing is in flight right now: uv is between the last
                    # `Downloaded` and the `Prepared` line, i.e. unpacking/
                    # linking, which for torch is the slow part this phase
                    # exists to name rather than let read as 100%.
                    self.phase = "preparing"
            return
        if _PREPARED_RE.match(line):
            with self._lock:
                self.phase = "installing"
            return
        if _INSTALLED_RE.match(line):
            with self._lock:
                self.phase = "installed"

    def snapshot(self, elapsed):
        """`(activity, bytes_done, bytes_total)` right now.

        `activity` is None before the first `Downloading` line and after the
        final `Installed` line — the two states in which this has nothing to
        add over the stage word already being reported, which is the
        contract `_ensure_venv` (fused_render/ai/supervisor.py) relies on to
        fall back cleanly. It is also None whenever nothing has actually been
        ANNOUNCED yet even though a later phase was reached — a fully-cached
        sync prints `Prepared`/`Installed` with no `Downloading` line at all,
        and `(word, 0, 0)` would render as a bare "0" in the frontend's byte
        column (`jobAmount`, `frontend/src/platform/lib/jobs.ts`) instead of
        nothing. `0` is a real, meaningful download size; "never announced"
        is not the same fact and must not be spelled the same way.
        """
        with self._lock:
            phase = self.phase
            if phase in ("resolving", "installed"):
                return None, None, None
            # Copies taken UNDER the lock: iterating a `list`/`set` COPY after
            # releasing the lock is still safe even if `feed` mutates the
            # originals concurrently, but the copy itself must happen while
            # the lock is held, or the copy operation is the very iteration
            # racing a mutation this lock exists to prevent.
            sizes = dict(self._sizes)
            downloaded = frozenset(self._downloaded)
        total = sum(sizes.values())
        if total == 0:
            # Nothing has been ANNOUNCED at all — see the docstring above.
            return None, None, None
        done = sum(sizes[name] for name in downloaded if name in sizes)
        done = min(done, total)  # see the class docstring: belt, not suspenders
        if phase == "downloading":
            pending = [(name, size) for name, size in sizes.items()
                      if name not in downloaded]
            # Named for the biggest package still in flight — concurrency is
            # 50, so several may be downloading at once, and the biggest is
            # the one most likely to be why the bar is not moving.
            biggest = max(pending, key=lambda kv: kv[1])[0] if pending else None
            phrase = "downloading %s%s of %s (%s)" % (
                (biggest + " — ") if biggest else "",
                _format_bytes(done), _format_bytes(total), elapsed)
            return phrase, done, total
        # preparing / installing: every announced byte has already landed
        # (done == total by construction), but uv is not finished — it is
        # unpacking wheels and linking them into the venv, which the wording
        # says explicitly rather than letting the bar read as stuck at 100%.
        word = "preparing" if phase == "preparing" else "installing"
        return "%s packages (%s)" % (word, elapsed), total, total


#: Bound on how much of uv's stderr is kept for a failure message: the LAST
#: this many lines, dropping older ones as new ones arrive. `_build`'s error
#: text must stay verbatim (SPEC PY-18 — a resolver error naming a missing
#: wheel is the actual answer a user needs), but "verbatim" cannot mean
#: "unbounded": a pathological or merely chatty uv run must not turn a
#: multi-GB install into a multi-GB progress reporter. 400 lines comfortably
#: covers every failure transcript this codebase has seen from uv (a
#: resolver conflict is a handful of lines; even a noisy dependency-graph
#: dump does not run to hundreds), while capping memory at a small, fixed
#: multiple of a typical line's length.
_STDERR_RING_LINES = 400


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
                          encoding="utf-8", errors="replace",
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


#: The `fused_render` package directory. This file LIVES in the package — it is
#: `fused_render/_env_install_worker.py`, spawned by path — so its own dirname is
#: that directory, with no import and no argv slot needed to learn it. (D152 is
#: about not IMPORTING the package in a detached child, not about not being part
#: of it.)
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))

#: Stands in for `_PACKAGE_DIR` in the identity of a folder that ships inside the
#: app, and is deliberately unspellable as a path so it can never collide with a
#: real folder of the user's. RESTATED from `projectenv._PACKAGE_IDENTITY`, like
#: every other constant here (D152); a test pins the two computations together.
_PACKAGE_IDENTITY = "<fused_render>"


def _source_identity(project_dir):
    """What the sidecar records *project_dir* as: its path, or its path IN the app.

    Byte-identical to `projectenv._venv_identity`, which is not a nicety — that
    function computes the venv KEY from this string, and `gc()` resolves the
    sidecar's copy of it back to a directory to decide whether the source is gone.
    A divergence here means a bundled venv whose sidecar names something `gc`
    cannot map, i.e. exactly the multi-gigabyte permanent orphan the
    package-relative identity was introduced to make collectable.

    Why not the absolute path: on the packaged builds the app's own path is not
    stable (the AppImage mounts itself at a fresh `.mount_FusedRxxxxxx` every
    launch), so an absolute path recorded here reads as merely UNREACHABLE forever
    — and `gc` deliberately keeps an unreachable source rather than reclaiming it,
    since that is what an unplugged external drive also looks like. A runner
    folder that a release removes or renames would strand its environment for
    good. See `projectenv._venv_identity` for the full argument; RESTATED rather
    than imported for the reason every constant in this file is (D152).
    """
    path = os.path.abspath(project_dir)
    try:
        rel = os.path.relpath(path, _PACKAGE_DIR)
    except ValueError:
        # Windows, different drives — nothing relative to say, so it is not ours.
        return path
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return path
    if rel == os.curdir:
        return _PACKAGE_IDENTITY
    return _PACKAGE_IDENTITY + "/" + rel.replace(os.sep, "/")


#: Suffix of the mirror directory, appended to the venv's own path. RESTATED from
#: `projectenv.MIRROR_SUFFIX` rather than imported, like every other constant in
#: this file (D152: no `fused_render` import in a detached child). That module
#: needs the name because `gc()` reclaims the mirror with the venv; a test pins
#: the two together.
_MIRROR_SUFFIX = ".src"

#: Names copied into a read-only project's mirror, in `_sync_root`. The
#: declaration and the three files uv reads BESIDE it that a folder can
#: legitimately commit — nothing else, because a mirror is not a copy of the
#: project (a read-only tree can be arbitrarily large, and none of the rest is an
#: input to resolution for the `package = false` folders this path exists for).
#:
#: `uv.toml` is here because it is uv's own configuration for the folder — a
#: private index, an exclude-newer date, build settings. Left out of the mirror it
#: does not silently stop applying somewhere visible; it just quietly does not
#: apply, and a runner pinned to an internal index would resolve against PyPI
#: instead with no message anywhere saying why.
_MIRRORED_NAMES = ("pyproject.toml", "uv.lock", "uv.toml", ".python-version")

#: The one entry of `_MIRRORED_NAMES` that is uv's OUTPUT rather than something
#: the project ships, and therefore the one exempt from "drop what the source
#: dropped" in `_sync_root`. Deleting the mirror's copy because the source has
#: none would delete the lock on every single build — the source never has one,
#: which is the entire reason the mirror persists.
_MIRROR_OWN_OUTPUT = "uv.lock"


def _writable_dir(path):
    """Can a file actually be CREATED in *path*? Answered by doing it.

    `os.access(path, os.W_OK)` is the obvious call and it is the wrong one. On
    Windows it reports the read-only ATTRIBUTE, which is meaningless for a
    directory: an ACL-protected `C:\\Program Files\\FusedRender\\...` answers
    "writable", `_sync_root` then runs the sync in place, and uv dies with
    `Access is denied. (os error 5)` — the exact platform the mirror exists to
    cover, silently mis-answered. POSIX has a smaller version of the same hole:
    `os.access` consults the mode bits, so a directory denied by an ACL entry or
    by SELinux also reports writable.

    A probe answers the question that is actually being asked ("can uv put
    `uv.lock` here"), because it IS that operation. `O_CREAT|O_EXCL` so it can
    never truncate something of the user's, and the pid in the name so two
    installs probing one folder at once cannot collide on it.

    The cleanup objection does not survive contact: a probe file we managed to
    create is a file in a directory we have just proven we can write to, so it is
    removable by definition. The unlink is still best-effort — a stray zero-byte
    file in a folder that syncs fine is not a reason to fail a build a user is
    waiting on.
    """
    probe = os.path.join(path, ".fused-render-write-probe.%d" % os.getpid())
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return False
    os.close(fd)
    try:
        os.unlink(probe)
    except OSError:
        pass
    return True


def _unlink_quietly(path):
    """Remove *path* if it is there. A file the mirror should no longer hold that
    refuses to go is not worth failing a build over — the next build tries again,
    and the mirror lives in the home dir where nothing else reads it."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _read_bytes(path):
    """*path*'s contents, or None when it cannot be read. Absent and unreadable
    are one answer here because both mean "nothing to compare against"."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _sync_root(project_dir, venv_dir):
    """Where `uv sync` runs: the project dir, or a writable MIRROR of its manifest.

    `uv sync` WRITES to the directory it runs in — it puts `uv.lock` there, which
    is the whole point for a user's folder (the lock is source and gets
    committed). A project dir that cannot be written to therefore fails the sync
    outright, with uv's own `failed to write to file .../uv.lock: Read-only file
    system (os error 30)` and no environment built.

    That is not a hypothetical: the AI runner folders (`fused_render/ai/runners/*`)
    ship INSIDE the app, and the app's own tree is read-only in the shapes it is
    normally installed in — the AppImage runs from a squashfs mount, and a Windows
    install under `Program Files` is not user-writable. Every model download on
    those builds died here. A user's folder on a read-only mount (a mounted
    archive, a `ro` network share) is the same failure.

    So: when the folder cannot hold uv's output, the sync runs in
    `<venv_dir>.src` instead — a mirror holding the declaration, uv's own
    configuration beside it, and nothing else (`_MIRRORED_NAMES`). The venv, the
    cache and the interpreter are unchanged, so the environment built is the same
    one; only the directory uv is allowed to litter moves.

    That the mirror is enough to resolve from is a claim about the folders this
    exists for, not about every folder: the AI runners and the templates are
    `[tool.uv] package = false`, so nothing outside `pyproject.toml` and uv's
    config beside it takes part in resolution. A read-only folder that DID declare
    a `build-system` (uv would build and install the folder itself, from sources
    the mirror does not hold) or a `[tool.uv.sources] {path = ...}` relative
    dependency (which the mirror would resolve relative to itself, i.e. nowhere)
    would resolve differently here than it does in place. That is a stated limit
    of this fallback rather than a silent one; the alternative is copying an
    arbitrarily large read-only tree on every build.

    The mirror is NOT wiped between builds, and that is what makes it worth
    having: the `uv.lock` uv wrote there on the first build is still there on the
    next one, so a rebuild of a bundled runner resolves against the versions the
    first one picked instead of re-resolving from PyPI. Source still beats derived
    — a lock the project itself ships overwrites it, and so does the manifest.

    But a kept lock has to EXPIRE, and the manifest is what expires it. Bare
    `uv sync` re-resolves only what the manifest invalidates, and a widened
    ceiling invalidates nothing: the documented pattern in those runner manifests
    is a pre-1.0 ceiling (`mlx-lm>=0.31,<0.32`), so a release that widens it to
    `<0.33` still has the locked 0.31.x satisfying the range. The rebuild would
    reinstall the identical versions and the deliberate upgrade would never
    happen — packaged builds behaving as if a lock had been committed and never
    refreshed, while a dev checkout (writable folder, no lock, re-resolves every
    time) picked the new one up. Exactly inverted from what those manifests say
    they are relying on.

    So the mirror's own copy of `pyproject.toml` is read as the record of what its
    lock was resolved AGAINST: bytes equal means the lock still describes this
    declaration and is kept (which is the rebuild-after-repair case the mirror is
    for), bytes different means the declaration moved and the lock goes. The
    manifest copy IS the state — no separate marker file to write, fail to write,
    or leave behind.

    Names the source no longer has are removed from the mirror, `uv.lock` alone
    excepted (there the mirror is the only copy that ever existed). Without that,
    a release that drops a runner's `.python-version`, or withdraws a `uv.lock` it
    used to ship, leaves the withdrawn file governing every later build forever —
    a read-only folder cannot be edited to undo it, and the file is somewhere the
    user will never look.

    Writability is a question about the DIRECTORY and it is asked by probing, not
    by `os.access` — see `_writable_dir` for why that distinction is the
    difference between working and not on Windows.
    """
    # A folder that is not there is not a read-only folder, and mirroring it would
    # answer "no pyproject.toml in an empty directory" where the direct sync says
    # the project path does not exist. The probe cannot tell those apart — nothing
    # can be created in either — so the question is asked separately.
    if not os.path.isdir(project_dir) or _writable_dir(project_dir):
        return project_dir
    mirror = os.path.abspath(venv_dir) + _MIRROR_SUFFIX
    os.makedirs(mirror, exist_ok=True)

    # Before any copying, while the mirror's manifest still describes the build
    # its lock came from. A mirror with no manifest copy is not a mirror whose
    # lock can be vouched for, so the lock goes: `_read_bytes` cannot tell absent
    # from unreadable, and treating both as "matches an unreadable source" is the
    # one direction that would keep a lock nothing has been compared against.
    mirror_manifest = _read_bytes(os.path.join(mirror, "pyproject.toml"))
    if (mirror_manifest is None
            or mirror_manifest != _read_bytes(
                os.path.join(project_dir, "pyproject.toml"))):
        _unlink_quietly(os.path.join(mirror, _MIRROR_OWN_OUTPUT))

    for name in _MIRRORED_NAMES:
        src = os.path.join(project_dir, name)
        dst = os.path.join(mirror, name)
        if os.path.exists(src):
            shutil.copyfile(src, dst)
        elif name != _MIRROR_OWN_OUTPUT:
            _unlink_quietly(dst)
    return mirror


def _build(project_dir, venv_dir, uv_cache_dir, python_executable, tracker=None):
    """`uv sync` the project into `venv_dir`; returns that venv's interpreter.

    `tracker` is a `_UvProgress` that every line of uv's stderr is fed into as
    it streams, for a heartbeat elsewhere to read concurrently — `install()`
    is the only real caller and always passes one. It defaults to a
    throwaway instance rather than being required so every existing direct
    caller of `_build` (this module's own tests, mainly) keeps working
    unchanged; nothing reads a tracker nobody handed in.

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

    It runs in `_sync_root(project_dir, venv_dir)` rather than in `project_dir`
    itself — the same directory in every case a folder can be written to, and a
    manifest-only mirror when it cannot (the bundled AI runner folders, on every
    build whose tree is read-only). See that function.

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
    # After the makedirs above (the mirror is a SIBLING of the venv, so its parent
    # is the one they create) and after the unmarked-venv removal (which must not
    # be able to take the mirror's lock with it).
    sync_root = _sync_root(project_dir, venv_dir)
    if tracker is None:
        tracker = _UvProgress()  # nobody is watching; feed it anyway for one code path
    # close_fds=False for posix_spawn rather than fork()+exec — the same discipline
    # every other spawn in this codebase follows; see `_acquire_python` above.
    #
    # `stdout=DEVNULL, stderr=PIPE`, not `capture_output=True`: uv's own progress
    # text — everything the `_UvProgress` comment block above quotes — goes to
    # STDERR (confirmed by the probe cited there), so stdout has nothing this
    # needs and piping it too would only be a second buffer to drain. Streamed
    # line by line rather than read all at once so a heartbeat elsewhere can see
    # `tracker`'s state WHILE uv is still running, which is the entire point.
    # A bounded ring, not the growing list `capture_output` used to hand back:
    # `_STDERR_RING_LINES` caps memory against a pathological or merely chatty
    # uv run, while still keeping enough of the TAIL to raise verbatim below —
    # a resolver failure's own explanation is always the last thing uv prints
    # before exiting non-zero, never the first.
    ring = collections.deque(maxlen=_STDERR_RING_LINES)
    # `with Popen(...)` PLUS an explicit `kill()` on any exception — the same
    # two-part discipline `subprocess.run` itself uses internally (its source
    # is `with Popen(...) as process: try: ... except: process.kill(); raise`).
    # The `with` alone is not enough: `Popen.__exit__` only closes the pipes
    # and calls `wait()`, it does NOT kill — so a plain `with` here would wait
    # forever for a `uv sync` that has no reason to exit just because ITS
    # reader raised. Without the explicit kill, an exception out of this loop
    # (a bug in `tracker.feed`, a cancel unwinding through here) would leave a
    # multi-GB `uv sync` running unsupervised, attached to nothing.
    with subprocess.Popen(cmd, cwd=sync_root, env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          text=True, bufsize=1, close_fds=False,
                          encoding="utf-8", errors="replace",
                          creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0) as proc:
        try:
            for line in proc.stderr:
                line = line.rstrip("\n")
                ring.append(line)
                tracker.feed(line)
        except BaseException:
            proc.kill()
            raise
    if proc.returncode != 0:
        # Verbatim: uv's own text names the real problem (no wheel for this
        # platform, a bad pin, no network, a lock that no longer matches the
        # manifest), and that is the answer the user needs. The ring holds the
        # TAIL of stderr rather than all of it (see above), which is the part
        # that matters for a failure — uv prints its diagnosis right before
        # exiting, not at the top of a long resolve.
        raise RuntimeError(
            "Failed to build the environment for %s:\n%s"
            % (project_dir, "\n".join(ring).strip())
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
    #
    # `_source_identity`, not the absolute path: for a folder inside the app the
    # absolute path is this launch's mount directory, which `gc()` can never
    # resolve again — see that function and `projectenv.write_sidecar`, the other
    # writer of this same record.
    tmp = os.path.join(venv_dir, _SIDECAR_NAME + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"path": _source_identity(project_dir),
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

    def write(stage, pct, detail="", done=False, error=None,
              activity=None, bytes_done=None, bytes_total=None):
        with write_lock:
            if finished:
                return  # a terminal record is already on disk; nothing may follow it
            _write(progress_dir, stage, pct, detail, done, error,
                   activity=activity, bytes_done=bytes_done, bytes_total=bytes_total)
            # Latched only once the record is actually ON DISK. Latching before the
            # write would make a FAILED terminal write shut the file anyway, and the
            # `except` path's error record — the one carrying the reason — would
            # silently no-op, leaving `done: false` on the wire forever: the same
            # stuck poll this whole mechanism exists to prevent, reached by the
            # opposite route. The lock is what keeps this safe: `_write` and the
            # latch are one atomic step, so no beat can slip between them.
            if done:
                finished.append(True)

    def with_heartbeat(stage, pct, detail, work, progress=None):
        """Run `work()` while `stage` beats liveness onto the wire; returns its result.

        `pct` STAYS put for the whole step and the stage never changes: neither long
        step in here has a computable PERCENTAGE — `_acquire_python` still captures
        its output wholesale, so the interpreter download stays exactly as
        indeterminate as before — and a bar creeping upward on an invented
        percentage is worse than an honest one that does not move, because the
        number is the thing a waiting user trusts most. What the beat used to
        refresh was only the elapsed time and `ts`, the sole evidence of liveness
        on the wire. The client still renders `pct`/`stage` as an indeterminate
        bar either way (runtime.js) — this is about what rides ALONGSIDE that bar,
        not about replacing it.

        `progress`, when given, is a `_UvProgress` some OTHER thread (`_build`,
        streaming uv's stderr) is concurrently feeding lines into. Each beat reads
        its `.snapshot(elapsed)` and reports whatever it has — `None`s before the
        first `Downloading` line, same as if `progress` had never been passed at
        all, which is the fallback `_ensure_venv` (supervisor.py) relies on. Left
        as the default `None` for `_acquire_python`'s heartbeat: interpreter
        downloads run through `subprocess.run(capture_output=True)` still (there
        is exactly one file involved, so package-level progress does not apply),
        and passing no tracker there is how that call keeps reporting precisely
        what it always has.

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
                elapsed = _elapsed(time.time() - started)
                activity = bytes_done = bytes_total = None
                if progress is not None:
                    activity, bytes_done, bytes_total = progress.snapshot(elapsed)
                write(stage, pct, "%s (%s)" % (detail, elapsed),
                      activity=activity, bytes_done=bytes_done, bytes_total=bytes_total)

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

        # `create` and `install` are reported as one call because `uv sync` does
        # both in one invocation and the two stages exist so the UI can say
        # "preparing" before the long wait, not to imply progress inside it —
        # that much is unchanged. What HAS changed is that "install" is no
        # longer a single unobservable pause: `_UvProgress` (fed by `_build`,
        # below, as it streams uv's stderr) is what turns the beats during this
        # stage from a bare elapsed-time keepalive into real bytes and a real
        # phrase, whenever uv has printed anything to report.
        write("create", _CREATE_PCT, f"preparing the environment for {summary}")
        # `python_executable` is the server's own `_python_executable()`, handed
        # over rather than re-decided: the backend runs the code, so a different
        # interpreter here builds an environment the run cannot use.
        tracker = _UvProgress()
        venv_python = with_heartbeat(
            "install", _INSTALL_PCT,
            f"resolving and installing the dependencies of {summary}",
            lambda: _build(project_dir, venv_dir, uv_cache_dir, python_executable, tracker),
            progress=tracker,
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
