"""Execute a Python map target and classify the returned object.

The warm loopback service calls :func:`main` directly from this folder's geo
runtime (declared in `pyproject.toml`, SPEC PY-16 — see `geo_classify.py`'s
header). A one-shot CLI invocation remains as a degraded fallback
if that service cannot start. Raster paths are offered to ``RasterEngine``
first; vector and in-memory results then fall through to ``geo_classify``.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import time
import traceback
from typing import Any


ENTRYPOINTS = ("main", "run", "udf", "fn")
RESULT_VARS = ("result", "output", "layer", "gdf", "df")


def _load_module(path: str):
    directory = os.path.dirname(os.path.abspath(path))
    if directory not in sys.path:
        sys.path.insert(0, directory)
    spec = importlib.util.spec_from_file_location("map_viewer_target", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    module.__file__ = os.path.abspath(path)
    spec.loader.exec_module(module)
    return module


def _run_entrypoint(module: Any, preferred: str = "") -> tuple[Any, str]:
    names = ([preferred] if preferred else []) + list(ENTRYPOINTS)
    for name in names:
        function = getattr(module, name, None)
        if callable(function):
            return function(), name
    for name in RESULT_VARS:
        value = getattr(module, name, None)
        if value is not None and not callable(value):
            return value, name
    raise RuntimeError(
        "Target defines no main()/run()/udf()/fn() function and no "
        "result/output/layer/gdf/df value to render."
    )


def _declared_packages() -> list[str]:
    """This folder's declaration, READ rather than restated (D177).

    The first version of the message below listed six of the thirteen by hand,
    and the two it omitted were `duckdb` and `requests` — the two added to the
    manifest specifically so that user targets could import them. It told the
    reader to "rewrite the target using the packages above" while hiding the two
    most likely to save them. A hand-copied list of another list drifts; this one
    drifted before it was a day old.

    `tomllib` is stdlib from 3.11 and this manifest requires 3.12, so reading it
    costs no dependency and keeps the template standalone-copyable — it imports
    nothing from `fused_render` (SPEC PY-15). An unreadable manifest degrades to
    an empty list and a message that simply omits the enumeration, because a
    diagnostic must never be the thing that raises.
    """
    try:
        import tomllib

        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "pyproject.toml"), "rb") as handle:
            declared = tomllib.load(handle)["project"]["dependencies"]
    except Exception:  # noqa: BLE001 — a help string must not be able to fail
        return []
    names = []
    for requirement in declared:
        name = requirement.split(";")[0].split("[")[0]
        for separator in ("<", ">", "=", "!", "~", " ", "("):
            name = name.split(separator)[0]
        if name.strip():
            names.append(name.strip())
    return names


def _missing_module_help(error: ModuleNotFoundError) -> str:
    """Explain the one thing about this process a user cannot guess.

    A Python map target is loaded and executed IN THIS PROCESS (`_load_module`
    above), because the descriptor is built from the live object it returns —
    not from JSON, so there is no process boundary to put between them. That
    process used to be the app's own interpreter, carrying everything `[bundled]`
    promised user code. Since D275 it is this template's environment
    (`map/pyproject.toml`, SPEC PY-16), which contains exactly what that file
    declares and nothing else — so a target importing anything outside it fails
    here, and only here.

    The bare error says `No module named 'x'`, which is true and sends the
    reader looking in the wrong place: their own folder's `pyproject.toml` is not
    consulted for this call, so nothing they can write near their script fixes
    it. Naming the environment, and what is IN it, is the difference between a
    puzzle and a decision.

    A message, not a repair. `duckdb` and `requests` were added to the manifest
    to buy back the common case (D275), but mirroring the app's whole set there
    would be a hand-kept second copy of `[bundled]` inside a template (D177) that
    could never be complete — a map target may import anything. The real fix is
    for the target to run under the environment PY-16 gives the USER's folder,
    which needs the live object to cross a process boundary.
    """
    name = error.name or "a package"
    available = _declared_packages()
    listing = (" — " + ", ".join(available)) if available else ""
    return (
        f"{error}. A Python map target runs inside the Map Viewer's own "
        f"environment (fused_render/templates/map/pyproject.toml{listing}), "
        f"NOT the app's interpreter and not your script's folder, so {name!r} is "
        f"not available to it and a pyproject.toml beside your script will not "
        f"change that. Either rewrite the target using the packages above, or "
        f"precompute with {name!r} in a separate script and point the Map Viewer "
        f"at the file it writes."
    )


def build(
    request: dict[str, Any],
    raster_engine=None,
    vector_engine=None,
) -> dict[str, Any]:
    import geo_classify

    target = request["target"]
    artifact_dir = request["artifact_dir"]
    artifact_id = request["artifact_id"]
    options = request.get("opts") or {}

    logs = io.StringIO()
    entrypoint = None
    with contextlib.redirect_stdout(logs), contextlib.redirect_stderr(logs):
        if isinstance(target, str) and target.lower().endswith(".py"):
            try:
                module = _load_module(target)
                obj, entrypoint = _run_entrypoint(
                    module, str(options.get("entrypoint") or "")
                )
            except ModuleNotFoundError as missing:
                raise ModuleNotFoundError(_missing_module_help(missing)) from missing
        else:
            obj = target

        descriptor = (
            raster_engine.try_describe(request, obj=obj)
            if raster_engine is not None
            else None
        )
        if descriptor is None and vector_engine is not None:
            descriptor = vector_engine.try_describe(request, obj=obj)
        if descriptor is None:
            descriptor = geo_classify.classify(
                obj, artifact_dir, artifact_id, options
            )

    descriptor["logs"] = logs.getvalue()[-8000:]
    descriptor["entrypoint"] = entrypoint
    return descriptor


def main(
    request: dict[str, Any],
    raster_engine=None,
    vector_engine=None,
) -> dict[str, Any]:
    started = time.time()
    try:
        descriptor = build(
            request,
            raster_engine=raster_engine,
            vector_engine=vector_engine,
        )
    except Exception as error:
        descriptor = {
            "id": request.get("artifact_id", ""),
            "status": "error",
            "kind": None,
            "bounds": None,
            "data": {},
            "message": f"{type(error).__name__}: {error}",
            "error": {
                "message": str(error),
                "traceback": traceback.format_exc(),
            },
            "detected_type": None,
            "warnings": [],
            "logs": "",
        }
    descriptor["timing_ms"] = int((time.time() - started) * 1000)
    return descriptor


if __name__ == "__main__":
    try:
        payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
        print(json.dumps(main(payload)))
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "kind": None,
                    "bounds": None,
                    "data": {},
                    "message": f"{type(error).__name__}: {error}",
                    "error": {
                        "message": str(error),
                        "traceback": traceback.format_exc(),
                    },
                }
            )
        )
