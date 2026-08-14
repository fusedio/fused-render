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
            module = _load_module(target)
            obj, entrypoint = _run_entrypoint(
                module, str(options.get("entrypoint") or "")
            )
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
