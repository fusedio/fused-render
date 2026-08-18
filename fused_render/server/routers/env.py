"""The project-venv install loader (PY-16 / PY-18).

/api/run's pre-flight answers `needs_install` for a script whose PROJECT FOLDER
declares packages that are not installed yet, instead of blocking on a download
that cannot fit runPython's ~30s budget. These three endpoints are what the
page shell's loader then drives: start it, watch it, stop it.

The project is always re-derived from the .py on disk here, never taken from
the request: the key the loader fills has to be the key the run then looks for,
and one source for both is the only way that stays true.
"""

import os
import sys

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.server.common import _error, _require_fused

router = APIRouter()

# The bundled data-stack packages a first-party reader (duckdb/xlsx/sqlite/
# structure) actually uses. Reported by /api/env/interpreter so a caller (a
# Claude Code session embedded beside a file preview, in particular) can learn
# the real versions instead of shelling out to a fresh interpreter that has no
# relationship to the one that rendered the file.
_REPORTED_PACKAGES = ("duckdb", "pyarrow", "pandas")


def _project_for(body: dict):
    """(project_dir, error_response) for a {py, html} body, or (None, resp).

    Resolved through `projectenv`, the same call `run_python` makes, so the key
    this endpoint installs under is by construction the key the run looks for.
    None means the file is in no project that declares an environment.
    """
    py, html = body.get("py"), body.get("html")
    if not py:
        return None, _error("request body must include 'py': a path to a Python file")
    if os.path.isabs(py):
        resolved = py
    elif html:
        resolved = os.path.normpath(os.path.join(os.path.dirname(html), py))
    else:
        return None, _error(
            "'py' is a relative path but 'html' was not provided; "
            "either send an absolute 'py' path or include 'html' so it can be resolved"
        )
    if not os.path.isfile(resolved):
        return None, _error(f"no such Python file: {resolved}")
    from fused_render import projectenv

    return projectenv.project_env_for(resolved), None


@router.post("/api/env/install")
def api_env_install(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    project, err = _project_for(body)
    if err is not None:
        return err
    if not project:
        return _error(
            f"{os.path.basename(body.get('py', ''))} is not in a folder with a "
            "pyproject.toml declaring dependencies, so there is nothing to "
            "install — it runs on this app's own interpreter"
        )
    from fused_render import envinstall, projectenv

    # The APPLICABLE ones, matching what `/api/run`'s `needs_install` reported and
    # what `uv sync` will actually install. Reporting the raw declaration here
    # would let this endpoint's answer disagree with the pre-flight's over a
    # marker-scoped dependency.
    reqs = projectenv.applicable_dependencies_of(project)

    # envinstall speaks to the fused backend: `_backend_attr` raises RuntimeError
    # BY DESIGN when an upstream attribute is missing (guessing would build the
    # environment on the wrong interpreter), and reaching it imports
    # `fused.agent_core...`. Both are reachable here without the fused engine — a
    # page loaded before the engine preference was switched, or any direct API
    # call — and uncaught they become a 500 the loader shows as a bare
    # "HTTP 500", discarding the diagnostic that was the whole point.
    try:
        record = envinstall.start(project)
        # The key comes back FROM `start`, never recomputed here: when this machine
        # has no pinned Python yet the install reports under
        # `envinstall.PYTHON_BOOTSTRAP_KEY` rather than the venv key (D214), and a
        # second derivation would hand the page a key with no record behind it.
        key = record["key"]
    except (ImportError, RuntimeError) as e:
        return _error(str(e))
    return JSONResponse({"ok": True, "key": key, "project": project,
                         "requirements": reqs, "progress": record})


# `key` comes straight off the wire and becomes a path component, so its
# shape is checked before anything touches the filesystem. _require_fused is
# not a containment boundary — it only blocks blind cross-origin POSTs, and
# the pages this app renders are same-origin by design. envinstall refuses a
# bad key too; this exists so the caller gets a 400 saying why rather than a
# silent "no such install".
def _checked_key(key):
    from fused_render import envinstall

    if not envinstall.valid_key(key):
        return None, _error(
            "'key' is not a valid install key (expected 16 lowercase hex "
            "characters, as returned by /api/run's needs_install)"
        )
    return key, None


@router.get("/api/env/progress")
def api_env_progress(key: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    key, err = _checked_key(key)
    if err is not None:
        return err
    from fused_render import envinstall

    # Same containment as /api/env/install: the error shape, not a 500.
    try:
        prog = envinstall.progress(key)
    except (ImportError, RuntimeError) as e:
        return _error(str(e))
    return JSONResponse({"ok": True, "key": key, "progress": prog})


@router.get("/api/env/interpreter")
def api_env_interpreter(py: str | None = None, html: str | None = None,
                        x_fused: str | None = Header(default=None)):
    """Which interpreter actually runs *py* — the ground truth PY-16/PY-17
    otherwise has no way to reach a caller outside this process.

    Without `py` (the common case: a data file with no project of its own —
    every core template, and most quick-look files) this answers for THE APP'S
    OWN interpreter, which is what ran it: the built-in duckdb/xlsx/sqlite/
    structure readers execute in-process (D72), and everything else without a
    declared project runs as a subprocess of this same interpreter
    (executor.py's `[sys.executable, CHILD]`) — so this process's own
    `sys.executable` and its already-installed duckdb/pyarrow/pandas are
    exactly what a reader used, not a guess.

    This exists because a shell probe (`which python3`, a fresh `sys.
    executable`) run from outside this app — e.g. a Claude Code session
    embedded beside a file's preview — reports whatever happens to be first on
    THAT SHELL'S PATH, which has no relationship to the interpreter
    fused-render itself used to render the file. That mismatch is silent: the
    shell probe always succeeds, it just answers a different question.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    project = None
    if py:
        resolved = py if os.path.isabs(py) else (
            os.path.normpath(os.path.join(os.path.dirname(html), py))
            if html else None)
        if resolved and os.path.isfile(resolved):
            from fused_render import projectenv
            project = projectenv.project_env_for(resolved)

    from fused_render.shell import prefs as shell_prefs
    engine = shell_prefs.effective_engine()

    packages = None
    if project:
        from fused_render import envinstall
        interpreter = envinstall.venv_python_for(project)
        source = (f"{projectenv.display_name(project)}'s own project "
                  "environment (declared in its pyproject.toml)")
        # Not this process's own packages — that venv is a different
        # interpreter, and probing it here would cost a subprocess spawn for
        # an answer most callers (no declared project) never need.
    else:
        if engine == "fused":
            from fused_render import engine as _engine
            interpreter = _engine.app_interpreter()
        else:
            interpreter = sys.executable
        source = "this app's own interpreter (no project declares one, SPEC PY-17)"
        packages = {}
        for name in _REPORTED_PACKAGES:
            try:
                packages[name] = getattr(__import__(name), "__version__", None)
            except ImportError:
                pass

    return JSONResponse({
        "ok": True,
        "engine": engine,
        "interpreter": interpreter,
        "source": source,
        "python_version": sys.version,
        "packages": packages,
    })


@router.post("/api/env/cancel")
def api_env_cancel(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    key, err = _checked_key(body.get("key"))
    if err is not None:
        return err
    from fused_render import envinstall

    # Same containment as /api/env/install: the error shape, not a 500.
    try:
        killed = envinstall.cancel(key)
        prog = envinstall.progress(key)
    except (ImportError, RuntimeError) as e:
        return _error(str(e))
    return JSONResponse({"ok": True, "key": key, "cancelled": killed,
                         "progress": prog})
