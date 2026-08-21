"""Path classification shared by the map raster, vector, and multidim engines."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit


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


def locator_name(value: str) -> str:
    """The final path segment of *value*, whatever kind of locator it is.

    A remote locator's name lives in its path — reading it off the whole
    string would pick up the query, and a signed `/vsicurl/` or `s3://` URL
    carries one just as an `https://` one does — and a Windows path uses the
    separator `Path` only understands on Windows. Both are normalized here
    rather than in each engine that asks.
    """
    path = urlsplit(value).path if is_http_url(value) else value
    if is_remote_path(path):
        path = path.split("?", 1)[0]
    return Path(path.replace("\\", "/")).name


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


# Formats read through xarray rather than GDAL. Named here, beside the
# other locator classification, so the light callers that only need to
# ROUTE a target do not import the engine that reads one.
MULTIDIM_SUFFIXES = {".nc", ".nc4", ".zarr", ".h5", ".hdf5", ".he5", ".hdf"}


def multidim_suffix(target: str) -> str:
    """The store format *target* names, or "" when it is not a multidim store.

    A zarr store shows up under several names — a ``.zarr`` directory or URL, a
    versioned suffix like ``.zarr-v3``, or a path to the store's own
    ``.zmetadata``/``zarr.json`` metadata object — all of which mean the same
    store.
    """
    name = locator_name(target).lower()
    if re.search(r"\.zarr(-[^.]*)?$", name):
        return ".zarr"
    if name == ".zmetadata" or (name == "zarr.json" and _is_zarr_metadata(target)):
        return ".zarr"
    suffix = Path(name).suffix
    return suffix if suffix in MULTIDIM_SUFFIXES else ""


def _is_zarr_metadata(target: str) -> bool:
    """Whether a file named ``zarr.json`` really is zarr v3 store metadata.

    Any ordinary JSON file can carry that name, and claiming one here would
    steal it from the vector/JSON path for good — an error descriptor is not
    ``None``, so nothing after this engine would ever see it. A remote URL is
    claimed on the name alone (fetching it to sniff costs a network round
    trip, and a remote GeoJSON named exactly ``zarr.json`` is not a real
    case); a local file must actually say ``zarr_format``.
    """
    if is_remote_path(target) or not os.path.isfile(target):
        return True
    try:
        with open(target, "rb") as handle:
            metadata = json.loads(handle.read(1 << 16))
        return isinstance(metadata, dict) and "zarr_format" in metadata
    except (OSError, ValueError):
        return False


def zarr_store(source: str) -> str:
    """The store a metadata-object locator points at: its parent directory.

    Trimming the name off the END of the whole locator eats into a query
    string — a signed URL finishes with its signature, not with the object
    name — so the locator is taken apart and rebuilt around the shortened
    path. That applies to every signed spelling, not just ``https://``:
    ``/vsicurl/`` and ``s3://`` carry the same tokens.
    """
    if is_http_url(source):
        parts = urlsplit(source)
        name = Path(parts.path).name.lower()
        if name not in {".zmetadata", "zarr.json"}:
            return source
        return urlunsplit(
            parts._replace(path=parts.path[: -(len(name) + 1)] or "/")
        )
    head, separator, query = source.partition("?")
    name = Path(head.replace("\\", "/")).name.lower()
    if name not in {".zmetadata", "zarr.json"}:
        return source
    return head[: -(len(name) + 1)] + separator + query


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
