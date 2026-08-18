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


@router.get("/api/env/custom-env")
def api_env_custom_env(file: str, x_fused: str | None = Header(default=None)):
    """Does *file*'s own reader run on a declared project environment, or on
    this app's own bundled interpreter?

    A Claude Code session embedded beside a file's preview already knows its
    OWN interpreter (`sys.executable` — the claude template's folder never
    declares a project, so agent.py's own process always runs on the app's own
    interpreter, SPEC PY-17). What it cannot know on its own is whether THAT
    matches what actually reads `file`: a `.py` file may sit inside a project
    declaring dependencies (SPEC PY-16), and some core templates ship their own
    `pyproject.toml` too (D276 — `map`/`vector`/`pdf_studio`/…, moved out of
    the bundled app on purpose), in which case the file's real reader runs on
    a dedicated venv instead. Guessing which templates those are would drift
    the moment a new one adds its own deps, so this asks the two real sources
    instead of hardcoding a list:

    * a `.py` file IS the script `/api/run` would execute, so its own
      `project_env_for` is the direct, exact answer.
    * anything else is served by whichever template's reader the registry
      resolves for it (`server/templates._templates_for`, the same match
      `/render` uses) — the FIRST (default, SPEC PT-7) entry's folder is
      checked for a declared environment. A conditional template that gates
      out the default at request time is a known, accepted imprecision here;
      this is advisory, not a guarantee.

    `custom_env: true` means "don't trust the session's own interpreter for
    this file" (a declared project, an unresolvable file, or a file type this
    app has no template for at all — safest default when unsure). `false`
    means the session's own interpreter genuinely is what ran it.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not os.path.exists(file):
        return _error(f"no such file or directory: {file}")

    from fused_render import projectenv

    is_dir = os.path.isdir(file)
    # Case-INsensitive, matching _match_registry's own basename lowercasing
    # below: a `script.PY` must take the same direct-project_env_for path a
    # `script.py` does, or it falls through to the registry's "code" template
    # (no pyproject.toml of its own) and reports `custom_env: false` for a
    # file that may actually run on a project's venv.
    if not is_dir and file.lower().endswith(".py"):
        custom_env = projectenv.project_env_for(file) is not None
        return JSONResponse({"ok": True, "custom_env": custom_env})

    from fused_render.server import templates as _server_templates

    entries, _template_error = _server_templates._templates_for(file, is_dir)
    default_path = entries[0].get("path") if entries else None
    if not default_path:
        # No template resolved (unmapped file type) or the default entry is a
        # sentinel with no folder (e.g. `_render`) — nothing to check, so the
        # safe answer is "don't know, don't trust it."
        return JSONResponse({"ok": True, "custom_env": True})
    folder = os.path.dirname(default_path)
    return JSONResponse({"ok": True,
                         "custom_env": projectenv.has_project_env(folder)})


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
