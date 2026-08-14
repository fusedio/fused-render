"""FusedRender entry point for the built-in Map Viewer template.

The shell invokes this module in a short-lived worker. Heavy geospatial work
and XYZ raster tiles live in one cross-platform loopback service running from
FusedRender's bundled Python. Windows launches use CREATE_NO_WINDOW, so a
service crash cannot produce recurring terminal windows.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote


if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "map_render.py")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from geo_paths import is_managed_mount, is_remote_path, normalize_remote_path

CACHE_DIR = Path(
    os.environ.get(
        "FUSED_RENDER_MAP_CACHE",
        Path.home() / ".fused-render" / "cache" / "map-v2",
    )
).expanduser()
ARTIFACT_DIR = CACHE_DIR / "artifacts"
DAEMON = HERE / "daemon.py"
WORKER = HERE / "worker.py"
LOG = CACHE_DIR / "daemon.log"
START_LOCK = CACHE_DIR / "daemon-start.lock"
SERVICE_START_TIMEOUT = 120
START_LOCK_STALE_AFTER = SERVICE_START_TIMEOUT + 30
FOLLOWER_WAIT_TIMEOUT = SERVICE_START_TIMEOUT + 10
BACKEND_FILES = (
    DAEMON,
    WORKER,
    HERE / "raster_engine.py",
    HERE / "vector_engine.py",
    HERE / "geo_classify.py",
    HERE / "geo_paths.py",
    HERE / "raster_categories.py",
)
RASTER_SUFFIXES = (
    ".tif", ".tiff", ".cog", ".vrt", ".jp2", ".j2k", ".img", ".ntf",
    ".nitf", ".dem", ".dt0", ".dt1", ".dt2", ".hgt", ".grd", ".nc",
    ".hdf", ".h5",
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
STATE = CACHE_DIR / f"daemon-{VERSION}.json"


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


def _read_state() -> dict | None:
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
        return value if value.get("version") == VERSION else None
    except (OSError, ValueError):
        return None


def _service_url(state: dict, path: str) -> str:
    token = quote(str(state.get("token") or ""), safe="")
    separator = "&" if "?" in path else "?"
    return f"http://127.0.0.1:{int(state['port'])}{path}{separator}t={token}"


def _ping(state: dict, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(
            _service_url(state, "/ping"), timeout=timeout
        ) as response:
            payload = json.load(response)
        return payload.get("ok") is True and payload.get("version") == VERSION
    except Exception:
        return False


def _wait_for_service(timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _read_state()
        if state and _ping(state):
            return state
        time.sleep(0.25)
    return None


def _claim_start_lock() -> bool:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            START_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
    except FileExistsError:
        try:
            if time.time() - START_LOCK.stat().st_mtime > START_LOCK_STALE_AFTER:
                START_LOCK.unlink()
                return _claim_start_lock()
        except OSError:
            pass
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")
    return True


def _ensure_service() -> dict:
    state = _read_state()
    if state and _ping(state):
        return state

    owner = _claim_start_lock()
    if not owner:
        state = _wait_for_service(FOLLOWER_WAIT_TIMEOUT)
        if state:
            return state
        owner = _claim_start_lock()
    if not owner:
        raise RuntimeError("map service startup is already in progress")

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            str(DAEMON),
            "--state",
            str(STATE),
            "--cache",
            str(CACHE_DIR),
            "--version",
            VERSION,
        ]
        with LOG.open("ab") as log:
            subprocess.Popen(
                command,
                cwd=HERE,
                stdout=log,
                stderr=log,
                **_process_options(),
            )
        state = _wait_for_service(SERVICE_START_TIMEOUT)
        if state:
            return state
        tail = ""
        try:
            tail = LOG.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            pass
        raise RuntimeError(f"map service did not start\n{tail}")
    finally:
        try:
            START_LOCK.unlink()
        except OSError:
            pass


def _post(state: dict, path: str, payload: dict, timeout: float = 300) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        _service_url(state, path),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise RuntimeError(f"map service HTTP {error.code}: {detail}") from error


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
    warmup: str = "",
    source_url: str = "",
    source_origin: str = "",
    render_mode: str = "",
    category_colors: str = "",
):
    if warmup:
        try:
            state = _ensure_service()
            return {
                "status": "warm",
                "daemon": True,
                "port": state["port"],
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
    if rescale:
        try:
            lo, hi = (float(value) for value in rescale.split(","))
            options["rescale"] = [lo, hi]
        except ValueError:
            pass
    if render_mode:
        options["render_mode"] = render_mode
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
        state = _ensure_service()
        descriptor = _post(state, "/describe", request)
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
