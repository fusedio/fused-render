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

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.server.common import _error, _require_fused

router = APIRouter()


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

    reqs = projectenv.dependencies_of(project)

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
