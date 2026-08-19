"""Path classification shared by the map raster, vector, and multidim engines."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote


REMOTE_PREFIXES = ("http://", "https://", "s3://", "/vsi")


def is_remote_path(value: str) -> bool:
    """Return whether *value* is a supported remote or GDAL VSI locator."""
    return bool(value) and value.lower().startswith(REMOTE_PREFIXES)


def is_http_url(value: str) -> bool:
    """Return whether *value* is an HTTP(S) URL, regardless of scheme case."""
    return bool(value) and value.lower().startswith(("http://", "https://"))


def is_native_remote_path(value: str) -> bool:
    """Return whether GDAL should open *value* directly."""
    return bool(value) and value.lower().startswith(("s3://", "/vsi"))


def is_vsi_path(value: str) -> bool:
    """Return whether *value* is a GDAL virtual-filesystem path."""
    return bool(value) and value.lower().startswith("/vsi")


def normalize_remote_path(value: str) -> str:
    """Canonicalize only transport prefixes while preserving object-path case."""
    if not value:
        return value
    lowered = value.lower()
    for prefix in ("https://", "http://", "s3://"):
        if lowered.startswith(prefix):
            return prefix + value[len(prefix):]
    if lowered.startswith("/vsi"):
        slash = value.find("/", 1)
        if slash < 0:
            return lowered
        return lowered[:slash] + "/" + normalize_remote_path(value[slash + 1:])
    return value


def raw_url(origin: str, path: str) -> str:
    """Range-capable shell URL for *path*, for readers that cannot open it."""
    return (
        origin.rstrip("/")
        + "/api/fs/raw?path="
        + quote(path, safe="")
        + "&pooled=1"
    )


def resolve_source(request: dict[str, Any], target: str) -> str:
    """The locator an engine should actually read *target* through.

    A readable local file is opened directly. A native remote path (s3://,
    /vsi) goes to the reader as-is. The shell's pre-signed ``source_url`` is
    trusted only when *target* is the request's own target — a path returned
    by a Python target has no relationship to the URL the shell signed for
    the script file. Everything else falls back to the shell's range
    endpoint, or an absolute path when no origin was supplied.
    """
    target = normalize_remote_path(target) if is_remote_path(target) else target
    supplied_url = str(request.get("source_url") or "")
    if is_remote_path(supplied_url):
        supplied_url = normalize_remote_path(supplied_url)
    direct_target = str(request.get("target") or "")
    if is_remote_path(direct_target):
        direct_target = normalize_remote_path(direct_target)
    local = (
        not is_remote_path(target)
        and not is_managed_mount(target)
        and os.path.isfile(target)
    )
    if local:
        return os.path.abspath(target)
    if is_native_remote_path(target):
        return target
    if target == direct_target and supplied_url:
        return supplied_url
    if is_http_url(target):
        return target
    origin = str(request.get("source_origin") or "")
    return raw_url(origin, target) if origin else os.path.abspath(target)


def _base_home() -> Path:
    return Path(
        os.environ.get("FUSED_RENDER_HOME") or Path.home() / ".fused-render"
    ).expanduser()


def _branch_ref() -> str:
    raw = os.environ.get("FUSED_RENDER_BRANCH", "")
    if not raw or raw.lower() in {"main", "master", "head"}:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:12].rstrip("-")


def _contains(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(str(root)), os.path.normcase(str(candidate)))
        ) == os.path.normcase(str(root))
    except ValueError:
        return False


def is_managed_mount(value: str) -> bool:
    """Return whether a local path belongs to any Fused Render mount root."""
    if not value or is_remote_path(value):
        return False
    candidate = Path(value).expanduser().absolute()
    base = _base_home().absolute()
    roots = [base / "mounts"]
    branch = _branch_ref()
    if branch:
        roots.append(base / "branches" / branch / "mounts")
    if any(_contains(root, candidate) for root in roots):
        return True

    # A view can retain a path from another branch-isolated shell. Recognize
    # <base>/branches/<ref>/mounts without treating arbitrary "mounts"
    # directories elsewhere on disk as managed.
    try:
        relative = candidate.relative_to(base / "branches")
    except ValueError:
        return False
    return len(relative.parts) >= 3 and relative.parts[1].lower() == "mounts"
