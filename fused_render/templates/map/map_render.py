"""FusedRender entry point for the built-in Map Viewer template.

The shell invokes this module in a short-lived worker inside the map template's
project venv — the interpreter that holds the geo stack. Heavy geospatial work
and XYZ raster tiles live in one loopback daemon that the fused-render server
owns as a generic managed engine (fused_render/server/engine_host.py): this
module hands the server its own sys.executable over /api/engines/map/ensure,
then describes through the engine proxy and rewrites the descriptor's tile URLs
to stable /api/engines/map/proxy/... paths on the server origin, so a daemon
death or restart never invalidates a URL the page holds.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "map_render.py")

HERE = Path(__file__).resolve().parent
SHARED = HERE.parent / "shared"
for _path in (str(HERE), str(SHARED)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from geo_paths import (
    base_home,
    is_managed_mount,
    is_remote_path,
    multidim_suffix,
    normalize_remote_path,
)

CACHE_DIR = Path(
    os.environ.get("FUSED_RENDER_MAP_CACHE", base_home() / "cache" / "map-v2")
).expanduser()
ARTIFACT_DIR = CACHE_DIR / "artifacts"
DAEMON = HERE / "daemon.py"
WORKER = HERE / "worker.py"
SERVICE_START_TIMEOUT = 120
# The server hosts this template's daemon under a generic engine id
# (fused_render/server/engine_host.py); tiles ride /api/engines/map/proxy/...
ENGINE_ID = "map"
PROXY_BASE = f"/api/engines/{ENGINE_ID}/proxy"
# The daemon emits absolute child URLs for these keys; each is rewritten to a
# stable proxy path here, since their shape is this template's knowledge.
_URL_KEYS = ("tile_url", "vtile_url", "job_url", "optimize_url")
# Every module the daemon imports, because VERSION is a hash of these and a
# module left out here can be edited without the running daemon being retired —
# it would keep serving the old code. test_map_daemon.py walks the imports.
BACKEND_FILES = (
    DAEMON,
    WORKER,
    HERE / "raster_engine.py",
    HERE / "multidim_engine.py",
    HERE / "vector_engine.py",
    HERE / "mvt_encode.py",
    HERE / "geo_classify.py",
    HERE / "geo_paths.py",
    HERE / "raster_categories.py",
    HERE / "blob_tokens.py",
    HERE / "optional_runtime.py",
)
# The GDAL-read formats only. Multidim stores are recognized by
# `multidim_suffix`, which is the one list that knows their spellings.
RASTER_SUFFIXES = (
    ".tif", ".tiff", ".cog", ".vrt", ".jp2", ".j2k", ".img", ".ntf",
    ".nitf", ".dem", ".dt0", ".dt1", ".dt2", ".hgt", ".grd",
)
VECTOR_SUFFIXES = (
    ".geojson", ".json", ".shp", ".gpkg", ".fgb", ".kml", ".gml",
)
VECTOR_ONESHOT_MAX_BYTES = 32 << 20
QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’"}


def _clean_target(value: str) -> str:
    target = str(value or "").strip()
    while (
        len(target) >= 2
        and target[0] in QUOTE_PAIRS
        and target[-1] == QUOTE_PAIRS[target[0]]
    ):
        target = target[1:-1].strip()
    if is_remote_path(target):
        return normalize_remote_path(target)
    return os.path.expandvars(os.path.expanduser(target))


def _looks_like_raster(target: str) -> bool:
    # multidim_suffix also recognizes the zarr spellings a plain suffix check
    # misses: store.zarr/, .zmetadata, zarr.json, .zarr-v3.
    if multidim_suffix(target):
        return True
    return target.lower().split("?", 1)[0].endswith(RASTER_SUFFIXES)


def _requires_vector_service(target: str, source_url: str = "") -> bool:
    normalized = target.lower().split("?", 1)[0]
    if not normalized.endswith(VECTOR_SUFFIXES):
        return False
    if is_remote_path(target) or is_managed_mount(target):
        return True
    if os.path.isfile(target):
        try:
            return os.path.getsize(target) >= VECTOR_ONESHOT_MAX_BYTES
        except OSError:
            return False
    if source_url:
        return True
    try:
        return os.path.getsize(target) >= VECTOR_ONESHOT_MAX_BYTES
    except OSError:
        return False


def _backend_version() -> str:
    digest = hashlib.sha256()
    for path in BACKEND_FILES:
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(str(path).encode("utf-8"))
    return digest.hexdigest()[:12]


VERSION = _backend_version()


def _process_options() -> dict:
    options: dict = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    return options


def _server_post(path: str, payload: dict, timeout: float,
                 headers: dict | None = None) -> dict:
    origin = os.environ.get("FUSED_RENDER_ORIGIN", "")
    if not origin:
        raise RuntimeError("FUSED_RENDER_ORIGIN is not set")
    request = urllib.request.Request(
        origin.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Fused": "1", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise RuntimeError(f"map engine HTTP {error.code}: {detail}") from error


def _ensure_service() -> dict:
    """Ask the server to have the managed daemon running, on THIS interpreter —
    the project venv is the only one on the machine holding the geo stack."""
    return _server_post(
        f"/api/engines/{ENGINE_ID}/ensure",
        {
            "python": sys.executable,
            "daemon": str(DAEMON),
            "cache": str(CACHE_DIR),
            "version": VERSION,
        },
        timeout=SERVICE_START_TIMEOUT,
    )


def _stable_url(url: str) -> str:
    """Rewrite a child's absolute URL to a stable server-origin proxy path,
    dropping the ephemeral origin and the token the browser must never see."""
    return PROXY_BASE + urlsplit(url).path


def _describe_service(request: dict) -> dict:
    """Describe through the proxy, then rewrite the descriptor's live URLs to
    stable paths, so a daemon restart is invisible to the page. The URL shape is
    this template's knowledge, so the rewrite lives here rather than in the
    generic engine host.

    Replay registration rides the describe request itself (X-Engine-Reinit): the
    server records it atomically when the child accepts the describe, so it can
    never be lost to a separate call. The key is a digest of the request, stable
    across re-describes of the same layer; the page forgets by it (reinit_key)."""
    reinit_key = hashlib.sha256(
        json.dumps(request, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]
    descriptor = _server_post(
        f"{PROXY_BASE}/describe", request, timeout=300,
        headers={"X-Engine-Reinit": reinit_key},
    )
    data = descriptor.get("data") if isinstance(descriptor, dict) else None
    if not isinstance(data, dict):
        return descriptor
    for key in _URL_KEYS:
        url = data.get(key)
        if isinstance(url, str) and url.startswith("http"):
            data[key] = _stable_url(url)
    if descriptor.get("status") == "ok" and data.get("source_id"):
        data["reinit_key"] = reinit_key
    return descriptor


def _artifact_exists(descriptor: dict) -> bool:
    if descriptor.get("status") != "ok":
        return False
    if descriptor.get("kind") in {"raster_tiles", "vector_tiles_mvt"}:
        return False
    data = descriptor.get("data") or {}
    for name in ("geojson_path", "image_path", "points_path"):
        if name in data and not os.path.exists(data[name]):
            return False
    return True


def _cached_descriptor(path: Path) -> dict | None:
    try:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
        return descriptor if _artifact_exists(descriptor) else None
    except (OSError, ValueError):
        return None


def _save_descriptor(path: Path, descriptor: dict) -> None:
    if (
        descriptor.get("status") != "ok"
        or descriptor.get("kind") in {"raster_tiles", "vector_tiles_mvt"}
    ):
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(descriptor), encoding="utf-8")
    os.replace(temporary, path)


def _run_oneshot(request: dict) -> dict:
    command = [sys.executable, str(WORKER), json.dumps(request)]
    options = _process_options()
    options.pop("stdin", None)
    completed = subprocess.run(
        command,
        cwd=HERE,
        capture_output=True,
        text=True,
        timeout=300,
        **options,
    )
    output = (completed.stdout or "").strip()
    if not output:
        raise RuntimeError(
            f"worker produced no output (exit {completed.returncode}): "
            f"{(completed.stderr or '')[-1200:]}"
        )
    return json.loads(output.splitlines()[-1])


def main(
    target: str = "",
    colormap: str = "viridis",
    rescale: str = "",
    entrypoint: str = "",
    var: str = "",
    sel: str = "",
    warmup: str = "",
    source_url: str = "",
    source_origin: str = "",
    render_mode: str = "",
    category_colors: str = "",
    stretch: str = "",
):
    if warmup:
        try:
            _ensure_service()
            return {
                "status": "warm",
                "daemon": True,
                "version": VERSION,
            }
        except Exception as error:
            return {
                "status": "error",
                "daemon": False,
                "message": f"{type(error).__name__}: {error}",
            }

    target = _clean_target(target)
    source_url = _clean_target(source_url)
    if not target:
        return {
            "status": "error",
            "message": "No target selected.",
            "kind": None,
            "bounds": None,
            "data": {},
            "warnings": [],
        }

    is_url = is_remote_path(target)
    if not is_url:
        target = os.path.abspath(os.path.expanduser(target))
        if not source_url and not os.path.exists(target):
            return {
                "status": "error",
                "message": f"Not found: {target}",
                "kind": None,
                "bounds": None,
                "data": {},
                "warnings": [],
            }

    options: dict[str, object] = {
        "colormap": colormap,
        "entrypoint": entrypoint,
        "var": var,
    }
    if sel:
        try:
            options["sel"] = json.loads(sel)
        except ValueError:
            pass
    if rescale:
        try:
            lo, hi = (float(value) for value in rescale.split(","))
            options["rescale"] = [lo, hi]
        except ValueError:
            pass
    if render_mode:
        options["render_mode"] = render_mode
    if stretch:
        options["stretch"] = stretch
    if category_colors:
        try:
            options["category_colors"] = json.loads(category_colors)
        except ValueError:
            pass

    options_json = json.dumps(options, sort_keys=True)
    local_fingerprint = ""
    if not is_url and os.path.isfile(target):
        stat = os.stat(target)
        local_fingerprint = f"{stat.st_size}:{stat.st_mtime_ns}"
    artifact_id = hashlib.sha256(
        (
            f"{VERSION}|{target}|{source_url}|{local_fingerprint}|"
            f"{options_json}"
        ).encode("utf-8")
    ).hexdigest()[:16]
    cache_path = CACHE_DIR / f"desc_{artifact_id}.json"
    cached = _cached_descriptor(cache_path)
    if cached:
        return cached

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    request = {
        "target": target,
        "source_url": source_url,
        "source_origin": source_origin,
        "artifact_dir": str(ARTIFACT_DIR),
        "artifact_id": artifact_id,
        "opts": options,
    }
    try:
        _ensure_service()
        descriptor = _describe_service(request)
    except Exception as error:
        if _looks_like_raster(target):
            return {
                "id": artifact_id,
                "status": "error",
                "kind": None,
                "bounds": None,
                "data": {},
                "warnings": [],
                "detected_type": "raster",
                "message": (
                    "The range-first raster service is unavailable. "
                    "The unsafe one-shot fallback was not used; retrying will "
                    f"start a fresh service. {type(error).__name__}: {error}"
                ),
            }
        if _requires_vector_service(target, source_url):
            return {
                "id": artifact_id,
                "status": "error",
                "kind": None,
                "bounds": None,
                "data": {},
                "warnings": [],
                "detected_type": "vector",
                "message": (
                    "The bounded vector-tile service is unavailable. The "
                    "whole-file one-shot fallback was not used; retrying will "
                    f"start a fresh service. {type(error).__name__}: {error}"
                ),
            }
        descriptor = _run_oneshot(request)
    _save_descriptor(cache_path, descriptor)
    return descriptor
