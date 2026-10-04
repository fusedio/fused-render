"""Copy a remote layer's data to a folder on this machine.

The map streams remote sources in place (range requests for a COG or a
PMTiles archive, one chunk per read for Zarr), so nothing is ever written to
disk while looking. This module is for keeping a copy:

  plan   what a download of `source` into `dest_dir` would write: the local
         path it would create (never an existing one), the files behind it,
         their total size when the server says, and whether the disk has room.
  run    do it. Every file lands as `<name>.part` first and is renamed when
         complete, so an interrupted download never leaves something that looks
         whole. Progress goes to a small JSON file (`progress` in the plan) that
         the page polls while this call is still running.

         An interrupted run (the process killed, the network gone) RESUMES: the
         next run of the same source into the same folder finds the `.part`
         whose owner is no longer alive, and continues it — an HTTP Range read
         from where a file stopped, and the chunks a Zarr copy already has are
         skipped. A cancelled run, by contrast, deletes what it wrote.
  cancel   ask a running download to stop: it notices within a block, deletes
           its `.part` files and returns `cancelled`. (Aborting the page's
           request does not stop the Python process, so the stop has to come
           from inside the run.)

A single-file source (COG, GeoParquet, FlatGeobuf, PMTiles, NetCDF...) is one
file. A shapefile brings its sidecars. A Zarr store is a directory, listed from
its consolidated metadata (v2 `.zmetadata`, or v3 `zarr.json` with
`consolidated_metadata`); chunks the store never wrote are simply absent, as
they are in the original.
"""
from __future__ import annotations

import glob
import hashlib
import itertools
import json
import math
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if "__file__" not in globals():
    __file__ = os.path.join(sys.path[0], "download.py")
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from geo_paths import base_home, is_http_url, locator_name, multidim_suffix  # noqa: E402

PROGRESS_DIR = base_home() / "cache" / "map-downloads"
SHAPEFILE_SIDECARS = (".shx", ".dbf", ".prj", ".cpg")
MAX_STORE_FILES = 250_000
WORKERS = 16
TIMEOUT = 60
PROGRESS_EVERY = 0.25  # seconds between progress-file writes


class Refusal(Exception):
    """A download this module will not attempt, with the reason to show."""


class Cancelled(Exception):
    """The page asked this download to stop."""


# ---- where the bytes come from ------------------------------------------------------

def _http_url(source: str) -> str:
    """The HTTPS URL behind a remote locator: `s3://bucket/key` becomes the
    bucket's public endpoint, and an Azure blob gets a Planetary Computer
    token when its container needs one."""
    if source.lower().startswith("s3://"):
        bucket, _, key = source[5:].partition("/")
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    if not is_http_url(source):
        raise Refusal("Only http(s):// and s3:// sources can be downloaded.")
    from blob_tokens import TOKENS, container_of

    return TOKENS.sign(source) if container_of(source) else source


def _session():
    import requests

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=WORKERS, pool_maxsize=WORKERS)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _join(base: str, key: str) -> str:
    """`key` under the store URL `base`, keeping any query (a SAS token)."""
    path, sep, query = base.partition("?")
    return path.rstrip("/") + "/" + key + (sep + query if sep else "")


def _size(session, url: str) -> int | None:
    try:
        response = session.head(url, allow_redirects=True, timeout=TIMEOUT)
        if response.ok and response.headers.get("Content-Length"):
            return int(response.headers["Content-Length"])
        # Some servers refuse HEAD; a one-byte range read reports the length too.
        response = session.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=TIMEOUT)
        response.close()
        total = response.headers.get("Content-Range", "").rpartition("/")[2]
        return int(total) if total.isdigit() else None
    except Exception:
        return None


def _get_json(session, url: str):
    response = session.get(url, timeout=TIMEOUT)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


# ---- what a source is made of ------------------------------------------------------------

def _zarr_v2_keys(metadata: dict) -> list[str]:
    keys = [".zmetadata"]
    for key, meta in metadata.items():
        keys.append(key)
        if not key.endswith(".zarray"):
            continue
        array = key[: -len(".zarray")].rstrip("/")
        sep = meta.get("dimension_separator") or "."
        shape, chunks = meta["shape"], meta["chunks"]
        if not shape:
            keys.append(f"{array}/0" if array else "0")
            continue
        grid = [range(math.ceil(s / c)) if c else range(1) for s, c in zip(shape, chunks)]
        prefix = f"{array}/" if array else ""
        keys.extend(prefix + sep.join(map(str, index)) for index in itertools.product(*grid))
        if len(keys) > MAX_STORE_FILES:
            break
    return keys


def _zarr_v3_keys(root: dict) -> list[str]:
    nodes = (root.get("consolidated_metadata") or {}).get("metadata")
    if not isinstance(nodes, dict):
        raise Refusal("This Zarr v3 store has no consolidated metadata, so its "
                      "files cannot be listed over HTTP.")
    keys = ["zarr.json"]
    for path, meta in nodes.items():
        keys.append(f"{path}/zarr.json")
        if meta.get("node_type") != "array":
            continue
        shape = meta.get("shape") or []
        chunks = (meta.get("chunk_grid") or {}).get("configuration", {}).get("chunk_shape") or shape
        # "default" keys are c/0/1 (just "c" for a scalar); "v2" keys are 0.1 ("0").
        encoding = meta.get("chunk_key_encoding") or {"name": "default"}
        default = encoding.get("name", "default") == "default"
        sep = (encoding.get("configuration") or {}).get("separator") or ("/" if default else ".")
        grid = [range(math.ceil(s / c)) if c else range(1) for s, c in zip(shape, chunks)]
        for index in itertools.product(*grid):
            coords = sep.join(map(str, index))
            if default:
                chunk = "c" + (sep + coords if index else "")
            else:
                chunk = coords if index else "0"
            keys.append(f"{path}/{chunk}")
        if len(keys) > MAX_STORE_FILES:
            break
    return keys


def _zarr_root(source: str) -> str:
    """The store URL for a source that may name the store's own metadata."""
    name = locator_name(source)
    if name in (".zmetadata", "zarr.json"):
        path, sep, query = source.partition("?")
        return path[: -len(name)].rstrip("/") + (sep + query if sep else "")
    return source


def _listing(session, source: str) -> tuple[str, list[str], bool]:
    """(base URL, relative keys, is_directory) for everything behind `source`."""
    if multidim_suffix(source) == ".zarr":
        base = _http_url(_zarr_root(source))
        v2 = _get_json(session, _join(base, ".zmetadata"))
        if v2 and isinstance(v2.get("metadata"), dict):
            keys = _zarr_v2_keys(v2["metadata"])
        else:
            v3 = _get_json(session, _join(base, "zarr.json"))
            if not v3:
                raise Refusal("This Zarr store has no consolidated metadata "
                              "(.zmetadata or zarr.json), so its files cannot be "
                              "listed over HTTP.")
            keys = _zarr_v3_keys(v3)
        if len(keys) > MAX_STORE_FILES:
            raise Refusal(f"This Zarr store has more than {MAX_STORE_FILES:,} files; "
                          "copy it with a tool made for bulk transfers instead.")
        return base, keys, True
    url = _http_url(source)
    path, sep, query = url.partition("?")
    parent, _, name = path.rpartition("/")
    base = parent + (sep + query if sep else "")
    keys = [name]
    if name.lower().endswith(".shp"):
        stem = name[:-4]
        keys += [stem + suffix for suffix in SHAPEFILE_SIDECARS]
    return base, keys, False


# ---- where the bytes go -----------------------------------------------------------------

def _read_progress(target: Path) -> dict:
    try:
        return json.loads(_progress_path(target).read_text())
    except (OSError, ValueError):
        return {}


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # someone else's process: alive, not ours to judge
        return True
    return True


def _resumable(candidate: Path, source: str) -> bool:
    """Whether `candidate.part` is an interrupted download of this same source:
    its progress file names the source, and the process that wrote it is gone."""
    state = _read_progress(candidate)
    return (state.get("source") == source and not state.get("finished")
            and not _alive(state.get("pid")))


def _free_name(dest_dir: Path, name: str, source: str = "") -> tuple[Path, bool]:
    """(`dest_dir/name` or `name-1`, `name-2`..., resume). A download never
    overwrites a file the user has, and never takes over a `.part` another run
    is still writing — but it does pick up one this source left unfinished."""
    stem, suffix = os.path.splitext(name)
    if name.lower().endswith(".zarr"):
        stem, suffix = name[:-5], name[-5:]
    candidate = dest_dir / name

    def taken(path: Path) -> bool:
        # A shapefile name is taken if any piece of the set already exists.
        # The path as spelled is always checked; each piece also in lower and
        # upper case, since a case-sensitive disk treats ROADS.SHP and
        # ROADS.shp as different files.
        if path.exists():
            return True
        if path.suffix.lower() == ".shp":
            return any(path.with_suffix(spelled).exists()
                       for s in (".shp",) + SHAPEFILE_SIDECARS
                       for spelled in (s, s.upper()))
        return False

    for n in itertools.count(1):
        if not taken(candidate):
            if not Path(str(candidate) + ".part").exists():
                return candidate, False
            if source and _resumable(candidate, source):
                return candidate, True
        candidate = dest_dir / f"{stem}-{n}{suffix}"
    raise AssertionError("unreachable")


def _dest_dir(dest_dir: str) -> Path:
    if not dest_dir:
        raise Refusal("Choose a folder to download into.")
    folder = Path(os.path.expanduser(dest_dir))
    if not folder.is_absolute():
        raise Refusal("The download folder must be an absolute path.")
    if not folder.is_dir():
        raise Refusal(f"Not a folder: {folder}")
    if not os.access(folder, os.W_OK):
        raise Refusal(f"This folder is not writable: {folder}")
    return folder


def _progress_path(target: Path) -> Path:
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    return PROGRESS_DIR / (hashlib.sha256(str(target).encode()).hexdigest()[:16] + ".json")


def _cancel_path(progress: Path) -> Path:
    return progress.with_suffix(".cancel")


def _write_progress(path: Path, **state) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


def plan(source: str, dest_dir: str = "") -> dict:
    session = _session()
    base, keys, is_dir = _listing(session, source)
    name = locator_name(_zarr_root(source)) if is_dir else keys[0]
    out: dict = {"source": source, "name": name, "files": len(keys), "directory": is_dir}
    if not is_dir:
        # Sidecars that do not exist on the server are skipped, not failed.
        out["bytes"] = _size(session, _join(base, keys[0]))
    if dest_dir:
        folder = _dest_dir(dest_dir)
        target, resume = _free_name(folder, name, source)
        out.update(target=str(target), progress=str(_progress_path(target)), resume=resume,
                   free_bytes=shutil.disk_usage(folder).free)
        if out.get("bytes") and out["bytes"] > out["free_bytes"]:
            raise Refusal(f"Not enough free space in {folder}: the download is "
                          f"{out['bytes']:,} bytes and {out['free_bytes']:,} are free.")
    return out


def _fetch(session, url: str, dest: Path, on_bytes, stop: Path, finalize: bool = True) -> bool:
    """Stream `url` to `dest` via `dest.part`; False when the server has no such
    object (an unwritten Zarr chunk, a shapefile without a .cpg). Raises
    Cancelled as soon as the `stop` marker appears. `finalize=False` leaves the
    finished bytes at `dest.part`, for a set of files renamed together."""
    if stop.exists():
        raise Cancelled()
    part = Path(str(dest) + ".part")
    have = part.stat().st_size if part.is_file() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with session.get(url, stream=True, timeout=TIMEOUT, headers=headers) as response:
        if response.status_code in (403, 404):
            return False
        if have and response.status_code == 416:  # the part already holds it all
            if finalize:
                os.replace(part, dest)
            on_bytes(have)
            return True
        response.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        # 206: the server continues where the part stops. 200: it ignored the
        # range, so the part starts over.
        resumed = have and response.status_code == 206
        if resumed:
            on_bytes(have)
        with open(part, "ab" if resumed else "wb") as handle:
            for block in response.iter_content(1 << 20):
                if stop.exists():
                    raise Cancelled()
                handle.write(block)
                on_bytes(len(block))
    if finalize:
        os.replace(part, dest)
    return True


ZARR_METADATA = {".zmetadata", ".zgroup", ".zattrs", ".zarray", "zarr.json"}


def _discard_parts(target: Path) -> list[str]:
    """Remove what an unfinished download of `target` left: `<stem>.<ext>.part`
    files and a `.zarr.part` directory. Finished files are never touched."""
    removed = []
    for leftover in target.parent.glob(glob.escape(target.stem) + ".*.part"):
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
        else:
            leftover.unlink(missing_ok=True)
        removed.append(str(leftover))
    return removed


def cancel(progress: str) -> dict:
    """Leave the stop marker for the run reporting to `progress`."""
    path = Path(progress)
    if path.parent != PROGRESS_DIR or path.suffix != ".json":
        raise Refusal("Not a download in progress.")
    _cancel_path(path).write_text("")
    return {"cancelled": True}


def run(source: str, dest_dir: str, target: str = "") -> dict:
    """Download into `dest_dir`. `target` is the path a previous `plan` chose
    (the page shows it and polls its progress); it is honoured while it is
    still free, and replaced by the next free name otherwise."""
    info = plan(source, dest_dir)
    if target and Path(target).parent == Path(info["target"]).parent:
        chosen = Path(target)
        part_free = not Path(str(chosen) + ".part").exists() or _resumable(chosen, source)
        if not chosen.exists() and part_free:
            info.update(target=str(chosen), progress=str(_progress_path(chosen)))
    target, progress = Path(info["target"]), Path(info["progress"])
    stop = _cancel_path(progress)
    stop.unlink(missing_ok=True)
    session = _session()
    base, keys, is_dir = _listing(session, source)
    state = {"done_bytes": 0, "total_bytes": info.get("bytes"), "done_files": 0,
             "total_files": len(keys), "finished": False, "source": source,
             "pid": os.getpid()}
    last = [0.0]
    lock = threading.Lock()

    def report(force: bool = False) -> None:
        now = time.monotonic()
        if force or now - last[0] >= PROGRESS_EVERY:
            last[0] = now
            _write_progress(progress, **state)

    def on_bytes(n: int) -> None:
        with lock:
            state["done_bytes"] += n
            report()

    report(force=True)
    try:
        if is_dir:
            # The whole store lands under <target>.part and is renamed at the
            # end, so a half-copied store never looks like a store.
            staging = Path(str(target) + ".part")
            staging.mkdir(exist_ok=True)  # a resumed copy keeps what it has

            def one(key: str) -> None:
                done = staging / key
                if done.is_file():  # finished by an earlier, interrupted run
                    on_bytes(done.stat().st_size)
                elif not _fetch(session, _join(base, key), done, on_bytes, stop):
                    # An absent chunk is one the store never wrote (it reads as
                    # fill value); absent metadata is a broken or refused copy.
                    if key.rsplit("/", 1)[-1] in ZARR_METADATA:
                        raise Refusal(f"The server did not return {key}, which the "
                                      "store's metadata lists; nothing was kept.")
                with lock:
                    state["done_files"] += 1
                    report()

            with ThreadPoolExecutor(WORKERS) as pool:
                for _ in pool.map(one, keys):
                    pass
            os.replace(staging, target)
            written = [str(target)]
        else:
            # A shapefile's pieces all stay as .part until every one has
            # arrived, then are renamed together: a cancel or failure never
            # leaves a .shp without its sidecars.
            fetched = []
            for index, key in enumerate(keys):
                # Sidecars follow the main file's (possibly de-duplicated) name.
                dest = target if index == 0 else target.with_name(target.stem + os.path.splitext(key)[1])
                if _fetch(session, _join(base, key), dest, on_bytes, stop, finalize=False):
                    fetched.append(dest)
                elif index == 0:
                    raise Refusal(f"The server has no file at {source}")
                state["done_files"] += 1
                report()
            for dest in fetched:
                os.replace(str(dest) + ".part", dest)
            written = [str(dest) for dest in fetched]
    except Cancelled:
        _discard_parts(target)
        state["finished"] = True
        return {"cancelled": True, "path": str(target)}
    except Refusal:
        _discard_parts(target)
        state["finished"] = True
        raise
    else:
        state["finished"] = True
    finally:
        # Any other failure (the network dropped) keeps its parts and stays
        # unfinished, so running the same download again resumes it.
        stop.unlink(missing_ok=True)
        state["pid"] = None
        report(force=True)
    return {"path": str(target), "written": written, "bytes": state["done_bytes"],
            "files": state["done_files"]}


def main(source: str = "", dest_dir: str = "", action: str = "plan", target: str = "",
         progress: str = ""):
    try:
        source = str(source or "").strip()
        if action == "cancel":
            return {"status": "ok", **cancel(progress)}
        if not source:
            raise Refusal("No source given.")
        if action == "plan":
            result = plan(source, dest_dir)
        elif action == "run":
            result = run(source, dest_dir, target)
        else:
            raise Refusal(f"unknown action {action!r}")
        return {"status": "ok", **result}
    except Refusal as refusal:
        return {"status": "error", "message": str(refusal)}
    except Exception as error:
        message = f"{type(error).__name__}: {error}"
        if action == "run":
            message += " — run the same download again to resume it."
        return {"status": "error", "message": message}
