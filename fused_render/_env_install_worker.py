"""Detached worker that builds one project venv, spawned by envinstall.start().

Run as:  python _env_install_worker.py <key> <progress_dir> <project_dir>
                                      <venv_dir> <uv_cache_dir>
                                      <python_executable> <acquire_python>

Every path arrives in argv rather than being derived here, because this file
must stay free of any `fused_render` import (D152 — importing the package in a
detached child is a bootstrap that broke once already) and so cannot call
`projectenv`. Re-deriving the venv directory would also be a second derivation
of a cache key, which is how a loader ends up filling a directory no run reads.

`<uv_cache_dir>` is EMPTY, same idiom, whenever `envinstall._spawn` was handed
`projectenv.uv_cache_dir() is None` — which is the ordinary case now, not the
exception: `_build` then runs `uv sync` with no `UV_CACHE_DIR` override at all,
deferring to uv's OWN default (XDG on Linux, `~/.cache/uv` on macOS too —
NOT `~/Library/Caches`, `%LOCALAPPDATA%` on Windows) instead of an explicit
sibling of the venv store — and to whatever `UV_CACHE_DIR` is already
AMBIENT in this process's own environment, if one is: `_uv_env`'s base is a
plain copy of `os.environ`, so a value set by the shell, or by CI's own
`setup-uv` action, rides along untouched rather than being stripped, which
would be imposing a different cache choice of our own. That explicit
sibling used to be unconditional and fragmented per branch/worktree as a
result — see `projectenv.uv_cache_dir()` for the history and the trade this
accepts (giving up the one-filesystem hardlink guarantee everywhere the two
happen to already coincide, to stop a guaranteed multi-gigabyte redownload
everywhere they used to differ). An explicit path still arrives when the
caller set `FUSED_RENDER_HOME` — not only the test suite's own isolation,
but also the packaged Linux/Windows desktop app, which sets it
unconditionally for every launch (`supervisor.paths.DesktopPaths`, D131).

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
import select
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import fcntl  # POSIX only; Windows raises ImportError, handled below.
    import pty
    import termios
except ImportError:  # pragma: no cover - exercised on Windows CI, not here
    fcntl = None
    pty = None
    termios = None

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
           activity=None, bytes_done=None, bytes_total=None, needs_build=None,
           platform_incompatible=None):
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
    #
    # `needs_build`: set only on the terminal `error` record of a `--no-build`
    # refusal (see `_no_build_package`, below) — the bare package name uv
    # refused to build from source, so `envinstall._mirror_into_jobs` can tell
    # this apart from a genuine resolver failure, and `runtime.js` can offer
    # the "Install anyway" retry off this field instead of re-parsing `error`'s
    # text itself. `error` still carries uv's message verbatim (SPEC PY-18) —
    # this is an ADDITIONAL field, not a replacement for it.
    #
    # `platform_incompatible`: set instead of (never alongside) `needs_build`
    # when `_incompatible_platform_name` (below) determines the refused
    # package can never build HERE — a dict of `{"package", "platform",
    # "current_platform"}`, or None. `error` is still uv's verbatim text
    # either way; this is what lets `envinstall._mirror_into_jobs` and
    # `runtime.js` each render their own plain-language sentence instead of
    # uv's jargon, and skip the "Install anyway" retry entirely for a
    # platform nothing will ever satisfy.
    path = os.path.join(progress_dir, "progress.json")
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"stage": stage, "pct": pct, "detail": detail, "done": done,
                   "error": error, "pid": os.getpid(), "ts": time.time(),
                   "activity": activity, "bytes_done": bytes_done,
                   "bytes_total": bytes_total, "needs_build": needs_build,
                   "platform_incompatible": platform_incompatible}, f)
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
        #: package name -> bytes transferred so far, for a name NOT yet in
        #: `_downloaded`. Populated only under a pty (`feed_pty_progress`) —
        #: a pipe never gives us anything between "announced" and "landed"
        #: for one package, which is the whole reason the pty exists: a
        #: single dominant wheel (torch) has nothing else to report progress
        #: FROM. Cleared for a name the moment it is marked downloaded, so
        #: `snapshot` never double-counts a package as both in-flight and
        #: confirmed.
        self._inflight = {}
        #: The set of names a pty's LAST redraw frame mentioned, or None
        #: before the first one — see `feed_pty_frame`. `None` is a real
        #: third state, not just "empty": an empty frozenset means "the
        #: block just went blank", which IS diffed against the previous
        #: frame (everyone in it just finished); `None` means there is no
        #: previous frame to diff against yet.
        self._last_pty_frame_names = None
        #: resolving -> downloading -> preparing -> installing -> installed.
        #: Stays "resolving" forever for a sync where every wheel is already
        #: cached (no `Downloading` line ever prints), which is exactly the
        #: case `snapshot` below answers with "nothing new to say". NOT
        #: monotonic across `resolving`/`downloading`/`preparing`: a
        #: `Downloading` line seen while `preparing` moves back to
        #: `downloading` (see `feed`) — only `installing`/`installed`, reached
        #: from uv's own `Prepared`/`Installed` lines, are one-way.
        self.phase = "resolving"

    def _mark_downloaded_locked(self, name):
        """*name* has fully landed. Caller holds `self._lock`.

        One place for the two routes that can decide this: a plain
        ` Downloaded <name>` line (piped mode) and a pty progress row whose
        own `done` has reached its own `total` (tty mode — uv prints no
        separate confirmation there at all, so reaching the announced total
        IS the confirmation). Both must apply the SAME "did everything
        announced just land" phase check, or the two modes would disagree
        about when `preparing` starts for no reason a user could see.
        """
        self._downloaded.add(name)
        self._inflight.pop(name, None)
        if self.phase == "downloading" and self._sizes and \
                set(self._sizes) <= self._downloaded:
            # Every size uv has ANNOUNCED so far has also been confirmed
            # landed. uv may still announce MORE later — `feed`/
            # `feed_pty_progress` are what un-latch this — but nothing is in
            # flight right now: uv is between the last confirmation and the
            # `Prepared` line, i.e. unpacking/linking, which for torch is the
            # slow part this phase exists to name rather than let read as
            # 100%.
            self.phase = "preparing"

    def feed(self, line):
        """One line of uv's stderr. Never raises: an unrecognised line (a
        warning, a future uv version's new wording) is simply not progress,
        not a reason to lose the ones already parsed.

        This is the PIPED-mode parser (`Downloading <name> (<size>)` /
        ` Downloaded <name>`), and it stays exactly as it was: `_build`
        still feeds it the plain lines it always has, in EITHER mode — a pty
        session prints no such lines at all (confirmed empirically: uv never
        writes "Downloading"/"Downloaded" once it believes stdout is a
        terminal, it only draws the live multi-package bar `feed_pty_progress`
        parses instead), so on a pty these two branches are simply dormant.
        `Resolved`/`Prepared`/`Installed` are NOT dormant either way — uv
        prints those the same in both modes, confirmed against the same
        probe — which is why this method keeps being the one thing every
        caller feeds complete lines through.
        """
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
                self._mark_downloaded_locked(m.group(1))
            return
        if _PREPARED_RE.match(line):
            with self._lock:
                # A backstop for pty mode: uv does not print this line while
                # anything is still downloading, so whatever is STILL
                # unconfirmed at this point must have finished — but the
                # per-frame route (`feed_pty_frame`) cannot see it, because
                # the final display clear (every remaining row vanishing at
                # once, right before this exact line) carries no
                # distinguishing text for that method to recognise as one
                # more frame. A no-op in piped mode, where `_DOWNLOADED_RE`
                # already confirmed everything before this line could ever
                # print.
                for name in list(self._sizes):
                    if name not in self._downloaded:
                        self._downloaded.add(name)
                        self._inflight.pop(name, None)
                self.phase = "installing"
            return
        if _INSTALLED_RE.match(line):
            with self._lock:
                self.phase = "installed"

    def feed_pty_frame(self, names_in_frame):
        """The set of package names a pty's LATEST redraw frame mentioned.

        uv stops redrawing a package's row the INSTANT it lands — it prints
        no separate confirmation under a tty the way a pipe's `Downloaded`
        line does (see `feed`'s docstring) — so a name that WAS in the
        previous frame and is gone from this one, while still short of its
        own announced total, USUALLY has finished by elimination. Confirmed
        against a real, multi-package capture: names drop out of the
        display ONE AT A TIME as each completes, not all together — except
        at the very final clear, which drops everyone remaining at once
        with no marker text of its own; `feed`'s `_PREPARED_RE` branch is
        the backstop for exactly that gap.

        **"Usually", not "always" — a vanished name can also mean DISPLACED,
        not finished.** `pty.openpty()` sets no winsize, so uv falls back to
        an 80x24 terminal and renders at most ~23 rows while downloading up
        to 50 wheels concurrently; a package still downloading simply
        scrolls out of the visible frame, and reading that as completion is
        indistinguishable from a real completion at THIS layer alone.
        Measured on a real 149-package capture: 44 names vanished from a
        frame while their last reading was under 90% of their own announced
        total (several at 0 B), and 4 of those reappeared in a LATER frame
        still downloading — 153.2 MB was credited as transferred that had
        not been, and the run reported 147.3 of 152.5 MB nowhere near done.
        `_run_uv_via_pty` now sets a tall winsize specifically so this
        should not happen in practice (uv never has a reason to clamp), but
        this method does not TRUST that — it is the second, independent
        layer: a vanished name is confirmed only when its last known
        in-flight reading was at or near its own total already
        (`_PTY_NEAR_TOTAL_FRACTION`). One that vanished well short of it is
        left exactly as it was — neither confirmed nor discarded, so its
        last-known bytes keep counting toward `done` (honest, if slightly
        stale) until it either reappears with fresh progress or the
        `Prepared` backstop confirms it. The damage of confirming wrongly
        would otherwise be PERMANENT: `feed_pty_progress` early-returns for
        any name already in `_downloaded`, so a displaced package's real
        bytes could never be counted again, and `_mark_downloaded_locked`
        would latch `phase = "preparing"` with `done == total` while
        gigabytes were still arriving — precisely the "stuck at 100%" lie
        this whole feature exists to remove, reached by a different route.

        Without this method at all, `_downloaded` gains almost no members
        under a pty: `feed_pty_progress`'s OWN route to
        `_mark_downloaded_locked` (a `done >= total` reading) fires on very
        little in practice, because uv's LAST redraw of a row measures short
        of the total even for a genuinely finished one — a real capture's
        numpy install: 15.55 of 15.94 MiB on its last observed row.
        """
        with self._lock:
            frame = frozenset(names_in_frame)
            if self._last_pty_frame_names is not None:
                vanished = self._last_pty_frame_names - frame - self._downloaded
                for name in vanished:
                    total = self._sizes.get(name, 0.0)
                    last_seen = self._inflight.get(name, 0.0)
                    if total > 0 and last_seen >= total * _PTY_NEAR_TOTAL_FRACTION:
                        self._mark_downloaded_locked(name)
                    # else: DISPLACED, not finished (see the docstring) — its
                    # `_inflight` reading is left exactly as it was, so
                    # `snapshot` keeps counting it honestly without lying
                    # that it landed.
            self._last_pty_frame_names = frame

    def feed_pty_progress(self, name, done_bytes, total_bytes):
        """One reading of *name*'s live in-flight bytes, off a PTY.

        Only a terminal gets this from uv at all — pointed at a pipe, uv
        suppresses per-package in-flight bytes entirely and only ever
        confirms a package once it is fully landed (`feed`, above). Under a
        pty it instead redraws a multi-line bar continuously, e.g.:

            numpy                ------------------------------  6.31 MiB/15.94 MiB

        parsed by `_PTY_PROGRESS_RE` in the reader that calls this (never a
        plain `Downloading`/`Downloaded` line — confirmed empirically, see
        `feed`'s docstring). This is what makes ONE dominant wheel (the
        ROCm/CUDA torch install this feature shipped to fix) show movement
        at all: package-level alone has nothing to report from until the
        whole multi-gigabyte file lands.

        `bytes_total` stays authoritative the same way an announced
        `Downloading` size is: `max()`'d in, never lowered, because uv's own
        number for a package's size does not change mid-download.
        """
        with self._lock:
            if name in self._downloaded:
                return  # already confirmed; a stale/replayed row must not un-confirm it
            self._sizes[name] = max(self._sizes.get(name, 0.0), total_bytes)
            if self.phase in ("resolving", "preparing"):
                # Same reasoning as the `Downloading`-line branch in `feed` —
                # and done FIRST, before the completed-on-first-report check
                # below: a package that is already fully landed the very
                # first time it is reported (a small file, or two `select()`
                # ticks apart) must still pass through `downloading` on its
                # way to `preparing`, or `_mark_downloaded_locked`'s own
                # "did everything just land" check — which requires
                # `phase == "downloading"` — would never fire, and the phase
                # would stay `resolving` forever.
                self.phase = "downloading"
            if self._sizes[name] > 0 and done_bytes >= self._sizes[name]:
                # The pty gives no separate "Downloaded" confirmation the way
                # a pipe does (see `feed`'s docstring) — reaching the
                # announced total off this row IS the confirmation.
                self._mark_downloaded_locked(name)
                return
            self._inflight[name] = max(self._inflight.get(name, 0.0), done_bytes)

    def snapshot(self, elapsed):
        """`(activity, bytes_done, bytes_total)` right now.

        `activity` is None before the first `Downloading` line/pty progress
        row and after the final `Installed` line — the two states in which
        this has nothing to add over the stage word already being reported,
        which is the contract `_ensure_venv` (fused_render/ai/supervisor.py)
        relies on to fall back cleanly. It is also None whenever nothing has
        actually been ANNOUNCED yet even though a later phase was reached —
        a fully-cached sync prints `Prepared`/`Installed` with no
        `Downloading` line at all, and `(word, 0, 0)` would render as a bare
        "0" in the frontend's byte column (`jobAmount`,
        `frontend/src/platform/lib/jobs.ts`) instead of nothing. `0` is a
        real, meaningful download size; "never announced" is not the same
        fact and must not be spelled the same way.

        **The phrase never carries the byte magnitudes — only what the
        numbers cannot express: which package, and elapsed time.**
        `activity` has exactly one consumer, `supervisor._ensure_venv`, and
        that same call site ALSO sets `done`/`total`/`unit="bytes"` on the
        job row from this same tuple's other two members — so whenever the
        phrase is shown, the row is already rendering the numbers
        (`jobAmount` + `dl-pct`, `frontend/src/platform/lib/jobs.ts`). A
        phrase reading `"downloading torch — 259.2 MB of 5.8 GB"` printed a
        SECOND pair right next to the row's own `0.25 / 5.76 GB` — the same
        "one number pair, two renderers, one row" defect already fixed once
        for the Denoising caption (`torch_image.py`) — and the two did not
        even agree: `jobAmount` scales both sides by the larger value, while
        the phrase's own formatting scaled each side independently (the
        helper that did that, `_format_bytes`, was removed with this fix —
        its only caller), so one instant produced two different-looking
        numbers for a user not even trying to compare them.
        """
        with self._lock:
            phase = self.phase
            if phase in ("resolving", "installed"):
                return None, None, None
            # Copies taken UNDER the lock: iterating a `list`/`set`/`dict`
            # COPY after releasing the lock is still safe even if `feed`/
            # `feed_pty_progress` mutate the originals concurrently, but the
            # copy itself must happen while the lock is held, or the copy
            # operation is the very iteration racing a mutation this lock
            # exists to prevent.
            sizes = dict(self._sizes)
            downloaded = frozenset(self._downloaded)
            inflight = dict(self._inflight)
        total = sum(sizes.values())
        if total == 0:
            # Nothing has been ANNOUNCED at all — see the docstring above.
            return None, None, None
        # Confirmed bytes PLUS whatever a pty says is in flight for the rest
        # — `inflight` is empty in piped mode (nothing ever populates it
        # there), so this is exactly the old `done` sum on a pipe and a
        # finer one on a pty.
        done = sum(sizes[name] for name in downloaded if name in sizes)
        done += sum(inflight.get(name, 0.0) for name in sizes if name not in downloaded)
        done = min(done, total)  # see the class docstring: belt, not suspenders
        if phase == "downloading":
            pending = [(name, size) for name, size in sizes.items()
                      if name not in downloaded]
            # Named for the biggest package still in flight — concurrency is
            # 50, so several may be downloading at once, and the biggest is
            # the one most likely to be why the bar is not moving. No bytes
            # here — see the docstring above for why the row's own numbers
            # are the only place they may appear.
            biggest = max(pending, key=lambda kv: kv[1])[0] if pending else None
            phrase = ("downloading %s (%s)" % (biggest, elapsed) if biggest
                      else "downloading (%s)" % elapsed)
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


# --------------------------------------------------------------------------
# In-flight bytes inside ONE package, off a pty (Item 3 of the streaming
# feature, dropped once and revived here for a reason worth stating).
#
# Package-level progress (above) still leaves a venv dominated by one huge
# wheel — the ROCm/CUDA torch install this whole feature shipped to fix —
# reporting nothing for however long that single file takes, because there
# is no SECOND package landing to move the aggregate. Measuring uv's own
# cache-directory growth for that was investigated once already and
# dropped: it tracks UNPACKED size, several times the compressed transfer,
# with no single file to stat as "bytes so far" (see `_UvProgress`'s
# docstring for that finding, still true and still why this is not built on
# the cache directory).
#
# What changed is where to look instead. uv only prints per-package IN-FLIGHT
# bytes when it believes stdout is a terminal — pointed at a pipe it never
# announces anything between "Downloading" and "Downloaded" for one file, but
# under a pty (confirmed empirically: `pty.openpty()` + a real `uv sync`,
# transcript captured with per-chunk timestamps) it redraws a live multi-line
# bar, ANSI escapes and all, e.g. — after stripping the escapes:
#
#   Preparing packages... (0/2)  numpy   ------------------------------  6.31 MiB/15.94 MiB
#                                 scipy   ------------------------------  6.14 MiB/33.68 MiB
#
# — refreshed every few milliseconds while the download runs, which is
# exactly the missing signal. uv prints NO "Downloading"/"Downloaded" lines
# at all once it is on a tty (also confirmed empirically) — the live bar
# replaces them, it does not supplement them — so `_PtyProgressReader` below
# feeds the SAME `_UvProgress` through a second entry point
# (`feed_pty_progress`) rather than a parallel one, and the plain-line parser
# (`feed`) stays exactly as built: it is what still catches
# `Resolved`/`Prepared`/`Installed`, which uv prints identically either way.
#
# **The separator between a redraw and a PERMANENT line is a CR, not an
# LF.** A real capture, replayed byte-for-byte, is instructive here: 31,631
# bytes for a two-package sync carried 277 carriage returns and only 7
# newlines. uv's actual shape is one CR-separated frame after another,
# with a permanent line reached by one more CR before the LF that finally
# ends it. `_PtyProgressReader` therefore splits on the CHARACTER CLASS
# (either separator), not on LF alone — the bug that shipped first split
# only on LF, so the reconstructed "line" was the entire multi-KB redraw
# history glued to whatever permanent text followed it, which none of the
# anchored regexes below ever matched: `phase` stuck at `downloading`
# forever, confirmed by replaying the capture.
#
# POSIX only, and optional even there. `pty` does not exist on Windows —
# guarded at import time above — and pty SETUP (not the sync itself) can fail
# on a POSIX box with no `/dev/ptmx` available (a locked-down container,
# some CI sandboxes). Either way this must never be the reason an install
# fails: `_PtyUnavailable` is raised only for a failure BEFORE `uv sync` is
# spawned, so `_build` can fall back to the ORIGINAL, already-tested pipe
# path with no risk of running uv twice.
class _PtyUnavailable(Exception):
    """A pty could not be set up for this sync. Raised only before the uv
    child is spawned — see the module comment above `_PtyUnavailable`."""


#: Strips ANSI CSI sequences (`ESC [ ... letter`) — cursor movement
#: (`\x1b[1A`), line-clear (`\x1b[2K`) and colour (`\x1b[36m`) are all this
#: shape in uv's own output; confirmed against a real transcript rather than
#: assumed. uv is not observed to use any other escape family (OSC, DCS), so
#: this is scoped to what was actually seen, not to "ANSI in general".
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: uv's own frame/line separator, RAW BYTES, before any decoding: a bare
#: carriage return between live redraws, CR-then-LF before a permanent one
#: (see the module comment above `_PtyUnavailable`). Splitting BYTES, not
#: decoded text — see `_PtyProgressReader.feed_bytes` for why decoding has
#: to happen AFTER this split, not before it. Splitting on the CHARACTER
#: CLASS, not on the two bytes as one token, because consecutive redraws
#: are CR-only with no LF at all — treating CRLF as one inseparable unit
#: would still glue every redraw frame onto the permanent line that finally
#: follows it, which is the exact bug this replaced.
_PTY_LINE_SPLIT_RE = re.compile(rb"[\r\n]")

#: One package's live progress row, after `_ANSI_RE` has stripped the escapes
#: around it: `<name>` padded with spaces, a dash-filled bar of variable
#: width, then `<done> <unit>/<total> <unit>` — WITH a space between the
#: number and unit here (unlike the piped `Downloading name (15.9MiB)` form,
#: which has none; both are handled, `_DOWNLOADING_RE`'s pattern tolerates
#: either). `{5,}` on the bar run is deliberately loose (dashes, spaces, or a
#: fill character a future uv version might use) rather than pinned to
#: exactly what was observed, since the bar's APPEARANCE is not the signal —
#: the trailing numbers are.
_PTY_PROGRESS_RE = re.compile(
    r"(\S+)[ \t]+[-#\s]{5,}([\d.]+)\s*(B|KiB|MiB|GiB)/([\d.]+)\s*(B|KiB|MiB|GiB)")

#: Text markers of a live-redraw fragment that carries no per-package row of
#: its own (yet) — the "Preparing packages... (n/m)" header before any row
#: has appeared, the "Resolving dependencies..." spinner that precedes it,
#: and "Installing wheels..." — the header of the DIFFERENT bar uv draws
#: while linking (see `_PTY_INSTALL_ROW_RE` below for its per-package row).
#: A fragment matching any of these — OR `_PTY_PROGRESS_RE`, OR
#: `_PTY_SPINNER_RE`, OR `_PTY_INSTALL_ROW_RE` — is chrome: kept OUT of the
#: ring `_build` raises on failure with (SPEC PY-18), because it is not uv's
#: diagnosis, it is the redraw ahead of it. A REAL capture of a resolver
#: failure measured a 1,303-character spinner/redraw prefix ahead of the
#: actual "No solution found" text — exactly what this exists to strip
#: before that text ever reaches a `RuntimeError`.
_PTY_CHROME_MARKERS = ("Preparing packages...", "Resolving dependencies...",
                       "Installing wheels...")

#: A live-redraw fragment starts with one of uv's spinner glyphs (the
#: Braille Patterns Unicode block) in every capture taken so far — the
#: resolving spinner, the per-package "name==version" resolver status, and
#: the DOWNLOAD bar all begin this way, while every PERMANENT line
#: (`Resolved`/`Prepared`/`Installed`, uv's own error text) does not. Used
#: as an independent chrome signal alongside the others — belt and
#: suspenders, because a locale or terminal this was not captured against
#: could plausibly pick a different (ASCII) spinner character set, and a
#: false NEGATIVE here (a chrome fragment let through to the ring) is only
#: noise, while a false POSITIVE (real diagnosis text mistaken for chrome)
#: is silent data loss — so nothing is excluded from the ring on this
#: signal alone unless the others also miss it, biasing toward keeping
#: unrecognised text rather than discarding it.
_PTY_SPINNER_RE = re.compile("^[⠀-⣿]")

#: The INSTALL-phase bar's per-package row — a different shape than the
#: download bar entirely, block glyphs and a `[n/m]` counter rather than a
#: spinner and a byte-size row: `'░░░░░░░░░░░░░░░░░░░░ [7/30]
#: markdown-it-py==4.2.0'`, observed verbatim in a real capture. Missed by
#: every OTHER chrome signal (`_PTY_PROGRESS_RE` needs a byte-size row that
#: this shape does not have; it has no spinner glyph; "Installing wheels..."
#: is its header, not its per-row text) — measured letting 24 of 55 "genuine"
#: ring lines through from a link phase that took 5ms, which for the
#: multi-GB torch case (a minutes-long link, hundreds of these rows, against
#: `_STDERR_RING_LINES` = 400) is exactly the flooding the classifier exists
#: to prevent, on the failures a real Permission/disk-space error would
#: raise from that phase.
_PTY_INSTALL_ROW_RE = re.compile(r"^[░█\s]*\[\d+/\d+\]")

#: Bound on the RAW BYTES kept between reads while looking for a split
#: point. Now that a split point is a CR OR an LF (see
#: `_PTY_LINE_SPLIT_RE`), the carry is ordinarily short — just the
#: still-arriving tail of the CURRENT frame — but the cap stays as a
#: defensive bound against a pathological stretch of output with no
#: separator at all for an extended run. Only the tail matters if it is
#: ever hit: a value already extracted is in the tracker via its
#: max()-based aggregation (`_UvProgress.feed_pty_progress`), and a row cut
#: off at the cut point reappears whole on the very next frame,
#: milliseconds later. Never actually reached against a real capture (the
#: largest single frame measured was 1,920 characters, uv's own 80x24
#: fallback-terminal-size ceiling before `_run_uv_via_pty` started setting
#: a real winsize).
_PTY_CARRY_MAX = 8192

#: How close to its own announced total a package's LAST-SEEN in-flight
#: reading has to be before a name vanishing from the live display is
#: trusted as "finished" (`_UvProgress.feed_pty_frame`). Exists because a
#: displaced-not-finished row looks IDENTICAL to a finished one from here —
#: both simply stop appearing — and `_run_uv_via_pty` now sets a tall
#: winsize specifically so uv never has to clamp the bar and displace a row
#: (see that function), but this is the second layer that still catches a
#: displacement if that assumption ever breaks (a future uv raising its
#: concurrency past what the winsize covers, a platform that ignores
#: `TIOCSWINSZ`). Measured on a real 149-package capture: every GENUINELY
#: finished row's last reading was at least this close to its total; the 44
#: WRONGLY-vanished ones (some clamped out at 0 B) were all under 90%.
_PTY_NEAR_TOTAL_FRACTION = 0.90


class _PtyProgressReader:
    """Feeds one `_UvProgress` from a pty's raw output: split on a CR OR an
    LF into uv's own frames/lines (see the module comment for why the CR
    matters), THEN decoded and ANSI-stripped, each one scanned for
    `_PTY_PROGRESS_RE`'s in-flight byte rows and handed to the ORIGINAL
    line-based parser (`feed`).

    **The split happens on RAW BYTES, before decoding or ANSI-stripping —
    not the other way around.** An earlier version decoded and stripped
    PER READ, then joined the result with the text carry: `text =
    _ANSI_RE.sub("", raw.decode(...)); buf = self._carry + text`. Both a
    UTF-8 multi-byte character and an ANSI escape sequence can straddle a
    `read()` boundary — a pty's kernel buffer is only ~4KiB, so a read
    truncates there whenever uv outruns the reader — and decoding/stripping
    before the halves are ever rejoined leaves the torn piece unfixable: a
    replayed real failure capture at 4KiB reads left 11 bare ESC bytes
    sitting in decoded text that `_ANSI_RE` could no longer recognise as an
    escape (its other half was in the PREVIOUS read, already stripped), and
    one of those leaked straight into the ring as `'\\x1b[1B'` between a
    permanent line and uv's own diagnosis. Splitting on the raw bytes first
    is safe because neither a CR nor an LF byte value can occur INSIDE a
    valid ANSI CSI sequence or a UTF-8 continuation byte (both are
    restricted byte ranges that exclude 0x0D/0x0A) — so accumulating raw
    bytes until a real separator appears, and decoding only once a
    complete, separator-delimited fragment exists, cannot tear either one.

    Only GENUINE, permanent lines are returned for the caller's ring buffer
    (SPEC PY-18's verbatim-error contract) — a live-redraw fragment (a
    download-bar frame, an install-bar frame, either spinner) is chrome,
    identified inline below, and must never reach it: a bounded ring
    flooded with near-duplicate redraws is how a real resolver failure's
    own diagnosis gets pushed out by
    `_STDERR_RING_LINES`, or arrives mid-way through a single multi-KB
    "line" that is mostly bar frames — both measured against real captures
    before this fix.
    """

    def __init__(self, tracker):
        self._tracker = tracker
        self._carry = b""

    def feed_bytes(self, raw):
        """One read()'s worth of raw pty bytes. Returns the complete,
        GENUINE (non-chrome) lines found, for the caller to append to its
        ring."""
        buf = self._carry + raw
        *complete, self._carry = _PTY_LINE_SPLIT_RE.split(buf)
        lines = []
        for frag_bytes in complete:
            if not frag_bytes:
                continue
            # Decoded and ANSI-stripped ONLY now that `frag_bytes` is known
            # complete (bounded by two real separators) — see the class
            # docstring for why this order is the fix.
            frag = _ANSI_RE.sub("", frag_bytes.decode("utf-8", errors="replace"))
            if not frag:
                continue
            matches = list(_PTY_PROGRESS_RE.finditer(frag))
            names_here = set()
            for m in matches:
                name, done_v, done_u, total_v, total_u = m.groups()
                names_here.add(name)
                self._tracker.feed_pty_progress(
                    name,
                    float(done_v) * _UNIT_BYTES[done_u],
                    float(total_v) * _UNIT_BYTES[total_u],
                )
            is_chrome = bool(matches) or bool(_PTY_SPINNER_RE.match(frag)) or \
                bool(_PTY_INSTALL_ROW_RE.match(frag)) or \
                any(marker in frag for marker in _PTY_CHROME_MARKERS)
            if is_chrome:
                # Part of the live "Preparing packages..." block — tell the
                # tracker what it showed, so a name that VANISHES between
                # this frame and the next (uv stops redrawing a row the
                # instant it lands; it confirms nothing under a tty the way
                # a pipe's `Downloaded` line does) is recognised as finished.
                # See `_UvProgress.feed_pty_frame`.
                self._tracker.feed_pty_frame(names_here)
            self._tracker.feed(frag)
            if not is_chrome:
                lines.append(frag)
        if len(self._carry) > _PTY_CARRY_MAX:
            # See the constant's own comment: only the tail can still become
            # a real line, and a stale prefix has already been mined for
            # whatever progress rows it held. Truncating raw bytes can cut a
            # multi-byte UTF-8 character at the boundary; `errors="replace"`
            # above turns that into one U+FFFD, not a crash.
            self._carry = self._carry[-_PTY_CARRY_MAX:]
        return lines


def _run_uv_piped(cmd, cwd, env, tracker):
    """`uv sync` behind an ordinary pipe: the ORIGINAL streaming path (before
    the pty), kept byte-for-byte as the fallback every platform still gets
    when a pty is unavailable or unsupported (Windows, always; a POSIX
    sandbox with no `/dev/ptmx`). Returns `(returncode, ring)`.

    `stdout=DEVNULL, stderr=PIPE`: uv's own progress text goes to STDERR
    (confirmed by probe), so stdout has nothing this needs and piping it too
    would only be a second buffer to drain. Streamed line by line rather
    than read all at once so a heartbeat elsewhere can see `tracker`'s state
    WHILE uv is still running.

    `with Popen(...)` PLUS an explicit `kill()` on any exception — the same
    two-part discipline `subprocess.run` itself uses internally (its source
    is `with Popen(...) as process: try: ... except: process.kill(); raise`).
    The `with` alone is not enough: `Popen.__exit__` only closes the pipes
    and calls `wait()`, it does NOT kill.
    """
    ring = collections.deque(maxlen=_STDERR_RING_LINES)
    # close_fds=False for posix_spawn rather than fork()+exec — the same
    # discipline every other spawn in this codebase follows; see
    # `_acquire_python` above.
    with subprocess.Popen(cmd, cwd=cwd, env=env,
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
    return proc.returncode, ring


#: Rows/columns set on the pty before uv is spawned — tall enough that uv
#: never has to clamp its live bar and displace a row out of view. See
#: `_UvProgress.feed_pty_frame`'s docstring for what happens when it does:
#: a real 149-package capture measured 44 names wrongly confirmed
#: "downloaded" because they had simply scrolled off screen while still
#: transferring. `pty.openpty()` sets NO winsize at all, which is why uv
#: fell back to its own 80x24 default in every capture taken before this
#: fix — every single frame measured exactly 1920 = 24*80 characters — while
#: downloading up to 50 wheels concurrently. 200 rows is comfortably above
#: that concurrency ceiling, with margin for a future uv raising it further;
#: the width matters less (a name plus its byte row fits well inside 80
#: columns) but costs nothing to widen too, since a virtual terminal size
#: is two integers, not a real allocation.
_PTY_WINSIZE_ROWS = 200
_PTY_WINSIZE_COLS = 200


def _set_pty_winsize(fd):
    """Best-effort: tell the pty at *fd* it is `_PTY_WINSIZE_ROWS` x
    `_PTY_WINSIZE_COLS` — see those constants for why. Never raises: a
    `TIOCSWINSZ` failure (an exotic platform, a non-tty fd) is not worth
    failing an install over. This is the FIRST layer against uv clamping
    its bar; `_UvProgress.feed_pty_frame`'s near-total-fraction check is
    the second, and stays in place regardless of whether this one works —
    belt and suspenders, the same discipline `_PTY_SPINNER_RE`'s own
    comment argues for.
    """
    try:
        winsize = struct.pack("HHHH", _PTY_WINSIZE_ROWS, _PTY_WINSIZE_COLS, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


def _run_uv_via_pty(cmd, cwd, env, tracker):
    """`uv sync` under a pty, so it prints the live in-flight bytes it
    suppresses for a pipe (see the module comment above `_PtyUnavailable`).
    Returns `(returncode, ring)` — same shape as `_run_uv_piped`, so `_build`
    treats the two identically once either has returned.

    Raises `_PtyUnavailable` only for a SETUP failure before `uv sync` is
    spawned (`pty.openpty()` itself failing — no `/dev/ptmx`, an exhausted
    pty allocation), which is what makes falling back to `_run_uv_piped`
    always safe: the child never started, so there is no risk of running uv
    twice. Once the child IS running, any exception here is genuine (kill,
    WAIT — matching `_run_uv_piped`'s own discipline, since `kill()` alone
    leaves the reap to nobody while `install()` writes the error record —
    then re-raise). It does NOT fall back, because a second `uv sync`
    racing the first one's still-live process would be worse than the
    original failure.

    `stdin=subprocess.DEVNULL`, not the slave fd: uv's bar keys off
    stdout/stderr, so stdin being a real terminal buys this feature nothing.
    What it WOULD cost: anything in the sync that reads stdin (a prompt uv
    decides to show, a subprocess of its own) blocks forever, because
    nothing on our side ever writes to the master — and neither this loop's
    `poll()` nor `proc.wait()` below carries a deadline that would notice.
    `DEVNULL` makes such a read fail immediately, exactly as it did before
    this feature (a plain pipe with `stdin=DEVNULL`) — a behaviour change
    here would be a regression nothing about the pty NEEDS.

    `close_fds=False`, matching every other spawn in this file (see
    `_acquire_python`): the child inherits an extra, unused copy of
    `master_fd` as a result, which is harmless — it is closed the moment the
    child exits — and consistency here is worth more than trimming one
    inherited descriptor `close_fds=True` would also cost the posix_spawn
    path on a system without `POSIX_SPAWN_CLOSEFROM`.

    `select.poll()`, not `select.select()`: `select()` raises `ValueError`
    for a watched fd at or above `FD_SETSIZE` (1024) — reachable here, since
    this worker inherits whatever fds the server process already had open
    (progress files, sockets) and a pty adds two more on top. `poll()` has
    no such ceiling. This is not a defensive nicety: the ORIGINAL version of
    this loop treated that `ValueError` (and any `OSError`) as "stop
    watching" and broke out silently, still WAITING TO BE FIXED at the time
    of writing — `master_fd` closes underneath a live `uv sync`, its next
    write fails EIO, it dies, and `_build` raises "Failed to build the
    environment for <proj>:\n" with an EMPTY ring: a failure with no
    diagnosis at all, the worst possible outcome for PY-18.
    """
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError as e:
        raise _PtyUnavailable(str(e)) from e
    # BEFORE the spawn, so uv's own startup terminal-size query already
    # sees it — see `_set_pty_winsize`/`_PTY_WINSIZE_ROWS`.
    _set_pty_winsize(slave_fd)
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                                stdin=subprocess.DEVNULL,
                                stdout=slave_fd, stderr=slave_fd,
                                close_fds=False)
    except BaseException:
        os.close(master_fd)
        os.close(slave_fd)
        raise
    # Only the CHILD needs the slave end from here — holding our own copy
    # open would stop us ever seeing EOF on `master_fd`, because a pty only
    # signals "the other side is gone" once EVERY reference to the slave is
    # closed, ours included.
    os.close(slave_fd)
    reader = _PtyProgressReader(tracker)
    ring = collections.deque(maxlen=_STDERR_RING_LINES)
    poller = select.poll()
    poller.register(master_fd, select.POLLIN)

    def _drain_once():
        """One NON-BLOCKING read, if anything is ready right now — used to
        pick up whatever uv wrote in the same instant it exited, which a
        bare `break` on `proc.poll()` would otherwise drop on the floor."""
        if not poller.poll(0):
            return b""
        try:
            return os.read(master_fd, 65536)
        except OSError:
            return b""

    try:
        while True:
            events = poller.poll(500)  # milliseconds
            if events:
                try:
                    chunk = os.read(master_fd, 65536)
                except OSError:
                    # EIO is how a pty master reports "every copy of the
                    # slave end is closed" on Linux — the ordinary, expected
                    # end of output, not a real error.
                    chunk = b""
                if chunk:
                    for line in reader.feed_bytes(chunk):
                        ring.append(line)
                    continue
                break  # empty read or EIO: nothing more will ever arrive
            if proc.poll() is not None:
                # The child exited between two polls with nothing flagged
                # ready — the ordinary path once uv is done. One more drain
                # first (see `_drain_once`): uv can write its last line and
                # exit in the same instant this poll's own 500ms timed out,
                # and breaking immediately would lose exactly that.
                chunk = _drain_once()
                if chunk:
                    for line in reader.feed_bytes(chunk):
                        ring.append(line)
                break
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        os.close(master_fd)
    proc.wait()
    return proc.returncode, ring



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


def _build(project_dir, venv_dir, uv_cache_dir, python_executable, tracker=None,
           allow_build=False):
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
      UV_CACHE_DIR            set ONLY when `uv_cache_dir` is not None — which is
                              only when the caller asked for isolation
                              (`FUSED_RENDER_HOME`; see `projectenv.uv_cache_dir()`).
                              Ordinarily left UNSET, deferring to uv's own default
                              cache rather than a sibling of the venv store: an
                              explicit sibling used to be unconditional and
                              fragmented per branch/worktree as a result — the
                              trade `uv_cache_dir()` documents and accepts.

    `UV_LINK_MODE` is deliberately NOT set either way: uv already prefers
    hardlinks and degrades on its own, and pinning it here would override a
    user who had a reason to choose otherwise.

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

    `allow_build=False` (the default) adds `--no-build`, which fails resolution
    outright rather than letting uv fall back to a source build for a
    dependency with no matching wheel. Static detection
    (`projectenv.nonstandard_dependencies_of`) can only read the manifest — it
    has no way to know a plain `foo>=1.0` happens to publish no wheel for this
    platform until uv actually tries to resolve it, and a source build runs
    that package's OWN `build-system` backend with no consent asked for it.
    `--no-build` turns that into a clean resolver error the client can catch
    and re-offer as an explicit "install anyway" (`runtime.js`'s retry to
    `/api/env/install` with `allow_build: true`), rather than a build silently
    proceeding with no consent asked for it.

    `--no-install-project` rides along with `--no-build`, not on its own: uv's
    `--no-build` forbids building ANY sdist-only distribution, and the LOCAL
    PROJECT ITSELF is one such distribution the instant it declares
    `[build-system]` at all — which a bare `uv init` scaffold does by default
    (`uv_build`), with zero dependencies of its own. Without
    `--no-install-project`, `--no-build` therefore fails EVERY such folder
    outright ("can't be installed because it is marked as `--no-build` but has
    no binary distribution", naming the project itself, not a dependency), and
    that failure's wording does not match `NO_BUILD_HINT` in runtime.js, so no
    "Install anyway" retry is ever offered — the folder is permanently stuck.
    Verified against real `uv 0.12.5`: a fresh `uv init` folder fails on
    `uv sync --no-default-groups --no-build` alone, and succeeds with
    `--no-install-project` added; a folder with a genuinely wheel-less
    dependency (`uwsgi`) still gets refused, and the refusal still carries the
    `hint: Wheels are required for ...` line `NO_BUILD_HINT` matches, so the
    retry path is unaffected.

    The trade-off this accepts: the project's own package is no longer
    installed editable into the venv, so a SRC-LAYOUT folder whose scripts
    `import mypkg` (relying on that editable install to put `mypkg` on
    `sys.path`) would lose that import. Flat-layout script folders — the norm
    here, and what PY-16 describes — are unaffected: Python already puts a
    script's own directory on `sys.path`, with no editable install involved at
    all. A src-layout project that wants its own package importable would need
    to either declare it as an ordinary dependency (so uv builds *a* wheel for
    it, not skip the build) or run `uv sync` by hand outside this app.
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
    if not allow_build:
        # See the docstring above for why these two are never split: `--no-build`
        # alone refuses to build the LOCAL PROJECT too, bricking any folder with
        # a `[build-system]` table (every `uv init` scaffold) and zero
        # dependencies of its own.
        cmd.append("--no-build")
        cmd.append("--no-install-project")

    # `_uv_env` scrubs PYTHON* and VIRTUAL_ENV: without the first, every
    # dependency uv has to BUILD rather than download as a wheel failed inside
    # the packaged macOS app (D266); without the second, uv warns and can target
    # the server's own venv.
    env = _uv_env(UV_PROJECT_ENVIRONMENT=venv_dir)
    if uv_cache_dir:
        # ONLY when the caller asked for an isolated cache
        # (`envinstall._spawn` passes `projectenv.uv_cache_dir()`, which is
        # non-None only under `FUSED_RENDER_HOME`). Otherwise `UV_CACHE_DIR`
        # is left OUT of `env` entirely — not set to an empty string, which
        # uv would treat as a real, nonsensical path — so uv resolves
        # whatever it would for any other command a user ran: an AMBIENT
        # `UV_CACHE_DIR` already in this process's environment (`_uv_env`'s
        # base is a plain `dict(os.environ)` copy, so it is never stripped —
        # CI's own `setup-uv` action exports one), or failing that its own
        # platform default (XDG on Linux, `~/.cache/uv` on macOS too,
        # `%LOCALAPPDATA%` on Windows). See `projectenv.uv_cache_dir()` for
        # why an explicit sibling of the venv store is no longer the
        # unconditional choice: it made cache-target hardlinking work BY
        # CONSTRUCTION, and fragmented the cache per branch/worktree as an
        # unintended side effect of doing so.
        os.makedirs(uv_cache_dir, exist_ok=True)
        env["UV_CACHE_DIR"] = uv_cache_dir

    os.makedirs(os.path.dirname(venv_dir), exist_ok=True)
    # After the makedirs above (the mirror is a SIBLING of the venv, so its parent
    # is the one they create) and after the unmarked-venv removal (which must not
    # be able to take the mirror's lock with it).
    sync_root = _sync_root(project_dir, venv_dir)
    if tracker is None:
        tracker = _UvProgress()  # nobody is watching; feed it anyway for one code path

    # Pty first, pipe as the fallback — POSIX only, and only when the pty
    # module actually loaded (it does not exist on Windows) and its SETUP
    # succeeds (see `_run_uv_via_pty`'s docstring for what "setup" means and
    # why a failure there, and only there, is safe to fall back from). Every
    # other platform/failure keeps running exactly the path this feature
    # shipped with — `_run_uv_piped` is that same code, unchanged, just
    # pulled into its own function so this call site could pick either.
    if pty is not None and os.name != "nt":
        try:
            returncode, ring = _run_uv_via_pty(cmd, sync_root, env, tracker)
        except _PtyUnavailable:
            returncode, ring = _run_uv_piped(cmd, sync_root, env, tracker)
    else:
        returncode, ring = _run_uv_piped(cmd, sync_root, env, tracker)

    if returncode != 0:
        # Verbatim: uv's own text names the real problem (no wheel for this
        # platform, a bad pin, no network, a lock that no longer matches the
        # manifest), and that is the answer the user needs. The ring holds the
        # TAIL of stderr rather than all of it (see `_STDERR_RING_LINES`),
        # which is the part that matters for a failure — uv prints its
        # diagnosis right before exiting, not at the top of a long resolve.
        # Under a pty, `_PtyProgressReader` splits on a CR OR an LF (uv's
        # own frame/line separator — see the module comment above
        # `_PtyUnavailable`) and filters out whatever is chrome (a bar
        # frame, the resolving spinner) before a fragment ever reaches this
        # ring, so a resolver failure's own text — confirmed against a real
        # one — reaches here exactly as readable as it always was, merged
        # stdout+stderr or not.
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


# The resolver's OWN wording for "this can only be satisfied by building from
# source", raised by `_build` as a `RuntimeError` when `allow_build=False`
# (the default) refuses uv permission to do so. Detected HERE, in the worker
# that actually knows `--no-build` was passed, rather than by the client
# regexing uv's stderr after the fact (that used to live in runtime.js's
# `NO_BUILD_HINT` — moved here so there is one detector, not two that can
# disagree). See `_build`'s own `--no-build` comment for why the flag exists;
# the two wordings below are transcribed with their reasoning intact.
#
# Anchored on the hint line rather than the "Because … has no usable wheels"
# line above it: the hint is the one sentence uv writes FOR THIS SITUATION
# SPECIFICALLY (it names `--no-build` itself), where the other line's wording
# changes shape between "all versions of X" and "X==1.2.3" depending on
# whether the requirement carries a pin — one pattern to keep in sync with
# uv's output instead of several.
#
# Verified against a real `uv sync --no-build` failure (uv 0.12.5):
#   hint: Wheels are required for `uwsgi` because building from source is
#   disabled for all packages (i.e., with `--no-build`)
_NO_BUILD_HINT = re.compile(
    r"hint: Wheels are required for `([^`]+)` because building from source is disabled")

# The SAME refusal, in a different shape, when a `uv.lock` already exists.
# `_NO_BUILD_HINT` matches only the RESOLUTION-time `hint:` line, which uv
# prints while it is still resolving the dependency graph — but `_sync_root`
# deliberately preserves `uv.lock` across builds, and a folder gains one just
# by being run once. Once a lock exists, resolution succeeds against it and
# the refusal happens later, at INSTALL time, with no hint line at all:
#
#   error: Distribution `uwsgi==2.0.31 @ registry+https://pypi.org/simple`
#   can't be installed because it is marked as `--no-build` but has no
#   binary distribution
#
# Verified against real uv 0.12.5 (`uv lock && uv sync --no-default-groups
# --no-build --no-install-project`). The package name sits before the
# `==version @ source` — `[^`=]+` stops at the first `=` so a name is never
# captured with its pin attached. The second group captures everything
# between `==` and the closing backtick — uv writes `X==1.2.3 @
# registry+https://…` inside them, so `_no_build_pinned_version` (below)
# still has to trim the ` @ source` suffix off itself; the hint wording
# never carries a pin at all, since it fires during resolution, before uv
# has settled on one version to try.
_NO_BUILD_DISTRIBUTION = re.compile(
    r"Distribution `([^`=]+)==([^`]*)` can't be installed because it is marked as `--no-build`")


def _no_build_package(message):
    """The bare package name uv refused to build from source, or None.

    None both when `message` does not match either wording at all (a plain
    resolver failure — a bad pin, no network, a genuinely nonexistent
    package — must fall through to the ordinary error path unchanged, not be
    swallowed into a retry prompt that names nothing) and when the caller
    already allowed builds (checked by the caller, not here, since this
    function only knows about text — `allow_build=True` should never even
    have produced this failure, but a caller must not go looking for it).
    """
    if not isinstance(message, str):
        return None
    hint = _NO_BUILD_HINT.search(message)
    if hint:
        return hint[1]
    dist = _NO_BUILD_DISTRIBUTION.search(message)
    return dist[1] if dist else None


def _no_build_pinned_version(message):
    """The exact version uv refused to build, when the refusal came from the
    install-time `_NO_BUILD_DISTRIBUTION` wording (a `uv.lock` already pins
    one). None for the resolution-time `hint:` wording, which names no
    version at all, and None whenever `_no_build_package` itself would —
    callers only ever call this once that already matched.
    """
    if not isinstance(message, str):
        return None
    dist = _NO_BUILD_DISTRIBUTION.search(message)
    if not dist:
        return None
    return dist[2].split()[0] if dist[2].split() else None


# Dogfooding on Linux surfaced a refusal `_no_build_package` correctly
# detects but that the "Install anyway" prompt cannot honestly offer:
# `pyobjc-framework-applicationservices`, a macOS-only wheel set with no
# `sys_platform` marker in the manifest to have warned about it earlier.
# Compiling it here needs Objective-C frameworks this machine will never
# have — the prompt is not a real choice, only a guaranteed multi-minute
# wait ending in the same failure. uv's own refusal text cannot tell the two
# cases apart (`_NO_BUILD_HINT`/`_NO_BUILD_DISTRIBUTION` are byte-identical
# for "no wheels exist" and "wheels exist, but not for this platform"), so
# this looks the fact up independently, on PyPI.
#
# Modelled on `ai/hub_metadata.py`'s `_fetch_raw` rather than
# `update/common.py`'s manifest fetch: the update path is security-critical
# and correctly fails CLOSED (raises, refuses to trust an unverifiable
# manifest) — but a platform-incompatibility check that cannot complete must
# fail OPEN, to the ordinary compile prompt, exactly like `hub_metadata`
# already does for its own best-effort catalog enrichment. Same shape here:
# one broad exception tuple, a short timeout, a byte cap, no exception ever
# escapes this function.
_PYPI_TIMEOUT_S = 3.0
_PYPI_MAX_BYTES = 2 * 1024 * 1024
_PYPI_PROJECT_URL = "https://pypi.org/pypi/{name}/json"
_PYPI_RELEASE_URL = "https://pypi.org/pypi/{name}/{version}/json"
_PYPI_UNREACHABLE = (urllib.error.URLError, OSError, ValueError, TimeoutError)


def _fetch_pypi_json(name, version):
    """The PyPI JSON body for `name`==`version` (`_NO_BUILD_DISTRIBUTION` case),
    or for the project's latest release when `version` is None
    (`_NO_BUILD_HINT` gives no version at all) — or None on ANY failure:
    offline, unreachable, a timeout, a 404 for a version PyPI never
    published, a non-JSON body, anything. This is the one network seam
    `_incompatible_platform_name` calls through, and the only one this
    module has — kept to a single injectable function so tests can cover
    every branch (wheels-elsewhere-only, no-wheels, timeout, malformed JSON,
    404) by swapping this one thing, without a socket.
    """
    url = (_PYPI_RELEASE_URL.format(name=urllib.parse.quote(name),
                                     version=urllib.parse.quote(version))
           if version else _PYPI_PROJECT_URL.format(name=urllib.parse.quote(name)))
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=_PYPI_TIMEOUT_S) as response:
            raw = response.read(_PYPI_MAX_BYTES + 1)
        if len(raw) > _PYPI_MAX_BYTES:
            return None
        return json.loads(raw)
    except _PYPI_UNREACHABLE:
        return None


def _wheel_platform_tags(filename):
    """The platform compatibility tag(s) a `.whl` filename declares (PEP 427:
    `{name}-{version}(-{build})?-{python}-{abi}-{platform}.whl`) — a wheel
    naming multiple compatible platform tags dot-joins them in that last
    field, e.g. `macosx_10_9_x86_64.macosx_11_0_arm64`, so this returns a
    set. None when `filename` is not shaped like a wheel at all: no `.whl`
    suffix, or fewer than the five dash-separated fields PEP 427 always
    writes (name/version themselves never contain a dash — the spec
    requires `-` be normalized to `_` in both — so the LAST field is always
    the platform tag regardless of how many dashes precede it).
    """
    if not filename.endswith(".whl"):
        return None
    parts = filename[: -len(".whl")].split("-")
    if len(parts) < 5:
        return None
    return set(parts[-1].split("."))


# A tag's PLATFORM FAMILY, not its exact tag — `macosx_10_9_universal2` and
# `macosx_11_0_arm64` are both just "macOS" for this purpose, and treating
# them as distinct would make two wheels for the same OS look like coverage
# of two different ones.
_PLATFORM_TAG_PREFIXES = (
    ("macosx", "macosx"),
    ("win", "win"),
    ("manylinux", "linux"),
    ("musllinux", "linux"),
    ("linux", "linux"),
)
_PLATFORM_DISPLAY_NAME = {"macosx": "macOS", "win": "Windows", "linux": "Linux"}
_CURRENT_PLATFORM_FAMILY = {"darwin": "macosx", "win32": "win", "linux": "linux"}.get(sys.platform)
_CURRENT_PLATFORM_NAME = _PLATFORM_DISPLAY_NAME.get(_CURRENT_PLATFORM_FAMILY, sys.platform)


def _platform_tag_family(tag):
    if tag == "any":
        return "any"  # a pure-python wheel: runs everywhere, no platform to name
    for prefix, family in _PLATFORM_TAG_PREFIXES:
        if tag.startswith(prefix):
            return family
    return None  # an unrecognized tag — never trusted to mean "not this platform"


def _incompatible_platform_name(name, version, *, fetch=None):
    """The human platform name (e.g. `"macOS"`) when PyPI publishes wheels
    for `name`[`==version`] but NONE of them can ever run on this machine —
    or None, which callers MUST treat as "fall back to the ordinary compile
    prompt", in every one of these cases:

    - no wheels at all (a source-only project — compiling is legitimate);
    - a wheel exists that DOES cover this platform, or is pure-python
      (`any`), or carries a tag this function does not recognize — the last
      one on purpose: a future platform tag this code has never seen is not
      evidence the current platform is unsupported, only that this function
      is not yet current;
    - the lookup itself could not reach a confident answer at all (`fetch`
      returned None — see `_fetch_pypi_json`'s own docstring for the full
      list of ways that happens).

    Deriving the name from the tags actually found, never from a hardcoded
    package list: this must work for the next macOS-only (or Windows-only,
    or Linux-only) package nobody has heard of yet, not only the one that
    was dogfooded.

    `fetch` defaults to `_fetch_pypi_json`; tests inject a fake so every
    branch is covered without a socket.
    """
    fetch = fetch or _fetch_pypi_json
    payload = fetch(name, version)
    if not isinstance(payload, dict):
        return None
    urls = payload.get("urls")
    if not isinstance(urls, list) or not urls:
        return None
    families = set()
    for entry in urls:
        if not isinstance(entry, dict) or entry.get("packagetype") != "bdist_wheel":
            continue
        filename = entry.get("filename")
        if not isinstance(filename, str):
            continue
        tags = _wheel_platform_tags(filename)
        if tags:
            families.update(_platform_tag_family(tag) for tag in tags)
    if not families:
        return None
    if "any" in families or None in families or _CURRENT_PLATFORM_FAMILY in families:
        return None
    names = sorted({_PLATFORM_DISPLAY_NAME.get(family, family) for family in families},
                   key=str.casefold)
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


def install(key, progress_dir, project_dir, venv_dir, uv_cache_dir,
            python_executable=None, acquire_python=None, allow_build=False):
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
              activity=None, bytes_done=None, bytes_total=None, needs_build=None,
              platform_incompatible=None):
        with write_lock:
            if finished:
                return  # a terminal record is already on disk; nothing may follow it
            _write(progress_dir, stage, pct, detail, done, error,
                   activity=activity, bytes_done=bytes_done, bytes_total=bytes_total,
                   needs_build=needs_build, platform_incompatible=platform_incompatible)
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
            lambda: _build(project_dir, venv_dir, uv_cache_dir, python_executable, tracker,
                           allow_build=allow_build),
            progress=tracker,
        )
        write("done", 100, f"installed into {os.path.dirname(os.path.dirname(venv_python))}",
              done=True)
    except BaseException as e:  # noqa: BLE001
        # Verbatim: upstream's message already carries uv's/pip's stderr, which
        # names the real problem (a platform with no wheel, a bad pin, no
        # network). Only the exception class is prefixed, so the page can tell a
        # resolver failure from a disk-quota RuntimeError.
        message = f"{type(e).__name__}: {e}"
        # `allow_build` gates the lookup, not just the field: this run already
        # asked uv for permission to build from source, so an unrelated
        # RuntimeError that happens to mention "--no-build" in passing (there
        # is no such real case today, but the check costs nothing and removes
        # the possibility) must never be misread as the refusal this run
        # itself opted out of.
        needs_build = _no_build_package(str(e)) if not allow_build else None
        # A `--no-build` refusal splits further: is this platform ever going
        # to satisfy it, or is compiling a real (if slow, if risky) option?
        # Checked only once `needs_build` is already set — a plain resolver
        # failure never reaches this lookup at all — and the lookup itself
        # can never turn a genuine refusal into something else: it can only
        # downgrade `needs_build` to `platform_incompatible`, never invent
        # either from nothing.
        platform_incompatible = None
        if needs_build:
            incompatible = _incompatible_platform_name(
                needs_build, _no_build_pinned_version(str(e)))
            if incompatible:
                platform_incompatible = {
                    "package": needs_build,
                    "platform": incompatible,
                    "current_platform": _CURRENT_PLATFORM_NAME,
                }
                # Not a compile QUESTION any more — a platform FACT. Clearing
                # `needs_build` here (rather than leaving both fields set) is
                # what keeps this out of `envinstall._mirror_into_jobs`'s
                # "waiting for your approval" branch and out of runtime.js's
                # `confirmBuildRetry`: both key off `needs_build` alone, so a
                # refusal this platform can never satisfy must not carry it.
                needs_build = None
        write("error", 100, "", done=True, error=message, needs_build=needs_build,
              platform_incompatible=platform_incompatible)
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
    <python_executable> <acquire_python> <allow_build>`

    The empty string means None in the first THREE optional slots (argv cannot
    carry it): translated here and nowhere else, so `install` receives the real
    values. Read as the literal `""` instead, `uv_cache_dir` would hand `_build`
    a directory named nothing to create and point `UV_CACHE_DIR` at, and the
    `acquire_python` slot would have this worker try to download a Python
    version called nothing on every ordinary install.

    `allow_build` is not that idiom — argv already gives every slot a string,
    so there is no "missing" case to disambiguate — it is just truthy/falsy on
    the literal text: `"1"` for True, `""` for the (default) False.
    """
    _detach()
    if len(args) < 8:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    (key, progress_dir, project_dir, venv_dir, uv_cache_dir,
     python_executable, acquire_python, allow_build) = args[:8]
    install(key, progress_dir, project_dir, venv_dir, uv_cache_dir or None,
            python_executable or None, acquire_python=acquire_python or None,
            allow_build=bool(allow_build))


if __name__ == "__main__":
    main(sys.argv[1:])
