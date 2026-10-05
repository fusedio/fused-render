"""Locator classification shared by the map template's Python (prepare.py,
discover.py): which strings are remote, which name a multidimensional store,
and where the template's cache lives."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


REMOTE_PREFIXES = ("http://", "https://", "s3://", "/vsi")


def is_remote_path(value: str) -> bool:
    """Return whether *value* is a supported remote or GDAL VSI locator."""
    return bool(value) and value.lower().startswith(REMOTE_PREFIXES)


def is_http_url(value: str) -> bool:
    """Return whether *value* is an HTTP(S) URL, regardless of scheme case."""
    return bool(value) and value.lower().startswith(("http://", "https://"))


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
    """The final path segment of *value*, whatever kind of locator it is: a
    URL's name lives in its path (never its query), and a Windows path uses a
    separator `Path` only understands on Windows."""
    path = urlsplit(value).path if is_http_url(value) else value
    if is_remote_path(path):
        path = path.split("?", 1)[0]
    return Path(path.replace("\\", "/")).name


# Formats read through xarray rather than GDAL.
MULTIDIM_SUFFIXES = {".nc", ".nc4", ".zarr", ".h5", ".hdf5", ".he5", ".hdf"}


def multidim_suffix(target: str) -> str:
    """The store format *target* names, or "" when it is not a multidim store.

    A zarr store shows up under several names — a ``.zarr`` directory or URL, a
    versioned suffix like ``.zarr-v3``, or a path to the store's own
    ``.zmetadata``/``zarr.json`` metadata object — all meaning the same store.
    """
    name = locator_name(target).lower()
    if re.search(r"\.zarr(-[^.]*)?$", name):
        return ".zarr"
    if name == ".zmetadata" or (name == "zarr.json" and _is_zarr_metadata(target)):
        return ".zarr"
    suffix = Path(name).suffix
    return suffix if suffix in MULTIDIM_SUFFIXES else ""


def _is_zarr_metadata(target: str) -> bool:
    """Whether a file named ``zarr.json`` really is zarr v3 store metadata —
    any JSON file can carry that name. A remote URL is taken on the name
    alone; a local file must actually say ``zarr_format``."""
    if is_remote_path(target) or not os.path.isfile(target):
        return True
    try:
        with open(target, "rb") as handle:
            metadata = json.loads(handle.read(1 << 16))
        return isinstance(metadata, dict) and "zarr_format" in metadata
    except (OSError, ValueError):
        return False


def base_home() -> Path:
    return Path(
        os.environ.get("FUSED_RENDER_HOME") or Path.home() / ".fused-render"
    ).expanduser()
