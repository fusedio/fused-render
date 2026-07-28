"""Mount-safe filesystem discovery for the Map Viewer file-picker modal."""
from __future__ import annotations

import json
import os
import string
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


RASTER = (
    ".tif",
    ".tiff",
    ".cog",
    ".vrt",
    ".jp2",
    ".j2k",
    ".img",
    ".ntf",
    ".nitf",
    ".dem",
    ".dt0",
    ".dt1",
    ".dt2",
    ".hgt",
    ".grd",
    ".nc",
    ".hdf",
    ".h5",
)
VECTOR = (
    ".geojson",
    ".json",
    ".shp",
    ".gpkg",
    ".fgb",
    ".kml",
    ".gml",
    ".tab",
    ".mif",
    ".dxf",
)
TABLE = (".parquet", ".geoparquet", ".csv", ".tsv", ".xlsx", ".xls")
PMTILES = (".pmtiles",)
QUOTE_PAIRS = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019"}


def clean_path(value: str) -> str:
    """Remove clipboard quoting and expand only local path syntax."""
    cleaned = str(value or "").strip()
    while (
        len(cleaned) >= 2
        and cleaned[0] in QUOTE_PAIRS
        and cleaned[-1] == QUOTE_PAIRS[cleaned[0]]
    ):
        cleaned = cleaned[1:-1].strip()
    return os.path.expandvars(os.path.expanduser(cleaned))


def kind(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith(".py"):
        return "python"
    if lowered.endswith(PMTILES):
        return "pmtiles"
    if lowered.endswith(RASTER):
        return "raster"
    if lowered.endswith(VECTOR):
        return "vector"
    if lowered.endswith(TABLE):
        return "table"
    return "other"


def roots() -> list[dict[str, str]]:
    """Return native filesystem entry points without example-only locations."""
    locations: list[dict[str, str]] = []
    home = Path.home()
    if home.is_dir():
        locations.append({"name": "Home", "path": str(home), "kind": "home"})
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.is_dir():
                locations.append(
                    {"name": f"{letter}:", "path": str(drive), "kind": "drive"}
                )
    else:
        locations.append({"name": "Computer", "path": os.sep, "kind": "root"})
    deduplicated: dict[str, dict[str, str]] = {}
    for location in locations:
        deduplicated.setdefault(os.path.normcase(location["path"]), location)
    return list(deduplicated.values())


def _server_url(src: str, endpoint: str, path: str) -> str:
    origin = urllib.parse.urlsplit(src)
    return (
        f"{origin.scheme}://{origin.netloc}{endpoint}?path="
        + urllib.parse.quote(path)
    )


def _stat(src: str, path: str) -> tuple[str, dict[str, Any] | None]:
    try:
        with urllib.request.urlopen(
            _server_url(src, "/api/fs/stat", path), timeout=10
        ) as response:
            return "ok", json.load(response)
    except urllib.error.HTTPError as error:
        return ("missing", None) if error.code == 404 else ("unreachable", None)
    except Exception:
        return "unreachable", None


def _list_remote(src: str, path: str, cap: int = 5000) -> list[dict[str, Any]]:
    """Use the mount-routed API and never fall back to a kernel listing."""
    entries: list[dict[str, Any]] = []
    cursor = ""
    while True:
        url = _server_url(src, "/api/fs/list", path)
        if cursor:
            url += "&cursor=" + urllib.parse.quote(cursor)
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.load(response)
        entries.extend(payload.get("entries") or [])
        cursor = payload.get("cursor") or ""
        if len(entries) >= cap or not payload.get("truncated") or not cursor:
            return entries[:cap]


def _payload(
    directory: str,
    triples: list[tuple[str, bool, int | None]],
    *,
    selected: str = "",
) -> dict[str, Any]:
    entries = []
    for name, is_directory, size in triples:
        full = os.path.join(directory, name)
        item_kind = "dir" if is_directory else kind(name)
        if item_kind == "other":
            continue
        entries.append(
            {
                "name": name,
                "path": full,
                "kind": item_kind,
                "ext": "" if is_directory else os.path.splitext(name)[1].lower(),
                "size": None if is_directory else size,
                "hidden": name.startswith("."),
                "selectable": not is_directory and item_kind != "other",
            }
        )
    entries.sort(key=lambda entry: (entry["kind"] != "dir", entry["name"].lower()))
    result: dict[str, Any] = {
        "dir": directory,
        "parent": os.path.dirname(directory) or directory,
        "entries": entries,
        "roots": roots(),
    }
    if selected:
        result["selected"] = selected
        result["selected_kind"] = kind(os.path.basename(selected))
    return result


def _local_payload(requested: str) -> dict[str, Any]:
    path = Path(requested)
    selected = ""
    if path.is_file():
        selected = str(path)
        path = path.parent
    if not path.is_dir():
        return {
            "error": f"Not found: {path}",
            "dir": str(path),
            "entries": [],
            "roots": roots(),
        }
    triples: list[tuple[str, bool, int | None]] = []
    try:
        children = list(path.iterdir())
    except (OSError, PermissionError) as error:
        return {
            "error": f"Could not list directory: {path}",
            "detail": str(error),
            "dir": str(path),
            "entries": [],
            "roots": roots(),
        }
    for child in children:
        try:
            is_directory = child.is_dir()
            is_file = child.is_file()
            if not is_directory and not is_file:
                continue
            size = None if is_directory else child.stat().st_size
        except OSError:
            continue
        triples.append((child.name, is_directory, size))
    return _payload(str(path), triples, selected=selected)


def main(dir: str = "", src: str = "") -> dict[str, Any]:
    requested = os.path.abspath(clean_path(dir) or str(Path.home()))
    if src:
        status, metadata = _stat(src, requested)
        if status == "missing":
            return {
                "error": f"Not found: {requested}",
                "dir": requested,
                "entries": [],
                "roots": roots(),
            }
        if status == "ok" and metadata and metadata.get("remote"):
            selected = ""
            directory = requested
            if not metadata.get("is_dir", True):
                selected = requested
                directory = os.path.dirname(requested) or os.path.sep
            try:
                remote_entries = _list_remote(src, directory)
            except Exception as error:
                return {
                    "error": f"Could not list remote directory: {directory}",
                    "detail": str(error),
                    "dir": directory,
                    "entries": [],
                    "roots": roots(),
                }
            triples = [
                (
                    str(entry.get("name") or ""),
                    bool(entry.get("is_dir")),
                    entry.get("size"),
                )
                for entry in remote_entries
                if entry.get("name")
            ]
            return _payload(directory, triples, selected=selected)
    return _local_payload(requested)
