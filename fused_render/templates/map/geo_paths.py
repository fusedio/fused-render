"""Path classification shared by the map raster and vector engines."""
from __future__ import annotations

import os
import re
from pathlib import Path


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
