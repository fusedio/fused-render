"""Mount-safe filesystem discovery for the Map Viewer file-picker modal, and
the staging directory OS drag-and-drops upload into (action="drops_dir")."""
from __future__ import annotations

import json
import os
import re
import stat
import string
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

if __package__:
    from ..shared.private_dir import private_dir, require_private
    from .geo_paths import is_remote_path, normalize_remote_path
else:
    if "__file__" not in globals():
        __file__ = os.path.join(sys.path[0], "discover.py")
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))
    from geo_paths import is_remote_path, normalize_remote_path
    from private_dir import private_dir, require_private


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
    ".nc4",
    ".hdf",
    ".hdf5",
    ".he5",
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
    if is_remote_path(cleaned):
        return normalize_remote_path(cleaned)
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


ZARR_MARKERS = ("zarr.json", ".zmetadata", ".zgroup", ".zarray")


def _is_zarr_directory(path: str, name: str) -> bool:
    """Whether a directory is a zarr store rather than a lookalike name.

    A store always carries one of the metadata objects at its root. When the
    directory cannot be read here — a remote listing arrives as names alone —
    the name is all there is to go on, which is the old behaviour.
    """
    if not re.search(r"\.zarr(-[^.]*)?$", name.lower()):
        return False
    try:
        entries = set(os.listdir(path))
    except OSError:
        return True
    return any(marker in entries for marker in ZARR_MARKERS)


def _payload(
    directory: str,
    triples: list[tuple[str, bool, int | None]],
    *,
    selected: str = "",
) -> dict[str, Any]:
    entries = []
    for name, is_directory, size in triples:
        full = os.path.join(directory, name)
        # A zarr store is a directory that opens like a file — but only a
        # real one. Claiming every directory whose name ends in .zarr made an
        # ordinary folder someone happened to call `experiments.zarr` into a
        # dead end: the browser navigates on "dir" and nothing else, so it
        # could neither be entered nor opened.
        if is_directory and _is_zarr_directory(full, name):
            item_kind = "raster"
        else:
            item_kind = "dir" if is_directory else kind(name)
        if item_kind == "other":
            continue
        entries.append(
            {
                "name": name,
                "path": full,
                "kind": item_kind,
                "ext": "" if item_kind == "dir" else os.path.splitext(name)[1].lower(),
                "size": None if is_directory else size,
                "hidden": name.startswith("."),
                "selectable": item_kind != "dir",
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


def _drops_root() -> str:
    """Where OS drag-and-drops are staged: a per-user tree under the shared
    temp root, the same layout as claude/agent.py's `_runs_root` and for the
    same reason — one shared name cannot be both 0700-private and usable by a
    second account, and a per-uid root dissolves the contention. POSIX-only
    suffix: Windows has no `geteuid` and its temp dir is already per-user."""
    geteuid = getattr(os, "geteuid", None)
    suffix = "-%d" % geteuid() if geteuid is not None else ""
    return os.path.join(tempfile.gettempdir(), "fused_render_map" + suffix, "drops")


DROPS = _drops_root()

# Staged drops are scratch, not storage: a file only has to outlive the map
# session whose layer points at it, but it can be a multi-GB raster — so the
# TTL is generous (a week comfortably outlives any open session) and the count
# backstop small next to the screenshot pruner's 200 tiny crops.
DROPS_TTL = 7 * 24 * 3600
DROPS_KEEP = 50


def _prune_drops() -> None:
    """Drop staged files whose session is long gone. Best-effort throughout:
    this is housekeeping on a temp directory, and no failure here is worth
    refusing the user their drop over."""
    try:
        names = os.listdir(DROPS)
    except OSError:
        return
    now = time.time()
    aged = []
    for name in names:
        path = os.path.join(DROPS, name)
        try:
            mtime = os.lstat(path).st_mtime
        except OSError:
            continue
        aged.append((mtime, path))
    stale = [p for m, p in aged if now - m > DROPS_TTL]
    # Oldest first, so what survives the count cap is the recent session.
    aged.sort()
    excess = [p for _m, p in aged[:max(0, len(aged) - DROPS_KEEP)]]
    for path in set(stale) | set(excess):
        try:
            os.unlink(path)
        except OSError:
            pass


def _drops_dir() -> dict[str, Any]:
    """Ensure the drop staging directory exists and hand its path to the page.

    Same shape and same asymmetry as claude/agent.py's `_shots_dir`: the
    directory is SHARED and long-lived, so an existing one is adopted rather
    than refused — but only after `require_private` vouches for it, and on
    POSIX a merely world-READABLE one is tightened or refused (the drops are
    the user's own data files). A refusal (`require_private` raising)
    propagates; an ordinary OSError becomes an error dict, which the page
    surfaces as a failed drop rather than a crash.
    """
    if os.path.isdir(DROPS):
        require_private(DROPS)
        if hasattr(os, "geteuid"):
            try:
                mode = stat.S_IMODE(os.lstat(DROPS).st_mode)
                if mode & ~0o700:
                    os.chmod(DROPS, 0o700)
                    # Re-read rather than trust the call: an ACL, or a
                    # filesystem that does not carry unix modes, can accept a
                    # chmod and keep the bits exactly where they were.
                    mode = stat.S_IMODE(os.lstat(DROPS).st_mode)
            except OSError as error:
                return {"error": f"Could not secure the drop directory: {error}"}
            if mode & ~0o700:
                return {
                    "error": "The drop directory is readable by others "
                    f"(mode {mode:04o}) and could not be tightened"
                }
    else:
        try:
            private_dir(DROPS, os.path.dirname(DROPS))
        except FileExistsError:
            # Another page asked at the same moment. Theirs is fine if it is
            # ours; require_private is what decides that (and raises if not).
            require_private(DROPS)
        except OSError as error:
            return {"error": f"Could not prepare the drop directory: {error}"}
    _prune_drops()
    return {"dir": DROPS}


def main(dir: str = "", src: str = "", action: str = "") -> dict[str, Any]:
    if action == "drops_dir":
        return _drops_dir()
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
