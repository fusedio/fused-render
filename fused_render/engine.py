"""Runs a Python file through the fused local compute backend, when installed.

Re-introduction of the D55-era engine (rolled back in D67) with a different
posture (D69): the fused engine is **optional**. When the `fused` package is
importable, /api/run executes code through `LocalPythonComputeBackend` —
fresh subprocess per call in a temp exec dir, params delivered via
`_params.json`. When it is not installed, the built-in executor
(`executor.py`/`_child.py`) runs unchanged. `available()` is the probe;
`server/common.py`'s `_forced_engine` picks per process.

Which interpreter a script gets is decided by the FOLDER it belongs to, not by
anything written in the file (SPEC PY-16; supersedes D172's per-file header):

  * **no `pyproject.toml` at the project root** -> the app's own python, no venv
    (`app_interpreter()`), so `[bundled]` + the core `dependencies` are there
    with nothing to install;
  * **a `pyproject.toml`** -> the project's own venv, built by `uv sync` and
    shared by every `.py` under that root. If it doesn't exist yet (or no longer
    matches the declaration), /api/run answers `needs_install` instead of
    blocking on the download — see `envinstall.py` (PY-18).

A `# /// script` header is not read, and not detected either — it is an ordinary
comment (D233). Nothing here inspects the source for one, so a file carrying a
leftover block runs exactly as it would without it.
`projectenv.project_env_for` owns the folder rule.

Code contract under this engine (the fused contract, plus a compat bridge):

  * a function decorated with ``@fused.udf`` — **any name**; the last decorated
    one is the entrypoint and receives params as raw JSON values (no
    annotation coercion: the calling JS owns types);
  * or a plain script that assigns ``result = ...``;
  * or — compat bridge, so pages and the built-in templates run identically
    under either engine — a bare ``main()``, called with the same
    annotation-driven string coercion the built-in executor applies.

Which of the three is live depends on WHO runs the script, and the difference is
load-bearing rather than academic:

  * **Here (local).** The epilogue below reads ``fused._registered_udfs``, and
    the real ``fused`` wheel has no such attribute — ``fused.udf(fn)`` returns a
    wrapper and registers nothing. Only the hosted backend's injected ``fused``
    shim module keeps that list. So locally the first branch never fires and the
    bare-``main()`` bridge is what actually runs, for every template.
  * **Hosted (an exported page).** ``export.py`` bundles each ``runPython``
    target and the ``fused`` wheel's hosting layer turns it into a served
    entrypoint; that runner resolves ``_registered_udfs`` or ``result`` and has
    **no bare-main fallback**, so a ``main()`` alone returns null there. That is
    why 22 template files carry a guarded ``fused.udf(main)`` shim: inert under
    this engine, and the only thing that makes them callable once deployed.

Neither the shim nor its absence is enforced anywhere, and it is currently
applied unevenly — see D179.

The wire shape returned here is the built-in executor's
``{ok, result, error: {type, message, traceback}, stdout}`` (plus additive
``stderr``/``duration_ms`` keys), so runtime.js and every template consume one
shape regardless of which engine ran the code.
"""
import asyncio
import importlib.util
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)

# A traceback frame header: `  File "<path>", line N[, in func]`. SyntaxError
# frames have no `, in func` part.
_FRAME_LINE = re.compile(
    r'^  File "(?P<file>[^"]*)", line (?P<line>\d+)(?P<rest>, in (?P<func>\S+))?'
)

_backend = None

# --------------------------------------------------------------------------
# The app's own interpreter (PY-17 / D172)
# --------------------------------------------------------------------------
#
# A script whose project root declares no dependencies runs with
# `interpreter=<this app's python>` and gets no venv at all: the app already
# ships `[bundled]` + its core `dependencies`, so numpy/pandas/duckdb/rasterio/…
# are there for free, with no download and no first-run wait. A script whose
# FOLDER declares some (PY-16) runs on that folder's venv, which contains exactly
# what the manifest declares — the complete list, not a delta against an
# invisible baseline (D172's rule, kept).
#
# The dangerous half is picking the interpreter. `LocalPythonComputeBackend`
# spawns `interpreter` verbatim as argv[0], and on that branch it silently
# ignores `requirements` — so handing it anything that is not a genuine,
# usable python has no fallback and no error that names the cause:
#
#   * a py2app bundle's launcher stub (`Contents/MacOS/FusedRender`) would
#     spawn the whole app as a subprocess per run. `sys.executable` inside the
#     bundle is NOT that stub — py2app ships a real interpreter at
#     `Contents/MacOS/python` and points `sys.executable` at it, which is why
#     `executor.py`'s `[sys.executable, _child.py]` works there (D33, and
#     build_dmg.sh smoke-tests exactly that spawn).
#   * but that bundled python needs PYTHONHOME to find its runtime, and
#     `python_compute` STRIPS PYTHONHOME from the child. Measured on a real DMG:
#     stripped, it reports the BUILD MACHINE's Homebrew framework as its prefix.
#     So the probe runs under the child's env, not ours, and the macOS bundle
#     needs the wrapper (`_wrapper_interpreter`) rather than the raw path.
#   * on Windows the launcher execs `pythonw.exe` (windows/launcher/launcher.c);
#     `python.exe` beside it is the same install with usable std streams.
#   * the Linux AppImage's `usr/python/bin/python3` (scripts/linux/AppRun) is an
#     ordinary relocatable python and needs none of this.
#
# So: resolve a candidate, then PROVE it by running it — and if the raw candidate
# cannot work, prove a wrapper for it instead. The probe is the assertion (one
# subprocess per rung per server process). When nothing verifies, a header-less
# script FAILS with a configuration error (D175): it is never quietly run in an
# environment without the app's packages.
_UNPROBED = object()
_app_interpreter = _UNPROBED

# The probe used to be serialized for free: `app_interpreter()` ran on the
# single-threaded event loop. `/api/run` now does `await
# asyncio.to_thread(app_interpreter)` (so a slow probe cannot stall the loop),
# which makes it genuinely concurrent — and two header-less runs starting at once
# would then both probe, with the LOSER free to cache its `None` over the
# winner's working path. `None` is terminal and per-process, so that one race
# breaks every header-less script until the server restarts. Hence one lock,
# held only around the resolve; the fast path below still reads the cache
# unlocked, which is safe because the cache only ever goes _UNPROBED -> final.
_app_interpreter_lock = threading.Lock()

# Escape hatch and test seam: an explicit interpreter to use for header-less
# scripts. Still probed — an override that is not a usable python is a
# misconfiguration to fall back from, not a reason to spawn it.
_APP_PYTHON_ENV = "FUSED_RENDER_APP_PYTHON"

# Mirrors python_compute._STRIPPED_ENV_VARS. Read off the module when it is
# importable so the probe cannot drift from the env the child actually gets;
# the literal is the fallback for a fused too old (or too new) to expose it.
_FALLBACK_STRIPPED = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP")

_PROBE = (
    "import json,sys;"
    "print(json.dumps({'prefix': sys.prefix, 'executable': sys.executable}))"
)

# Where the generated wrapper (see _wrapper_interpreter) lives: under the app's
# OWN cache, never `fused`'s venvs_path, where it could collide with a
# requirements key or be deleted by `ensure_requirements_venv`. Named `python` so
# every log line and `ps` entry reads as an interpreter.
_WRAPPER_CACHE = ("cache", "_app_interpreter", "bin")
_WRAPPER_NAME = "python"

# The probe is a one-line `-c` on the local filesystem: a real python answers in
# well under a second, and nothing this budget protects is legitimately slower.
# Deliberately SMALL, because it is paid on the request path in exactly the case
# it exists to catch — a candidate that never answers (a GUI launcher stub that
# ignores `-c`) would otherwise spend the whole timeout on the first header-less
# /api/run, which is indistinguishable from a hung page. Cold-start slowness
# (first-launch Gatekeeper validation on macOS, an interpreter on a network
# share) is why this is 5s rather than 1s, not why it would be 60.
_PROBE_TIMEOUT_S = 5

# Basenames we are willing to SPAWN when the candidate was autodetected. The
# concern is not correctness (the probe settles that) but the cost of being
# wrong: if `sys.executable` were ever a py2app launcher stub, spawning it could
# start a second copy of the whole app rather than fail. So an autodetected
# candidate must at least be named like an interpreter before any process is
# created. (What py2app's stub actually does with `-c` is unverified here — this
# guard means we never have to find out.) An explicit FUSED_RENDER_APP_PYTHON is
# exempt: it is deliberate configuration, and a user pointing at a wrapper
# script with some other name is a case worth allowing.
_PYTHON_BASENAMES_PREFIX = "python"


def reset_app_interpreter_cache() -> None:
    """Forget the probed interpreter so the next call re-resolves it."""
    global _app_interpreter
    with _app_interpreter_lock:
        _app_interpreter = _UNPROBED


def _stripped_env_vars() -> tuple[str, ...]:
    try:
        from fused.agent_core.backends.local import python_compute
    except ImportError:
        return _FALLBACK_STRIPPED
    return tuple(getattr(python_compute, "_STRIPPED_ENV_VARS", _FALLBACK_STRIPPED))


def _interpreter_candidate() -> tuple[str, bool]:
    """(the interpreter we would like to use, whether it was autodetected)."""
    override = os.environ.get(_APP_PYTHON_ENV)
    if override:
        return override, False
    exe = sys.executable
    name = os.path.basename(exe)
    if name.lower().startswith("pythonw"):
        sibling = os.path.join(os.path.dirname(exe), name[:6] + name[7:])
        if os.path.isfile(sibling):
            return sibling, True
    return exe, True


def _child_env() -> dict:
    """The environment the backend will give the child — what the probe must use.

    `python_compute` strips PYTHONHOME/PYTHONPATH/VIRTUAL_ENV/PYTHONSTARTUP. A
    packaged interpreter that only self-locates *because* the app exports
    PYTHONHOME would pass a probe run with our env and then die for real.
    """
    return {k: v for k, v in os.environ.items() if k not in _stripped_env_vars()}


def _probe(exe: str) -> tuple[dict | None, str]:
    """Run `exe` and report what it says about itself. (info, failure detail)."""
    try:
        proc = subprocess.run(
            [exe, "-c", _PROBE],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S, env=_child_env(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"{type(e).__name__}: {e}"
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1]), ""
    except (ValueError, IndexError) as e:
        return None, f"unparseable probe output ({type(e).__name__}: {e})"


def _wrapper_path() -> str:
    from fused_render.shell.storage import home_dir

    return os.path.join(home_dir(), *_WRAPPER_CACHE, _WRAPPER_NAME)


def _interpreter_home() -> str | None:
    """The PYTHONHOME this process needs, when it needs one.

    Inside the py2app bundle the launcher exports
    `PYTHONHOME=…/Contents/Resources` (`scripts/build_dmg.sh` does this for every
    invocation of that python and says why), and that is the ONLY thing making
    `Contents/Resources/lib/python3.12` — where py2app flattens both the stdlib
    and site-packages — findable. The environment we were launched with is the
    ONLY source read here, and an unset PYTHONHOME is answered with None rather
    than with `sys.prefix`.

    That `sys.prefix` fallback was deliberately abandoned: without PYTHONHOME the
    bundle's python reports the BUILD MACHINE's Homebrew framework as its prefix
    (measured against a real DMG — `_wrapper_interpreter` below records the same
    measurement), so reading it there yields a path that does not exist on the
    user's machine. Building a wrapper around that is strictly worse than the
    honest "this process needs no wrapper".

    None means this process does not depend on PYTHONHOME, so there is nothing
    for a wrapper to hand on and no reason to build one.
    """
    home = os.environ.get("PYTHONHOME") or ""
    if not home:
        return None
    return home if os.path.isdir(home) else None


def _wrapper_interpreter(candidate: str) -> tuple[str | None, str]:
    """A tiny script that restores PYTHONHOME and execs `candidate`.

    This is the packaged-macOS answer, and it replaced a venv-based one because
    **the bundle ships no `venv` module at all** — not in
    `Contents/Resources/lib/python3.12`, not in `lib/python312.zip`, and the
    embedded `Python.framework` contains only the `Python` dylib with no second
    interpreter binary. `-m venv` fails there regardless of environment, so no
    amount of `--system-site-packages` could have worked. Measured against a real
    DMG, which is also how we know the direct candidate fails: without PYTHONHOME
    that python reports the BUILD MACHINE's Homebrew framework as its prefix — a
    path that does not exist on a user's machine.

    `interpreter=` is just an executable path, so a script is a legal answer.

    Two details that are not stylistic:

    **`exec -a <wrapper>`** (hence bash, not sh) makes the child's
    `sys.executable` the WRAPPER rather than the raw python. That is deliberate:
    `geotiff/tile_server.py` and `zarr_aoi/tile_server.py` spawn their daemons as
    `[sys.executable, …]` with PYTHONHOME **scrubbed from the child env** (their
    own comment explains why — a bundle-scoped PYTHONHOME would poison a uv
    venv). Measured on the DMG: with the raw python as `sys.executable` that
    spawn dies with `ModuleNotFoundError: No module named 'pandas'`; with the
    wrapper it succeeds, because the wrapper re-establishes PYTHONHOME itself and
    is therefore immune to the scrub. Same for `usd/convert_worker.py`.

    **Not `PYTHONEXECUTABLE`**, which would achieve the same `sys.executable`
    with less machinery and was rejected on measurement: it is inherited by every
    descendant and applies to *any* python they run, so an unrelated interpreter
    (exactly geotiff's uv-venv daemon, when uv IS present) reports OUR wrapper as
    its `sys.executable` and re-spawns into the wrong interpreter. `exec -a`
    affects only this one process.

    Regenerated whenever the content would differ (the app can move), 0700 since
    it is derived state naming absolute paths. Returns (path, "") or (None, why).
    """
    home = _interpreter_home()
    if home is None:
        return None, (
            "this process does not use PYTHONHOME, so a wrapper has nothing to "
            "restore"
        )
    if os.name == "nt":
        # Windows interpreters self-locate; there is no PYTHONHOME to restore and
        # no POSIX shell to do it with. Gated rather than attempted.
        return None, "not applicable on Windows"

    body = (
        "#!/bin/bash\n"
        "# Generated by fused_render (engine.app_interpreter) - derived state, not\n"
        "# config. Restores the PYTHONHOME the packaged interpreter needs, which the\n"
        "# compute backend strips from its children. Regenerated when it changes.\n"
        f"PYTHONHOME={shlex.quote(home)}\n"
        "export PYTHONHOME\n"
        "unset PYTHONPATH\n"
        # -a so the child's sys.executable is THIS script: see the docstring.
        f"exec -a {shlex.quote(_wrapper_path())} {shlex.quote(candidate)} \"$@\"\n"
    )
    path = _wrapper_path()
    try:
        existing = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                existing = f.read()
        if existing != body:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Write-then-rename so a concurrent reader never sees a partial
            # script. The temp name carries the THREAD id as well as the pid
            # (same reason as envinstall._write_record): two threads of this one
            # process can be in here at once, and with a pid-only name the first
            # `os.replace` consumes the shared temp file out from under the
            # second, which then fails with FileNotFoundError — reported as
            # "could not write the interpreter wrapper" for a wrapper that is
            # perfectly fine.
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(tmp, 0o700)
            os.replace(tmp, path)
    except OSError as e:
        return None, f"could not write the interpreter wrapper: {e}"
    return path, ""


def app_interpreter() -> str | None:
    """A verified path to an interpreter with this app's packages, or None.

    Two rungs, each **proven by running it** under the environment the backend
    will actually give the child — never accepted for existing:

      1. the app's own `sys.executable` (or `python.exe` beside a `pythonw.exe`).
         Serves a dev checkout, the Linux AppImage (python-build-standalone,
         self-locating) and the Windows installer.
      2. failing that, a generated wrapper that restores PYTHONHOME
         (`_wrapper_interpreter`). Serves the packaged macOS .app, where rung 1
         cannot work: strip PYTHONHOME and that interpreter reports the build
         machine's Homebrew framework as its prefix.

    ONE acceptance rule for both, which is why there are no per-rung caveats to
    keep straight: the child must report our own `sys.prefix`. Same prefix means
    the same installation, hence the same site-packages, which is the entire point
    — `[bundled]` and the core dependencies importable with nothing installed.

    Cached per process (each probe is a subprocess) and resolved at most once even
    under concurrent callers; `reset_app_interpreter_cache` clears it.
    """
    global _app_interpreter
    # Double-checked: the cache only transitions _UNPROBED -> final, so an
    # unlocked hit is already the final answer and needs no lock.
    if _app_interpreter is not _UNPROBED:
        return _app_interpreter
    with _app_interpreter_lock:
        if _app_interpreter is not _UNPROBED:
            return _app_interpreter
        _app_interpreter = _resolve_app_interpreter()
        return _app_interpreter


# --- a header the app interpreter ALREADY satisfies ---------------------------
#
# D172 settled that a header is the script's COMPLETE dependency list: no baseline
# is unioned into it. What nothing checked is the INVERSE question — whether the
# list is already satisfied by the interpreter the app ships. `[bundled]` bakes
# pandas/numpy/duckdb/pyarrow/geopandas/rasterio/zarr/pyproj/keyring/pyyaml/
# cryptography into that interpreter, so a header naming `pandas` built a
# multi-gigabyte venv beside the pandas already on disk, then downloaded it again
# for the next header that differed by one package.
#
# Measured on one developer machine's venv store: 33 venvs / 4.9GB beside a 51GB uv
# cache, in which the set ['duckdb>=1.5.0','keyring>=24','pandas>=2.0.0',
# 'pyarrow>=14.0.0','pyyaml>=6.0.0'] — every member already present on the
# interpreter — existed under FIVE distinct keys, and one thirteen-package science
# stack under four.
#
# What makes skipping the venv expressible at all is upstream's precedence rule:
# `_execute_sync` reads `interpreter` and, when it is set, never looks at
# `requirements` (`compute_base.execute`'s docstring states it outright, and
# tests/test_env_install.py pins it). So handing over an interpreter is a claim of
# full ownership over what is installed — upstream will neither build nor verify
# anything behind it. That is exactly why every branch below fails CLOSED: this
# path may only be taken when every requirement is PROVEN present, because the
# alternative to proof is not a slower run, it is a script that dies on its first
# import with an error about the code rather than about the environment.
#
# The accepted trade: such a script now runs on the shared app interpreter and can
# therefore see packages its header never declared, where a purpose-built venv
# would have hidden them. PY-17 already runs every header-LESS script exactly this
# way, so this widens an existing property rather than introducing a new one.
_FORCE_VENV_ENV = "FUSED_RENDER_FORCE_SCRIPT_VENV"

# Distribution names + versions, as the APP INTERPRETER sees them. It has to be
# that interpreter and not this process: in the packaged macOS app they are
# different pythons with different site-packages, and answering from ours would
# clear a header the child cannot actually import.
#
# `importlib.metadata`, not an import attempt, because the question is a PEP 508
# one — `pandas>=2.0.0` needs a VERSION, and importability cannot supply it — and
# because distribution names are what a header spells: `python-pptx` imports as
# `pptx`, so an import-based check would need a name map that metadata makes
# unnecessary.
_APP_PACKAGES_PROBE = (
    "import json;"
    "from importlib.metadata import distributions;"
    "print(json.dumps({(d.metadata['Name'] or ''): d.version for d in distributions()}))"
)

_app_packages = _UNPROBED
# Same reasoning as `_app_interpreter_lock`: `app_satisfies` is reached through
# `asyncio.to_thread`, so two runs starting together genuinely race, and the cache
# only ever goes _UNPROBED -> final.
_app_packages_lock = threading.Lock()


def reset_app_packages_cache() -> None:
    """Forget the probed distribution list so the next call re-probes."""
    global _app_packages
    with _app_packages_lock:
        _app_packages = _UNPROBED


def _probe_app_packages(exe: str) -> dict | None:
    """`{canonical distribution name: version}` for `exe`, or None if it wouldn't say.

    None is "no evidence", never "nothing installed" — `app_satisfies` treats the
    two completely differently, and collapsing them would send every satisfied
    header to the venv path on one unlucky spawn (harmless) or, with the sense
    inverted, clear a header on a probe that never ran (not harmless).

    Under `_child_env()` and `_PROBE_TIMEOUT_S`, for the same reasons `_probe`
    documents: the environment the backend will actually give the child, and a
    budget small enough that a candidate which never answers cannot hang a request.
    """
    try:
        proc = subprocess.run(
            [exe, "-c", _APP_PACKAGES_PROBE],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S, env=_child_env(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.info("app package probe failed for %s: %s: %s", exe, type(e).__name__, e)
        return None
    if proc.returncode != 0:
        logger.info("app package probe failed for %s: %s", exe,
                    (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}")
        return None
    try:
        raw = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        logger.info("app package probe output unparseable for %s: %s: %s",
                    exe, type(e).__name__, e)
        return None
    # Canonicalised HERE rather than in the probe: PEP 503 folding (case, and
    # `_`/`.`/`-` all equivalent) is `packaging`'s job, and the probe must stay a
    # one-liner that runs on an interpreter which may not have packaging at all.
    from packaging.utils import canonicalize_name

    return {canonicalize_name(name): version for name, version in raw.items() if name}


def app_packages() -> dict | None:
    """`_probe_app_packages` for the app interpreter, memoized per process.

    The ceiling this guarantees, and the reason it exists: at most ONE subprocess
    per server process, never one per `/api/run`. The probe enumerates every
    distribution on the interpreter's path, and it is consulted on the request path
    of every PEP 723 script — the exact per-request spawn this fast path was built
    to remove, so paying it per request would be self-defeating.
    """
    global _app_packages
    if _app_packages is not _UNPROBED:
        return _app_packages
    with _app_packages_lock:
        if _app_packages is not _UNPROBED:
            return _app_packages
        exe = app_interpreter()
        # No verified interpreter means nothing to probe AND nothing to run on, so
        # there is no answer to cache-bust later: `app_interpreter` is itself
        # per-process and terminal, so this cannot go stale while it stays None.
        _app_packages = None if exe is None else _probe_app_packages(exe)
        return _app_packages


def app_satisfies(requirements: list[str]) -> bool:
    """Does the app interpreter already meet EVERY one of `requirements`?

    All-or-nothing, because a script runs on exactly one interpreter: "pandas from
    the app, imagecodecs from a venv" is not a thing that can be arranged, so a
    single unmet requirement sends the whole header down the venv path unchanged.

    Every uncertain answer is False. The cases, and why each one is not a
    judgement call:

    * **no requirements** — that is PY-17's business, and the vacuous truth a
      quantifier gives here would claim this path for a script that never asked.
    * **`_FORCE_VENV_ENV` set** — the escape hatch, checked before any probe so it
      also costs nothing.
    * **the probe said nothing** (`app_packages() is None`) — no evidence is not
      evidence, the same three-valued discipline `envinstall._venv_is_usable` and
      `_probe` already follow.
    * **extras** (`pandas[performance]`) — a version number vouches for the
      distribution and says nothing whatsoever about an extra's transitive
      dependencies, so this is unprovable by construction, not merely unproven.
    * **unparseable requirement, or any exception at all** — "I could not read it"
      must never arrive as "it is already there".

    Environment markers are ignored on purpose: `run_python` has already dropped
    the requirements whose markers do not hold (via
    `projectenv.applicable_dependencies_of`) and passes
    the survivors verbatim, markers included, so re-evaluating one here would be a
    second implementation of a decision that is already made.
    """
    if not requirements:
        return False
    if os.environ.get(_FORCE_VENV_ENV):
        return False
    # Imported BEFORE `app_packages()`, not just before the loop: `_probe_app_packages`
    # canonicalises with `packaging` too, so probing first would let the very
    # ImportError this handler exists for escape from underneath it — turning a
    # missing parser into an EngineError on every header instead of one warning and
    # the venv path.
    #
    # Split out of the catch-all below, and NOT logged as an exception: a missing
    # parser is a packaging fault, not a bug in this call, and it would otherwise
    # write a full traceback on every run of every header while silently disabling
    # the fast path — a permanent slowdown whose only evidence is a stack trace that
    # looks like a crash. `packaging` is a declared dependency (pyproject.toml), so
    # this is a broken install rather than a supported configuration.
    try:
        from packaging.requirements import InvalidRequirement, Requirement
        from packaging.utils import canonicalize_name
    except ImportError as e:
        logger.warning(
            "cannot check whether the app interpreter satisfies %r: %s. Every "
            "header will build its own venv until `packaging` is importable.",
            requirements, e,
        )
        return False
    installed = app_packages()
    if installed is None:
        return False
    try:
        for spec in requirements:
            try:
                req = Requirement(spec)
            except InvalidRequirement:
                return False
            if req.extras:
                return False
            # A direct reference (`pandas @ https://…/pandas-9.9.9.whl`, or a local
            # path / VCS URL) names a SOURCE, and an installed version number is no
            # evidence at all about whether the thing installed came from it. Left
            # unchecked this was the extras hole in a second shape: the app happens
            # to have a `pandas`, so the header cleared and the wheel the author
            # deliberately pinned was never fetched — a script silently running
            # against different code than it asked for. Not hypothetical here,
            # either: this repo pinned its own `fused` as a direct-URL wheel for
            # months (pyproject.toml's `[fused]` extra, now a PyPI pre-release),
            # so the idiom is one a template author has every reason to copy.
            if req.url:
                return False
            version = installed.get(canonicalize_name(req.name))
            if version is None:
                return False
            # prereleases=True so an app shipping `2.1.0rc1` counts against
            # `>=2.0.0`. The default excludes pre-releases, which would send a
            # header to the venv path over a version the interpreter genuinely
            # has — and that venv would resolve the very same pre-release.
            if str(req.specifier) and not req.specifier.contains(version, prereleases=True):
                return False
        return True
    except Exception:  # noqa: BLE001
        # Deliberately total: this function's contract is that it never turns an
        # internal problem into a claim that packages are present. Logged rather
        # than swallowed, because a persistent failure here is a silent, permanent
        # loss of the fast path and nothing else would ever say so.
        logger.exception("app_satisfies failed for %r; treating as unsatisfied",
                         requirements)
        return False


def _interpreter_path_available() -> bool:
    """Can this backend run code on an interpreter WE choose?

    `_execute_sync` is the only route to that (`execute()` derives an interpreter
    only from a uv workflow project, which a standalone .py has none of), so
    without it the fast path cannot be taken at all.

    Asked before the pre-flight rather than discovered inside `_execute`, and that
    ordering is the whole point: a header that declines the fast path must still
    reach `is_installed`, so a missing venv comes back as `needs_install` and the
    loader builds it off the request path. Discovering it later would leave
    `execute(requirements=…)` to build the venv INLINE — a blocking download inside
    /api/run, which is the exact failure PY-18 exists to remove.

    Note this is a strictly weaker requirement than the header-LESS path's: there,
    no `_execute_sync` is fatal (D175 — an empty venv has no data stack, so running
    the script anyway would fail on `import numpy`), because there is nothing to
    fall back TO. Here the venv path is a perfectly good fallback: slower, more
    isolated, and exactly what this header did before the fast path existed.

    `get_backend()` raising is one of those "no" answers, not an error to propagate.
    It raises when `fused` is absent entirely, and this gate is the FIRST thing
    /api/run touches — so letting it through would make every failure downstream
    (the pre-flight's `RuntimeError`, `needs_install`, D175) unreachable, replacing
    each one's specific diagnosis with a bare ModuleNotFoundError from a fast-path
    probe that was only ever asking an optional question.
    """
    try:
        return hasattr(get_backend(), "_execute_sync")
    except Exception:  # noqa: BLE001
        return False


def _app_interpreter_if_satisfies(requirements: list[str]) -> str | None:
    """The app interpreter when it already meets `requirements`, else None.

    Both probes behind ONE `asyncio.to_thread` hop from `run_python`: they are
    subprocess-backed on first call, and two hops would be two chances to stall
    the event loop for no gain.
    """
    return app_interpreter() if app_satisfies(requirements) else None


def _resolve_app_interpreter() -> str | None:
    """Probe the rungs and answer with the interpreter to cache.

    Split out of `app_interpreter` purely so the cache is written in exactly one
    place, under the lock: every early return here used to assign the global
    itself, which is what let a losing thread's `None` land on top of a winner's
    working path.
    """
    candidate, autodetected = _interpreter_candidate()
    name = os.path.basename(candidate).lower().removesuffix(".exe")
    if autodetected and not name.startswith(_PYTHON_BASENAMES_PREFIX):
        # Rejected WITHOUT spawning it — see _PYTHON_BASENAMES_PREFIX.
        logger.error(
            "%r cannot be this app's interpreter: its name %r is not an "
            "interpreter's, so it was not run. Set %s to a real python.",
            candidate, name, _APP_PYTHON_ENV,
        )
        return None

    info, detail = _probe(candidate)
    if info is not None and info["prefix"] == sys.prefix:
        logger.info("header-less scripts will run on %s", candidate)
        return candidate

    why = detail or (
        f"it reports sys.prefix {info['prefix']!r}, not this app's {sys.prefix!r}"
    )
    logger.info(
        "%r cannot be used directly (%s) — trying a PYTHONHOME wrapper so "
        "header-less scripts still see this app's packages", candidate, why,
    )

    wrapper, wrap_detail = _wrapper_interpreter(candidate)
    if wrapper is not None:
        # Probed exactly like rung 1, under the same stripped env and against the
        # same rule: it has to earn its place by running, not by existing.
        wrap_info, wrap_probe_detail = _probe(wrapper)
        if wrap_info is not None and wrap_info["prefix"] == sys.prefix:
            logger.info(
                "header-less scripts will run on %s (PYTHONHOME wrapper for %s)",
                wrapper, candidate,
            )
            return wrapper
        wrap_detail = wrap_probe_detail or (
            f"the wrapper reports sys.prefix {wrap_info['prefix']!r}, not this "
            f"app's {sys.prefix!r}"
        )

    logger.error(
        "No usable interpreter for scripts that declare no dependencies. %r could "
        "not be used directly (%s), and the PYTHONHOME wrapper did not work either "
        "(%s). Such scripts will now fail with a clear error rather than run in an "
        "environment without this app's packages. Set %s to a Python executable "
        "that has them.",
        candidate, why, wrap_detail, _APP_PYTHON_ENV,
    )
    return None


def available() -> bool:
    """True iff the fused local backend is importable in this process.

    Import failure of any flavor (package absent, too-old Python, a broken
    install) means "not available" — the caller falls back to the built-in
    executor rather than surfacing an import error to every /api/run.
    """
    try:
        from fused.agent_core.backends.local import python_compute  # noqa: F401
    except ImportError:
        return False
    return True


# warm() caches both outcomes; invalidate() clears it (mid-session install).
_available_cached: bool | None = None
_available_lock = threading.Lock()


def warm() -> None:
    """Import the fused backend once off the request path and cache the result.

    Startup daemon thread: on a fresh install the cold import is ~a minute and
    left lazy it would freeze the first /api/config that resolves the engine."""
    global _available_cached
    t0 = time.monotonic()
    ok = available()
    with _available_lock:
        _available_cached = ok
    logger.info("engine warm-up: fused backend %s (%.1fs)",
                "ready" if ok else "unavailable", time.monotonic() - t0)


def warm_in_background() -> None:
    """Fire-and-forget warm() on a daemon thread (server startup hook)."""
    threading.Thread(target=warm, daemon=True, name="engine-warmup").start()


def invalidate() -> None:
    """Clear the cached availability so the next resolve re-checks (mid-session install)."""
    global _available_cached
    with _available_lock:
        _available_cached = None


def available_nonblocking() -> bool:
    """available() without the cold import: warm()'s cached result, else find_spec."""
    with _available_lock:
        if _available_cached is not None:
            return _available_cached
    return importlib.util.find_spec("fused") is not None


def get_backend():
    # Lazy singleton: importing the backend pulls in the fused package tree,
    # and constructing it is only needed once per server process. 60s matches
    # the built-in executor's per-run timeout (executor.DEFAULT_TIMEOUT) — a
    # cold overview read of a large remote COG legitimately takes ~30-40s, so
    # the two engines must agree or the cold pyramid analyze dies at 30s here.
    global _backend
    if _backend is None:
        from fused.agent_core.backends.local.python_compute import LocalPythonComputeBackend

        from fused_render import envinstall
        from fused_render.executor import DEFAULT_TIMEOUT

        # cache_storage=None disables result caching explicitly (PY-9: fresh
        # execution every call). It is the upstream default today, but we may
        # track a nightly wheel — don't rely on a default staying put.
        #
        # python_executable pins the base interpreter script venvs are built from
        # to 3.12 (D214). Passed HERE, at the one place the backend is constructed,
        # because `envinstall._python_executable()` reads the attribute straight
        # back off this instance: the resolution and the venv key it feeds are one
        # value with one source, and a loader that re-decided it independently
        # could disagree with the backend and fill a directory no run ever reads.
        #
        # None for every packaged build (they already run 3.12), which is exactly
        # the value this argument had before D214 — so their venv keys do not move.
        _backend = LocalPythonComputeBackend(
            timeout_seconds=int(DEFAULT_TIMEOUT),
            cache_storage=None,
            python_executable=envinstall.script_python(),
        )
    return _backend


async def _execute(code: str, requirements: list[str], interpreter: str | None, input_files: dict):
    """Run `code` on the backend, on `interpreter` when one was resolved.

    The venv path (a script whose folder declares dependencies) goes through the public
    `execute()`, unchanged. The interpreter path cannot: `execute()` derives an
    `interpreter` ONLY by resolving a uv workflow venv from a `project` /
    `project_dir`, and a standalone .py has neither. So it calls the documented
    subclass contract (`_execute_sync`, whose docstring defines exactly this
    parameter) directly, off the event loop — which, with `cache_storage=None`,
    is what `execute()` would have done for these arguments anyway, minus the
    caching it has already disabled.

    If a future `fused` drops `_execute_sync` this raises rather than quietly
    running the script in an empty venv: with no baseline requirements (D172)
    that venv has no data stack, so the "fallback" would fail on the first
    import with an error about numpy instead of about the real breakage.
    """
    backend = get_backend()
    if interpreter is not None:
        if not hasattr(backend, "_execute_sync"):
            raise RuntimeError(
                "this fused build has no LocalPythonComputeBackend._execute_sync, "
                "so a script with no dependencies of its own cannot be run on this "
                "app's interpreter. Refusing to run it in an empty script venv, "
                "which would fail on the first import instead. Pin a fused version "
                "that provides `_execute_sync`."
            )
        else:
            # Keywords, not positionals: `_execute_sync` takes ten parameters and
            # a reordering upstream would silently pass `interpreter` as something
            # else. `requirements` is deliberately omitted — the interpreter wins
            # over it upstream, and passing both would imply otherwise.
            return await asyncio.to_thread(
                backend._execute_sync,
                code=code,
                input_files=input_files,
                interpreter=interpreter,
            )
    return await backend.execute(
        code=code, requirements=requirements, input_files=input_files
    )


def _binding_source() -> str:
    """The text of `fused_render/_binding.py`, for embedding into the wrapper.

    Read through importlib.resources rather than `__file__` + open() so it
    still works when the package is inside a zip / frozen distribution (the
    py2app and AppImage builds), where `_binding.py` is not a filesystem path.
    Re-read per call rather than cached: it costs one small read on a code path
    that is about to spawn a subprocess and build a venv, and a cache would go
    stale under the dev server's edit-and-rerun loop.
    """
    from importlib.resources import files

    return files("fused_render").joinpath("_binding.py").read_text(encoding="utf-8")


def build_code(user_code: str, script_dir: str, script_path: str = "script") -> str:
    """Wrap user code so its imports/data paths resolve next to the .py, and
    bridge the bare-``main()`` contract.

    The user's source is embedded as a literal and ``exec``'d as **its own
    compile unit under its real filename** — so a leading ``from __future__``
    import stays the first statement of its unit, and every user traceback
    frame carries the real file and exact line (no offset bookkeeping).

    The preamble also defines the module globals the built-in executor's worker
    gets for free, because it loads the file through
    ``spec_from_file_location("__fused_module__", path)`` and CPython's import
    machinery sets them. ``exec(compile(...))`` sets neither: ``compile()``'s
    filename argument only *labels* code objects for tracebacks, so a script
    doing ``os.path.dirname(__file__)`` — the ordinary way to find a data file
    next to your ``.py`` — raised ``NameError`` here while working under the
    other engine. ``__name__`` is set for the same parity reason; it was
    inherited from the backend's runner namespace as ``"builtins"``. Neither
    engine makes ``__name__`` ``"__main__"``, so ``if __name__ ==
    "__main__":`` blocks stay dormant — templates such as
    ``geotiff/tile_server.py`` rely on that, using the guard for the
    subprocess they spawn of themselves.

    The wrapper must NOT chdir before the user code runs: the backend's runner
    reads _params.json from the exec cwd after module-level code finishes, so
    cwd stays on the exec dir until an entrypoint is actually invoked. The
    epilogue then:

      * wraps a registered ``@fused.udf`` function so the chdir to the
        script's dir happens just before it runs (relative data paths resolve
        against the script, params still get found);
      * otherwise — compat bridge — if the module defines a bare ``main()``,
        reads ``_params.json`` itself, coerces string params by ``main``'s
        annotations (using ``_binding.py``'s own source, embedded into the
        wrapper because the child cannot import the package), chdirs,
        and sets ``result = main(**bound)``. ``main()`` wins even if the
        module also assigned a module-level ``result`` — the built-in
        executor's worker (``_child.py``) always calls ``main(**params)`` and
        overwrites whatever ``result`` the module set, so a file defining both
        must behave identically under either engine;
      * otherwise, if the module set ``result`` itself, leaves it untouched;
      * otherwise raises the built-in executor's "no callable 'main'" error
        (extended with the fused-contract alternatives), so a file with no
        entrypoint fails identically under either engine.
    """
    binding_source = _binding_source()
    preamble = (
        f"import os as _fused_os, sys as _fused_sys\n"
        f"_fused_sys.path.insert(0, {script_dir!r})\n"
        f"__file__ = {script_path!r}\n"
        f'__name__ = "__fused_module__"\n'
        f"exec(compile({user_code!r}, {script_path!r}, 'exec'), globals())\n"
    )
    epilogue = f"""
try:
    import fused as _fused_shim
    _fused_udfs = getattr(_fused_shim, "_registered_udfs", None)
except ImportError:
    _fused_udfs = None
if _fused_udfs:
    _fused_udf = _fused_udfs[-1]
    _fused_inner = _fused_udf._fn
    def _fused_chdir_call(*_a, **_k):
        _fused_os.chdir({script_dir!r})
        return _fused_inner(*_a, **_k)
    _fused_udf._fn = _fused_chdir_call
else:
    import json as _fused_json

    # Param binding is `_binding.py`'s REAL source, embedded verbatim — not a
    # re-implementation. The child cannot `import fused_render` (the local
    # backend strips PYTHONPATH from the subprocess), which is why the logic
    # has to travel inside the generated code at all; a hand-written copy of
    # the same 40 lines is what drifted, and the drift was invisible — string
    # annotations were coerced under this engine and passed through raw under
    # the built-in one, so the same template returned "7" here and 7 there.
    # Embedding the source means there is one implementation with two callers.
    #
    # exec'd into its own namespace rather than these globals: the user's
    # module shares this global dict, and `coerce`/`bind_params`/`ParamError`
    # are names a template could plausibly define itself (everything else the
    # epilogue adds is `_fused_*`-prefixed for the same reason).
    # __name__ = "__main__" so that ParamError.__module__ is a name traceback
    # suppresses when printing the final line: the text stays
    # "ParamError: missing required param: 'x'", which _split_error turns into
    # the same error.type the built-in worker reports from type(e).__name__
    # (PY-14 — both engines must surface one wire shape for one bad input).
    _fused_binding_ns = {{"__name__": "__main__"}}
    exec(compile({binding_source!r}, "<fused_render/_binding.py>", "exec"), _fused_binding_ns)
    _fused_bind = _fused_binding_ns["bind_params"]

    def _fused_run_main():
        _fn = globals().get("main")
        if not callable(_fn):
            if "result" in globals():
                return globals()["result"]
            raise AttributeError(
                _fused_os.path.basename({script_path!r})
                + " does not define a callable 'main' function, a "
                "@fused.udf-decorated function, or a 'result' variable"
            )
        _params = {{}}
        _pf = _fused_os.path.join(_fused_os.getcwd(), "_params.json")
        if _fused_os.path.exists(_pf):
            with open(_pf) as _f:
                _params = _fused_json.load(_f) or {{}}
        _fused_os.chdir({script_dir!r})
        return _fn(**_fused_bind(_fn, _params))

    result = _fused_run_main()
"""
    return preamble + epilogue


def _clean_error(error_text: str, script_path: str) -> str:
    """Drop plumbing frames so a traceback starts at the user's real file.

    User code runs as its own compile unit under its real filename
    (build_code), so its frames already carry the script's path and exact
    lines — nothing needs rewriting. What remains is noise around them:
    backend internals (_runner.py, the fused shim) above the first user frame,
    and the "<lambda_exec>" wrapper/epilogue frames (the exec() trampoline and
    the bare-main bridge helpers). This drops those. Text with no
    "<lambda_exec>" frame (timeouts, backend messages) passes through
    unchanged — a raw traceback beats a mangled one.
    """
    if '  File "<lambda_exec>"' not in error_text:
        return error_text
    try:
        out = []
        seen_user_frame = False
        dropping = False  # inside a frame we decided to drop
        for line in error_text.splitlines():
            m = _FRAME_LINE.match(line)
            if m:
                if m.group("file") == script_path:
                    seen_user_frame = True
                    dropping = False
                    out.append(line)
                    continue
                # <lambda_exec> (wrapper/epilogue) frames are always plumbing;
                # other files above the first user frame are backend internals.
                # Frames below it (user code calling into libraries) are kept.
                dropping = m.group("file") == "<lambda_exec>" or not seen_user_frame
                if not dropping:
                    out.append(line)
                continue
            # Source/caret lines belong to the frame above them; anything not
            # indented (header, exception message, chain separators) is kept.
            if line.startswith("    ") and dropping:
                continue
            out.append(line)
        return "\n".join(out) + ("\n" if error_text.endswith("\n") else "")
    except (ValueError, AttributeError):
        return error_text


def _needs_install_dict(project_dir: str, abs_path: str) -> dict:
    """The pre-flight answer for a project whose venv isn't built yet (PY-18).

    Carries `needs_install` for the loader AND a populated `error` object, so a
    client that knows nothing about the loader (an older page, a direct API
    caller, the Calls log) still shows a real message naming the project rather
    than an undefined field.

    The project root and its display name travel in the object so the loader can
    title its progress row — "Preparing my-app" — without a second request.
    Additive, so a client that ignores them is unaffected.
    """
    from fused_render import envinstall, projectenv

    # The APPLICABLE ones — the same list the routing decision used and the same
    # list `uv sync` will install. Naming the raw declaration here meant the
    # loader row and the error message could promise a package (a
    # `sys_platform == 'darwin'` entry on Linux) that the install would skip.
    requirements = projectenv.applicable_dependencies_of(project_dir)
    name = projectenv.display_name(project_dir)

    # Two rounds are possible (D214): with no pinned Python on this machine the
    # FIRST install is the interpreter, reported under its own key, and the packages
    # follow once it lands. The key differs between the rounds on purpose — it is
    # what lets the page tell "we made progress, ask again" from "we installed and
    # nothing changed", which is a loop.
    needs_python = not envinstall.script_python_ready()
    if needs_python:
        key = envinstall.PYTHON_BOOTSTRAP_KEY
        message = (
            f"{name} declares dependencies that need Python "
            f"{envinstall.SCRIPT_PYTHON_VERSION}, which this machine does not have "
            "yet. It needs a one-time download, and then the packages themselves."
        )
    else:
        key = envinstall.venv_key_for(project_dir)
        message = (
            f"{name} declares dependencies that are not installed yet: "
            f"{', '.join(requirements)}. They need a one-time download."
        )
    return {
        "ok": False,
        "needs_install": {
            "key": key,
            "requirements": requirements,
            "py": abs_path,
            # The project the environment belongs to, so the loader can title one
            # row "Preparing <name>" rather than joining a package list — and so
            # every script in the folder is visibly ONE install rather than N.
            "project": project_dir,
            "name": name,
            # The declaration itself, so `runtime.js` can put it in the live-reload
            # watch set. Sent as a resolved path rather than left for the client to
            # join onto `project`: the root is the server's answer and the
            # separator is the server's platform. Without this, a user who fixes
            # their dependencies sees the same error overlay with nothing telling
            # them anything changed.
            "pyproject": projectenv.pyproject_path(project_dir),
            # So the loader can name what it is fetching instead of listing packages
            # it is not downloading yet. Absent (not false) on the ordinary path, so
            # a client that ignores it is unaffected.
            **({"python": envinstall.SCRIPT_PYTHON_VERSION} if needs_python else {}),
        },
        "error": {
            "type": "EnvNotInstalled",
            "message": message,
            "traceback": "",
        },
        "stdout": "",
    }


def _error_dict(err_type: str, message: str, tb: str = "") -> dict:
    # The built-in executor's wire shape, so all failures render uniformly.
    return {
        "ok": False,
        "error": {"type": err_type, "message": message, "traceback": tb},
        "stdout": "",
    }


def _split_error(cleaned: str) -> tuple[str, str]:
    """(type, message) from a traceback's final `SomeError: message` line.

    Falls back to ("Error", <last non-empty line>) when the text doesn't end in
    the standard form (timeouts, backend messages).
    """
    for line in reversed(cleaned.splitlines()):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*:\s*(.*)$", line)
        if m and (m.group(1).endswith("Error") or m.group(1).endswith("Exception")):
            return m.group(1), m.group(2)
        return "Error", line
    return "Error", cleaned.strip() or "execution failed"


async def run_python(path: str, params: dict) -> dict:
    if not os.path.isfile(path):
        return _error_dict("FileNotFoundError", f"no such Python file: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_code = f.read()
    except OSError as e:
        return _error_dict("OSError", f"cannot read {path}: {e}")

    from fused_render import projectenv

    # The environment is the FOLDER's (SPEC PY-16). `project` is the project root
    # when that folder declares one, and None when it does not — every `.py`
    # under a root resolves to the same answer however deep it sits, which is
    # what makes one page calling five scripts one install.
    project = projectenv.project_env_for(path)
    # The APPLICABLE dependencies, from the one helper every caller shares
    # (`_needs_install_dict` and `has_project_env` use it too). A dependency whose
    # PEP 508 marker does not hold here is not one this platform needs: leaving it
    # in would make `app_satisfies` refuse a fast path over a package that will
    # never be installed, and would let the loader name it. uv applies the same
    # markers when it syncs, so every side agrees about what the environment
    # actually contains.
    requirements = projectenv.applicable_dependencies_of(project) if project else []

    # No project -> the app's own interpreter, no venv (PY-17). `interpreter` and
    # `requirements` are mutually exclusive upstream (the interpreter branch
    # ignores requirements silently), so they are never both set here.
    interpreter = None
    if project is None:
        # Off the event loop: `app_interpreter` is sync (it is called from sync
        # contexts and tests) and its first call in a process runs up to two
        # `subprocess.run(..., timeout=5)` probes plus a wrapper write. /api/run
        # awaits this coroutine directly, so inline that stalls the entire
        # server — websockets, watcher, every other request — for the probe's
        # duration. The per-process cache means only the first call pays the hop.
        interpreter = await asyncio.to_thread(app_interpreter)
        if interpreter is None:
            # NEVER fall through to a venv here. With no baseline requirements
            # (D172) that venv is stdlib-only, so a template that works today
            # would fail on `import numpy` — an error about the wrong thing
            # entirely, on a path the user cannot see. And a header-less core
            # template must never reach the network, which a venv build would.
            # A configuration error naming its own fix is strictly better.
            return _error_dict(
                "InterpreterUnavailable",
                "This app could not resolve a usable Python interpreter for "
                f"{os.path.basename(path)}, which declares no dependencies of its "
                "own and so expects to run on the app's own interpreter (with "
                "numpy/pandas/duckdb/… already installed). Nothing was run: "
                "falling back to an empty environment would fail on the first "
                f"import instead. Set {_APP_PYTHON_ENV} to a Python executable "
                "that has this app's packages. The server log records which "
                "candidates were tried and why each was rejected.",
            )

    abs_path = os.path.abspath(path)
    try:
        # Pre-flight (PY-18): a header whose venv does not exist yet needs a real
        # download, which does not fit runPython's ~30s budget. Answer instead of
        # blocking — the page shows the install loader, POSTs /api/env/install and
        # retries. Only when there IS something to install: an existing venv runs
        # straight through, so the normal case pays one marker stat.
        #
        # Inside the guard, not before it, for the same reason build_code is:
        # `is_installed` -> `venv_key_for` reaches into `fused.agent_core...`
        # unguarded, and `_backend_attr` raises RuntimeError BY DESIGN when an
        # upstream private attribute disappears (routers/env.py catches exactly
        # that pair for its own calls). Escaping here made /api/run an unhandled
        # 500 whose body is `{"error": "<string>"}`, and runtime.js reads
        # `data.error.message` off that — so the diagnostic `_backend_attr` wrote
        # to be READ reached the user as the literal word `undefined`.
        #
        # Off the event loop, for the same reason `app_interpreter` above is: since
        # D212 this is not a single `os.path.exists` any more — the first call for a
        # given venv probes its interpreter with `subprocess.run(..., timeout=5)`.
        # /api/run awaits this coroutine directly (`routers/run.py`), so inline that
        # stalls the entire server — websockets, watcher, every other request — for
        # the probe's duration, and a venv on a wedged mount would stall it for the
        # full budget. `to_thread` re-raises in this frame, so the guard above still
        # contains the ImportError/RuntimeError pair (pinned by a test, because "the
        # exception now surfaces somewhere else" is exactly what a thread hop hides).
        # The app-interpreter fast path: a declaration this interpreter already
        # meets needs no venv, no download and no loader. See the `app_satisfies`
        # block above for why it fails closed and what it trades.
        #
        # Skipped entirely for a LOCKED project. A `uv.lock` is a request for
        # exact resolution — the user committed specific versions so the folder
        # resolves the same way on another machine — and satisfying it "near
        # enough" from whatever the app happens to ship is the one thing a lock
        # exists to rule out. Unlocked, the trade is the same one it always was:
        # skip a multi-hundred-MB download the app has already paid for.
        #
        # Inside the try and BEFORE the pre-flight, both deliberately.
        # `_interpreter_path_available` calls `get_backend()`, which imports the
        # backend and can raise — out here that would be an unhandled 500 rather
        # than the house wire shape. And declining the fast path has to fall through
        # to `is_installed` below, so a missing venv still becomes `needs_install`
        # instead of an inline download inside this request.
        #
        # Off the event loop for the same reason `app_interpreter` and
        # `is_installed` are: both probes behind it spawn a subprocess on their
        # first call.
        locked = bool(project) and projectenv.has_lock(project)
        if project and requirements and not locked and _interpreter_path_available():
            interpreter = await asyncio.to_thread(
                _app_interpreter_if_satisfies, requirements
            )

        # `interpreter is None` and not merely `project`: a declaration the app
        # interpreter already satisfies resolved one just above, and it has nothing
        # to install — asking `is_installed` about it would name a venv directory
        # that is never going to be built and answer `needs_install`, putting the
        # loader in front of a run that was ready to go.
        if project and interpreter is None:
            from fused_render import envinstall

            if not await asyncio.to_thread(envinstall.is_installed, project):
                return _needs_install_dict(project, abs_path=abs_path)
            # The environment lives under OUR home dir, not in the backend's
            # store, so the backend cannot find it by key — it is TOLD, through
            # the same `interpreter=` channel the app-interpreter path uses.
            # That is the whole reason `projectenv` may own the storage layout.
            interpreter = envinstall.venv_python_for(project)

        # build_code reads _binding.py's source off the package
        # (importlib.resources), so a broken/partial install fails here — and
        # every other failure in this function returns the house wire shape
        # rather than raising into the request handler as a 500.
        code = build_code(user_code, os.path.dirname(abs_path), abs_path)
        r = await _execute(
            code, requirements, interpreter,
            {"_params.json": json.dumps(params or {}).encode()},
        )
    except Exception:
        # The engine itself blew up (wrapper construction, backend import,
        # venv/dep resolution, subprocess spawn…) — not the user's script,
        # whose own failures come back in `r.error`. Return the same wire
        # shape as every other failure so the page's error overlay (D17)
        # shows the full traceback, and log it so the log file has it too.
        logger.exception("fused engine execute failed for %s", path)
        return _error_dict(
            "EngineError",
            f"fused-render internal error (not your script) while running {path}",
            traceback.format_exc(),
        )

    if r.error:
        cleaned = _clean_error(r.error, abs_path)
        err_type, message = _split_error(cleaned)
        return {
            "ok": False,
            "error": {"type": err_type, "message": message, "traceback": cleaned},
            "stdout": r.stdout,
            "stderr": r.stderr,
            "duration_ms": r.duration_ms,
        }

    # The backend hands return_value back JSON-encoded; decode it here so the
    # wire carries real values ({"x": 1}, not "{\"x\": 1}"). Base64 binary
    # bodies stay strings, and anything that isn't valid JSON passes through.
    # parse_constant: python's json accepts NaN/Infinity/-Infinity and would
    # decode them to floats that the response serializer re-emits as bare NaN,
    # which the browser's strict JSON.parse rejects — the whole /api/run
    # response would fail to parse. Decode them as their literal names instead.
    return_value = r.return_value
    if isinstance(return_value, str) and not (r.response and r.response.body_encoding == "base64"):
        try:
            return_value = json.loads(return_value, parse_constant=lambda c: c)
        except ValueError:
            pass
    return {
        "ok": True,
        "result": return_value,
        "stdout": r.stdout,
        "stderr": r.stderr,
        "duration_ms": r.duration_ms,
    }
