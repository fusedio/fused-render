"""Runs a Python file through the fused local compute backend, when installed.

Re-introduction of the D55-era engine (rolled back in D67) with a different
posture (D69): the fused engine is **optional**. When the `fused` package is
importable, /api/run executes code through `LocalPythonComputeBackend` —
fresh subprocess per call in a temp exec dir, params delivered via
`_params.json`. When it is not installed, the built-in executor
(`executor.py`/`_child.py`) runs unchanged. `available()` is the probe;
`server.py` picks per process.

Which interpreter a script gets is decided by its PEP 723 header (D172):

  * **no header** -> the app's own python, no venv (`app_interpreter()`), so
    `[bundled]` + the core `dependencies` are there with nothing to install;
  * **a header** -> a cached venv containing exactly what it declares. If that
    venv doesn't exist yet, /api/run answers `needs_install` instead of blocking
    on the download — see `envinstall.py` (PY-18).

Code contract under this engine (the fused contract, plus a compat bridge):

  * a function decorated with ``@fused.udf`` — **any name**; the last decorated
    one is the entrypoint and receives params as raw JSON values (no
    annotation coercion: the calling JS owns types);
  * or a plain script that assigns ``result = ...``;
  * or — compat bridge, so pages and the built-in templates run identically
    under either engine — a bare ``main()``, called with the same
    annotation-driven string coercion the built-in executor applies.

The wire shape returned here is the built-in executor's
``{ok, result, error: {type, message, traceback}, stdout}`` (plus additive
``stderr``/``duration_ms`` keys), so runtime.js and every template consume one
shape regardless of which engine ran the code.
"""
import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import traceback

logger = logging.getLogger(__name__)

# PEP 723 reference regex (verbatim from the spec) for inline script metadata.
_PEP723_BLOCK = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)

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
# A script with NO PEP 723 header runs with `interpreter=<this app's python>`
# and gets no venv at all: the app already ships `[bundled]` + its core
# `dependencies`, so numpy/pandas/duckdb/rasterio/… are there for free, with no
# download and no first-run wait. A script WITH a header keeps the venv path,
# and that venv contains exactly what the header declares — a header means what
# PEP 723 says it means (the script's complete dependency list), not a delta
# against an invisible baseline.
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
    and site-packages — findable. Prefer the value we were launched with; fall
    back to `sys.prefix`, which equals it whenever PYTHONHOME is in effect.

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
            # Write-then-rename so a concurrent reader never sees a partial script.
            tmp = f"{path}.{os.getpid()}.tmp"
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

    Cached per process (each probe is a subprocess); `reset_app_interpreter_cache`
    clears it.
    """
    global _app_interpreter
    if _app_interpreter is not _UNPROBED:
        return _app_interpreter

    candidate, autodetected = _interpreter_candidate()
    name = os.path.basename(candidate).lower().removesuffix(".exe")
    if autodetected and not name.startswith(_PYTHON_BASENAMES_PREFIX):
        # Rejected WITHOUT spawning it — see _PYTHON_BASENAMES_PREFIX.
        _app_interpreter = None
        logger.error(
            "%r cannot be this app's interpreter: its name %r is not an "
            "interpreter's, so it was not run. Set %s to a real python.",
            candidate, name, _APP_PYTHON_ENV,
        )
        return None

    info, detail = _probe(candidate)
    if info is not None and info["prefix"] == sys.prefix:
        logger.info("header-less scripts will run on %s", candidate)
        _app_interpreter = candidate
        return _app_interpreter

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
            _app_interpreter = wrapper
            return _app_interpreter
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
    _app_interpreter = None
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


def get_backend():
    # Lazy singleton: importing the backend pulls in the fused package tree,
    # and constructing it is only needed once per server process. 60s matches
    # the built-in executor's per-run timeout (executor.DEFAULT_TIMEOUT) — a
    # cold overview read of a large remote COG legitimately takes ~30-40s, so
    # the two engines must agree or the cold pyramid analyze dies at 30s here.
    global _backend
    if _backend is None:
        from fused.agent_core.backends.local.python_compute import LocalPythonComputeBackend

        from fused_render.executor import DEFAULT_TIMEOUT

        # cache_storage=None disables result caching explicitly (PY-9: fresh
        # execution every call). It is the upstream default today, but we may
        # track a nightly wheel — don't rely on a default staying put.
        _backend = LocalPythonComputeBackend(
            timeout_seconds=int(DEFAULT_TIMEOUT), cache_storage=None
        )
    return _backend


async def _execute(code: str, requirements: list[str], interpreter: str | None, input_files: dict):
    """Run `code` on the backend, on `interpreter` when one was resolved.

    The venv path (a script with a PEP 723 header) goes through the public
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


def _marker_applies(requirement: str) -> bool:
    """Does this PEP 508 requirement's environment marker hold here?

    A requirement with no marker always applies. This is what lets one template
    declare a dependency **only where the app doesn't already ship it**:

        dependencies = ["python-pptx; sys_platform == 'darwin'"]

    which is not a micro-optimisation but the difference between correct and
    wrong per platform. `[bundled]` is installed wholesale into the Linux
    AppImage and the Windows installer, while macOS's py2app build copies only
    what `setup_py2app.py` names and deliberately omits the heavy ones — so
    "does the app have python-pptx?" genuinely has different answers, and a
    header that ignored that would make Linux and Windows build a venv (and
    re-download 24 MB) for something already on their interpreter.

    An unparseable or unevaluatable marker is treated as APPLYING: the dependency
    then gets installed where it might not have been needed, which is wasteful.
    Guessing the other way would drop a dependency the script really needs and
    fail at import — the worse of the two.
    """
    if ";" not in requirement:
        return True
    marker = requirement.split(";", 1)[1].strip()
    if not marker:
        return True
    try:
        from packaging.markers import InvalidMarker, Marker
    except ImportError:
        return True
    try:
        return bool(Marker(marker).evaluate())
    except (InvalidMarker, KeyError, ValueError):
        logger.warning(
            "could not evaluate the environment marker %r in a `# /// script` "
            "dependency; treating it as applying", marker,
        )
        return True


def script_requirements(text: str, *, apply_markers: bool = True) -> list[str]:
    """Extract PEP 723 `dependencies` from a script's inline metadata block.

    Requirements whose PEP 508 environment marker does not hold on this platform
    are dropped (see `_marker_applies`), so a script that declares something only
    macOS is missing reads as header-LESS on Linux and Windows and runs straight
    on their interpreters.

    `apply_markers=False` returns the block verbatim, markers included — for
    tooling that must reason about ALL platforms rather than this one (the
    packaging invariant in tests/test_bundle_contents.py).

    Returns [] when there is no `# /// script` block. Malformed TOML raises
    ValueError with the parse error so the caller can surface it to the page
    instead of 500ing.
    """
    for match in _PEP723_BLOCK.finditer(text):
        if match.group("type") != "script":
            continue
        # Imported here, not at function top: tomllib is 3.11+, but this
        # function must still return [] on 3.10 for the (overwhelmingly
        # common) case of a script with no PEP 723 block at all — run_python
        # calls this unconditionally, regardless of which engine is active.
        import tomllib

        content = "".join(
            line[2:] if line.startswith("# ") else line[1:]
            for line in match.group("content").splitlines(keepends=True)
        )
        try:
            meta = tomllib.loads(content)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(
                f"invalid TOML in '# /// script' block: {e}. "
                "Fix the inline metadata header (PEP 723) or remove the block."
            ) from None
        deps = meta.get("dependencies", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise ValueError(
                "'dependencies' in the '# /// script' block must be a list of strings"
            )
        if not apply_markers:
            return deps
        return [d for d in deps if _marker_applies(d)]
    return []


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
    frame carries the real file and exact line (no offset bookkeeping). The
    wrapper must NOT chdir before the user code runs: the backend's runner
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


def _needs_install_dict(requirements: list[str], abs_path: str) -> dict:
    """The pre-flight answer for a header whose venv isn't built yet (PY-18).

    Carries `needs_install` for the loader AND a populated `error` object, so a
    client that knows nothing about the loader (an older page, a direct API
    caller, the Calls log) still shows a real message naming the packages rather
    than an undefined field.
    """
    from fused_render import envinstall

    return {
        "ok": False,
        "needs_install": {
            "key": envinstall.venv_key_for(requirements),
            "requirements": requirements,
            "py": abs_path,
        },
        "error": {
            "type": "EnvNotInstalled",
            "message": (
                f"{os.path.basename(abs_path)} declares dependencies that are not "
                f"installed yet: {', '.join(requirements)}. They need a one-time "
                "download."
            ),
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

    try:
        reqs = script_requirements(user_code)
    except ValueError as e:
        return _error_dict("ValueError", str(e))

    # Sorted+deduped so the venv cache key is stable regardless of how a script
    # orders its PEP 723 block. A header is the script's COMPLETE dependency
    # list: no baseline is unioned in (D172), so what it declares is what its
    # venv contains.
    requirements = sorted(set(reqs))

    # No header -> the app's own interpreter, no venv (PY-17). `interpreter` and
    # `requirements` are mutually exclusive upstream (the interpreter branch
    # ignores requirements silently), so they are never both set here.
    interpreter = None
    if not requirements:
        interpreter = app_interpreter()
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

    # Pre-flight (PY-18): a header whose venv does not exist yet needs a real
    # download, which does not fit runPython's ~30s budget. Answer instead of
    # blocking — the page shows the install loader, POSTs /api/env/install and
    # retries. Only when there IS something to install: an existing venv runs
    # straight through, so the normal case pays one marker stat.
    if requirements:
        from fused_render import envinstall

        if not envinstall.is_installed(requirements):
            return _needs_install_dict(requirements, abs_path=os.path.abspath(path))

    abs_path = os.path.abspath(path)
    try:
        # Inside the guard, not before it: build_code reads _binding.py's source
        # off the package (importlib.resources), so a broken/partial install
        # fails here — and every other failure in this function returns the house
        # wire shape rather than raising into the request handler as a 500.
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
