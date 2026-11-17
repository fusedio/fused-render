"""Search and folder listings over the public `fused-asset` bucket, from a
published parquet listing.

The bucket is never listed from here. A one-off indexer
(`scripts/fused_asset_index.py`) publishes one parquet file naming every
object; this module downloads it (conditional GET on its ETag), ingests it
into a SIBLING index store, ranks over that store with the main index's own
query code, and answers one-level folder listings from it.

**Sibling store, same format.** The store lives at
`<home>/index-remote/fused-asset`, never inside the main index: the main
index is the user's disk, and its scans, deletes and ignore rules have no
business touching bucket rows. It is written through the main index's own
`store.Sink` and `store.compact`, so the 7-column files table, the dirs
table, the partitions and the manifest cannot drift from what
`query.search_ranked` reads. Nothing in `fused_render/index/` knows this
store exists.

**Pseudo root.** `search_ranked` normalises its root with `os.path.abspath`,
which an `s3://` spelling does not survive. So inside the store an object
key `k` is spelled `<root>/k`, where `<root>` is `/fused-asset` normalised by
the same expression the query code applies (on Windows it picks up the
current drive, `C:/fused-asset`). The root never touches the disk. It is
recorded in `state.json` at ingest and that recorded spelling is what the
readers use, so a later change of the process's drive cannot orphan the
store. `s3://fused-asset/<key>` is produced only at this module's boundary.

**Coverage.** `_coverage_reason` answers "uncovered" unless dirs.parquet has
a row equal to the root, so every ingest synthesises a dirs row for the root
and for every ancestor folder of every key.

**Untrusted input.** The listing comes from a public bucket. It is fetched
without following redirects, capped in bytes and rows, streamed in batches,
and every row is checked (non-empty key under S3's 1024-byte limit, no NUL,
non-negative size, strictly increasing keys) before it reaches the Sink; a
bad file is rejected and the previous store keeps serving.

**Refresh.** `start_background(reason)` runs one ingest on a daemon thread
(one at a time per process; `store.compact`'s flock serialises across
processes). `startup_check()` starts one when the store is absent or the
last check is older than `REFRESH_S`; `retry_if_needed()` is the cheap,
throttled in-flight version every index-reading route runs (Home search,
the agent's search, folder listings), which retries a failed ingest and
also refreshes a long-running server once a day.

pyarrow and duckdb belong to the optional `index` extra and are imported
lazily; without them `status()` reports `error` and the readers answer
not-ready. Nothing here gates on the canvases flag: callers do.
"""
from __future__ import annotations

import glob
import hashlib
import logging
import os
import posixpath
import re
import shutil
import threading
import time
import urllib.request
import uuid

import httpx

from fused_render import extras, jobs
from fused_render.remote_access import BUCKET, REGION, canonical_url
from fused_render.shell import storage

logger = logging.getLogger(__name__)

#: Where the indexer publishes. `FUSED_RENDER_REMOTE_INDEX_URL` overrides it
#: and may also be a local file path (or file:// URL).
INDEX_URL = (f"https://{BUCKET}.s3.{REGION}.amazonaws.com/"
             f"fused-render-index/{BUCKET}.parquet")
ENV_URL = "FUSED_RENDER_REMOTE_INDEX_URL"
#: The parquet `schema_version` this reader understands.
SCHEMA_VERSION = "1"
REQUIRED_COLUMNS = ("key", "size", "last_modified")
#: How old a check may get before a refresh runs.
REFRESH_S = 24 * 3600
#: How long `retry_if_needed` waits after an attempt before another.
RETRY_S = 30
#: How often `retry_if_needed` looks at all (it runs on request paths).
CHECK_EVERY_S = 5.0
FETCH_TIMEOUT_S = 60
#: Limits on the (untrusted) listing. The real one is ~7 MB / ~261k rows.
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
MAX_ROWS = 5_000_000
MAX_KEY_BYTES = 1024  # S3's own limit on a key
#: Rows the open folder chain may hold before they are flushed (`_feed`).
#: The real bucket's largest folder has ~4.4k files.
MAX_OPEN_ROWS = 500_000
BATCH_ROWS = 65_536
#: One `list_dir` page.
PAGE_SIZE = 1000
#: Leftovers of a killed ingest older than this are swept.
STALE_S = 3600
#: The job-registry id (a `sys:` id: the server runs this work).
JOB_ID = "sys:remote-index"
FEATURE = "Fused assets search"

_guard = threading.Lock()
_running = False
_next_check = 0.0  # monotonic; guarded by `_guard`


class IngestError(Exception):
    """A short plain-language reason this ingest failed — recorded in
    state.json and shown to the user, so it never carries a local path."""


# --- layout ---------------------------------------------------------------


def store_dir() -> str:
    """The sibling store for this home (FUSED_RENDER_HOME / branch aware)."""
    return os.path.join(storage.home_dir(), "index-remote", BUCKET)


def _cfg():
    from fused_render.index.config import IndexConfig

    return IndexConfig(dir=store_dir())


def _state_path() -> str:
    return os.path.join(store_dir(), "state.json")


def _read_state() -> dict:
    s = storage.read_json(_state_path())
    return s if isinstance(s, dict) else {}


def _write_state(**fields) -> dict:
    s = _read_state()
    s.update(fields)
    storage.write_json(_state_path(), s)
    return s


def pseudo_root() -> str:
    """`/fused-asset` exactly as `query.search_ranked` normalises a root:
    `norm(os.path.abspath(...))` then trailing slashes stripped. Never a
    bare root, so its `_root_or_bare` step is a no-op here."""
    from fused_render.index.ignore import norm

    return norm(os.path.abspath(os.sep + BUCKET)).rstrip("/")


def source_url() -> str:
    return (os.environ.get(ENV_URL) or "").strip() or INDEX_URL


def _deps_missing() -> bool:
    return not extras.available("index")


def _open_store():
    """(cfg, root, manifest) for a ready store, else None. The manifest is
    read ONCE per call and only the partitions it names are read, the way
    query.py does, so a call racing an ingest answers from one complete
    generation (compaction keeps the previous generation's files on disk)."""
    if _deps_missing():
        return None
    from fused_render.index.store import read_manifest

    cfg = _cfg()
    m = read_manifest(cfg)
    if m is None or not os.path.exists(cfg.dirs_parquet):
        return None
    return cfg, _read_state().get("root") or pseudo_root(), m


# --- status / refresh -------------------------------------------------------


def status() -> dict:
    """`{state, rows, fetched_at, error}` — `state` is one of
    absent | fetching | ready | error. A store that is still serving after a
    failed refresh is `ready` with `error` set; `error` without a store means
    there is nothing to search. `error` is a short plain reason."""
    if _deps_missing():
        return {"state": "error", "rows": 0, "fetched_at": None,
                "error": extras.message("index", FEATURE)}
    s = _read_state()
    ready = _open_store() is not None
    with _guard:
        running = _running
    if running:
        state = "fetching"
    elif ready:
        state = "ready"
    elif s.get("error"):
        state = "error"
    else:
        state = "absent"
    return {"state": state,
            "rows": int(s.get("rows") or 0) if ready else 0,
            "fetched_at": s.get("fetched_at") if ready else None,
            "error": s.get("error") or None}


def start_background(reason: str) -> bool:
    """Start one ingest on a daemon thread. False (and nothing started) when
    one is already running in this process. Never raises."""
    global _running
    with _guard:
        if _running:
            return False
        _running = True
    try:
        threading.Thread(target=_run, args=(reason,), name="remote-index-ingest",
                         daemon=True).start()
    except Exception:  # noqa: BLE001 - a thread that cannot start must not wedge the guard
        logger.exception("remote index: could not start the ingest thread")
        with _guard:
            _running = False
        return False
    return True


def startup_check() -> bool:
    """Start an ingest when the store is absent, its last check is older
    than `REFRESH_S`, or the source changed. Returns whether one was
    started. Never raises."""
    try:
        if _deps_missing():
            return False
        s = _read_state()
        stale = time.time() - float(s.get("checked_at") or 0) > REFRESH_S
        if _open_store() is not None and not stale and s.get("source_url") == source_url():
            return False
    except Exception:  # noqa: BLE001
        logger.exception("remote index: startup check failed")
        return False
    return start_background("startup")


def retry_if_needed() -> None:
    """Cheap, idempotent, never raises. Starts a background ingest when the
    store is not ready (absent / failed) and the last attempt started more
    than `RETRY_S` ago, or a conditional refresh when a ready store's last
    check is older than `REFRESH_S` (a server left running for days). The
    routes that read the index call it on every request, ready or not (Home
    search's `/rank` per keystroke), so it looks at most once per
    `CHECK_EVERY_S`; the readers themselves (`rank`, `list_dir`) never fetch."""
    global _next_check
    try:
        if _deps_missing():
            return
        with _guard:
            if _running:
                return
            now_m = time.monotonic()
            if now_m < _next_check:
                return
            _next_check = now_m + CHECK_EVERY_S
        s = _read_state()
        now = time.time()
        if _open_store() is None:
            if now - float(s.get("attempted_at") or 0) > RETRY_S:
                start_background("retry")
        elif now - float(s.get("checked_at") or 0) > REFRESH_S:
            start_background("refresh")
    except Exception:  # noqa: BLE001
        logger.exception("remote index: retry check failed")


# --- readers ------------------------------------------------------------------


def rank(q: str, limit: int, ranked: bool, token=None, prefix: str | None = None) -> dict:
    """Top `limit` keys matching `q`, as `{ready, hits, total, truncated}`
    with hits `{url, key, is_dir, size, mtime}`. Runs `search_ranked` over
    the sibling store, files and folders together (a folder's key and url
    end in "/", size and mtime None). Raises `index.cancel.Cancelled` when
    `token` is cancelled mid-query.

    `prefix` (None = the whole bucket; "" = the bucket root folder, else a
    folder key ending in "/") scopes the search to that folder's subtree —
    the S3 folder view, which is the explorer's own Listing, so the query
    means exactly what it means in a local folder: it goes through the same
    `resolve_query` (globs, whitespace wildcards, a leading "/" anchoring a
    pattern to the folder itself), and the answer also carries its `mode`
    and `pattern` for the client's highlighting. Keys stay whole bucket
    keys. The whole-bucket search (Home, the launcher) is the address-aware
    one below instead: there a bucket name or an `s3://` spelling may be
    typed."""
    empty = {"ready": False, "hits": [], "total": 0, "truncated": False}
    opened = _open_store()
    if opened is None:
        return empty
    cfg, root, _ = opened
    from fused_render.index.query import search_ranked

    if prefix is not None:
        return _rank_scoped(cfg, root, q, limit, ranked, token, prefix)

    # Keys are matched relative to the bucket, so the bucket's own name is in
    # no key. Typing it (or part of it) must still find the bucket: it is
    # offered as the first hit, key "" and url `s3://<bucket>/`. A query that
    # walks into it (`fused-asset/census`) searches the rest as a key.
    text = (q or "").strip()
    low = text.lower()
    # An address being typed is a search too: `s3://`, or any start of it
    # (`s3:`, `s3:/`), lists what is served, and `s3://<text>` searches
    # `<text>` exactly as if the scheme had not been typed.
    scheme = "s3://"
    if low.startswith(scheme):
        text = text[len(scheme):]
        low = text.lower()
        if not low:
            low = text = BUCKET
    elif len(low) >= 3 and scheme.startswith(low):
        low = text = BUCKET
    bucket_hit = False
    if low == BUCKET or low.startswith(BUCKET + "/"):
        bucket_hit = low.rstrip("/") == BUCKET
        text = text[len(BUCKET) + 1:]
    elif low and low in BUCKET:
        bucket_hit = True
    hits = []
    if bucket_hit and limit > 0:
        hits.append({"url": canonical_url(BUCKET, ""), "key": "",
                     "is_dir": True, "size": None, "mtime": None})
    if not text.strip():
        return {"ready": True, "hits": hits, "total": len(hits),
                "truncated": False}
    out = search_ranked(cfg, root, q=text, limit=max(0, limit - len(hits)),
                        include_dirs=True, token=token, ranked=ranked)
    if not out.get("covered"):
        return empty
    hits.extend(_rank_hit(h) for h in out["hits"])
    return {"ready": True, "hits": hits, "total": len(hits),
            "truncated": bool(out.get("truncated"))}


def _rank_scoped(cfg, root: str, q: str, limit: int, ranked: bool, token,
                 prefix: str) -> dict:
    """`rank` within folder `prefix` — see `rank`."""
    from fused_render.index.query import resolve_query, search_ranked

    if prefix and not prefix.endswith("/"):
        prefix += "/"
    folder = _dir_path(root, prefix)
    resolved = resolve_query(folder, q or "", token=token)
    base, pattern, mode = resolved["base"], resolved["pattern"], resolved["mode"]
    nothing = {"ready": True, "hits": [], "total": 0, "truncated": False,
               "mode": mode, "pattern": pattern}
    if not pattern.strip():
        return nothing
    # A query that walks out of the folder (`~/…`, `..`, an absolute path on
    # this machine) has left the bucket: nothing in this store answers it.
    # The explorer never sends one live — it waits for Enter and opens that
    # folder instead (useListingSearch's commit gate).
    if base != folder and not base.startswith(folder + "/"):
        return nothing
    out = search_ranked(cfg, base, q=pattern, limit=max(0, limit), include_dirs=True,
                        token=token, ranked=ranked, glob=(mode == "glob"))
    if not out.get("covered"):
        # The store is open, so "not covered" is a folder the bucket does not
        # have (every real folder has a dirs row): ready, nothing in it.
        return nothing
    sub = prefix + (base[len(folder) + 1:] + "/" if base != folder else "")
    hits = [_rank_hit(h, sub) for h in out["hits"]]
    return {"ready": True, "hits": hits, "total": len(hits),
            "truncated": bool(out.get("truncated")), "mode": mode, "pattern": pattern}


def _rank_hit(h: dict, prefix: str = "") -> dict:
    """One `search_ranked` hit (its `rel` relative to folder `prefix`) as a
    bucket hit. A folder's key ends in "/"; its synthesised dirs-row mtime is
    not a real object time, so size and mtime are both None."""
    key = prefix + h["rel"]
    if h.get("is_dir"):
        key += "/"
        return {"url": canonical_url(BUCKET, key), "key": key,
                "is_dir": True, "size": None, "mtime": None}
    return {"url": canonical_url(BUCKET, key), "key": key,
            "is_dir": False, "size": h["size"], "mtime": h["mtime"]}


def _sq(s: str) -> str:
    """A SQL string literal's contents (single quotes doubled)."""
    return s.replace("'", "''")


def _dir_path(root: str, prefix: str) -> str:
    """The store's spelling of folder `prefix` ("" = bucket root, otherwise
    ending in "/"). Only the ONE trailing slash is dropped, so a key folder
    with an empty segment (`a//`) keeps its own spelling."""
    if not prefix:
        return root
    if not prefix.endswith("/"):
        prefix += "/"
    return root + "/" + prefix[:-1]


def _connect():
    import duckdb

    from fused_render.index.store import search_threads

    con = duckdb.connect()
    con.execute(f"SET threads TO {search_threads()}")
    return con


def has_dir(prefix: str) -> bool | None:
    """Whether folder `prefix` ("" = bucket root, else ending in "/") exists
    in the index; None when the store is not ready. Every ancestor folder of
    every key has a dirs row (see `_feed`), so this is one exact lookup."""
    opened = _open_store()
    if opened is None:
        return None
    cfg, root, _ = opened
    if not prefix:
        return True
    from fused_render.index.query import dirs_src

    con = _connect()
    try:
        d = _dir_path(root, prefix)
        return con.execute(f"SELECT count(*) FROM {dirs_src(cfg)} "
                           f"WHERE dir = '{_sq(d)}'").fetchone()[0] > 0
    finally:
        con.close()


def has_file(key: str) -> bool | None:
    """Whether an object with exactly `key` is in the index; None when the
    store is not ready. One lookup over the partitions whose path range can
    hold it (`query.prune`). A key may be both a file and a folder prefix
    (`x.zip` and `x.zip/...`): this and `has_dir` answer independently."""
    opened = _open_store()
    if opened is None:
        return None
    if not key or key.endswith("/"):
        return False
    cfg, root, m = opened
    from fused_render.index.query import files_src, prune

    path = root + "/" + key
    parts = prune(m.get("partitions") or [], path)
    if not parts:
        return False
    con = _connect()
    try:
        return con.execute(f"SELECT count(*) FROM {files_src(cfg, parts)} "
                           f"WHERE path = '{_sq(path)}'").fetchone()[0] > 0
    finally:
        con.close()


_CURSOR_RE = re.compile(r"o(\d{1,12})")


def _parse_cursor(cursor) -> int:
    if cursor in (None, ""):
        return 0
    m = _CURSOR_RE.fullmatch(cursor) if isinstance(cursor, str) else None
    if m is None:
        raise ValueError("invalid cursor")
    return int(m.group(1))


def list_dir(prefix: str, cursor: str | None = None,
             limit: int = PAGE_SIZE) -> dict | None:
    """One level of folder `prefix` ("" = bucket root, else ending in "/"):
    `{entries: [{key, name, is_dir, size, mtime}], cursor}`, child folders
    first then files, each by name (byte order), `limit` (at most
    `PAGE_SIZE`) per page. None when the store is not ready. A folder's key
    ends in "/" and carries no size or mtime. `cursor` is opaque (an
    offset, advanced by the entries returned, so pages of different sizes
    chain without gaps); a malformed one raises ValueError. A missing
    folder lists as empty — ask `has_dir` first.

    Child folders come from dirs.parquet (`depth` = one more than the
    folder's, inside the folder's byte range, so row-group statistics skip
    the rest); files from the manifest's partitions pruned to the folder's
    path range (`query.prune`) and filtered on `dir` exactly."""
    offset = _parse_cursor(cursor)
    page = max(1, min(int(limit), PAGE_SIZE))
    opened = _open_store()
    if opened is None:
        return None
    cfg, root, m = opened
    from fused_render.index.query import dirs_src, files_src, prune

    d = _dir_path(root, prefix)
    lo = d + "/"
    hi = d + "0"  # '0' is the code point right after '/': [lo, hi) = inside d
    depth = d.count("/") + 1
    cut = len(lo) + 1  # SQL substr is 1-based
    branches = [
        f"SELECT 0 AS grp, substr(dir, {cut}) AS name, CAST(NULL AS BIGINT) AS size, "
        f"CAST(NULL AS DOUBLE) AS mtime FROM {dirs_src(cfg)} "
        f"WHERE dir >= '{_sq(lo)}' AND dir < '{_sq(hi)}' AND depth = {depth}"]
    parts = prune(m.get("partitions") or [], lo)
    if parts:
        branches.append(
            f"SELECT 1 AS grp, name, size, mtime FROM {files_src(cfg, parts)} "
            f"WHERE path >= '{_sq(lo)}' AND path < '{_sq(hi)}' AND dir = '{_sq(d)}'")
    con = _connect()
    try:
        rows = con.execute(
            "SELECT grp, name, size, mtime FROM (" + " UNION ALL ".join(branches)
            + f") ORDER BY grp, name LIMIT {page + 1} OFFSET {offset}").fetchall()
    finally:
        con.close()
    key_base = d[len(root) + 1:] + "/" if d != root else ""
    entries = []
    for grp, name, size, mtime in rows[:page]:
        is_dir = grp == 0
        entries.append({"key": key_base + name + ("/" if is_dir else ""),
                        "name": name, "is_dir": is_dir,
                        "size": None if is_dir else int(size or 0),
                        "mtime": None if is_dir else float(mtime or 0.0)})
    nxt = f"o{offset + page}" if len(rows) > page else None
    return {"entries": entries, "cursor": nxt}


# --- ingest -------------------------------------------------------------------


def _run(reason: str) -> None:
    global _running
    try:
        _ingest(reason)
    except Exception:  # noqa: BLE001 - _ingest records its own failures
        logger.exception("remote index: ingest crashed")
    finally:
        with _guard:
            _running = False


def _report(state: str, detail: str = "", message: str = "",
            done: float | None = None, total: float | None = None) -> None:
    """Mirror progress into the job registry. Only called once a real
    download has started (never for a 304 / unchanged check). Never raises."""
    try:
        body = {"id": JOB_ID, "title": "Updating Fused assets search",
                "kind": "download", "state": state, "detail": detail,
                "message": message, "origin": "Fused assets", "unit": "bytes"}
        if done is not None:
            body["done"] = done
        if total is not None:
            body["total"] = total
        jobs.upsert(body, server=True)
    except Exception:  # noqa: BLE001 - reporting must never break the work
        logger.debug("remote index: could not report the job")


def _is_local(url: str) -> bool:
    return not url.startswith(("http://", "https://"))


def _local_path(url: str) -> str:
    if url.startswith("file://"):
        return urllib.request.url2pathname(url[len("file://"):])
    return os.path.expanduser(url)


def _sweep(cfg) -> None:
    """Remove a killed ingest's leftovers (download temp files, shard dirs).
    Only stale ones: another process sharing this home may be mid-ingest."""
    cutoff = time.time() - STALE_S
    paths = (glob.glob(os.path.join(glob.escape(cfg.dir), ".download-*.parquet"))
             + glob.glob(os.path.join(glob.escape(cfg.runs_dir), "remote-*")))
    for p in paths:
        try:
            if os.path.getmtime(p) >= cutoff:
                continue
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.unlink(p)
        except OSError:
            pass


def _ingest(reason: str) -> dict:
    """One ingest, in the calling thread (`_run` is the only caller besides a
    hand-run shell). Returns the recorded state; never raises for a bad
    source — the reason lands in `state.json["error"]`."""
    url = source_url()
    now = time.time()
    if _deps_missing():
        return _write_state(checked_at=now, attempted_at=now,
                            error=extras.message("index", FEATURE))
    cfg = _cfg()
    os.makedirs(cfg.dir, exist_ok=True)
    _sweep(cfg)
    prev = _write_state(attempted_at=now)
    can_skip = _open_store() is not None and prev.get("source_url") == url
    tmp = os.path.join(cfg.dir, f".download-{uuid.uuid4().hex}.parquet")
    # Set by `_download` the moment it opens a job row (never for a 304 or
    # an unreachable host), so every later outcome closes that row.
    progress = {"reported": False}
    try:
        if _is_local(url):
            try:
                st = os.stat(_local_path(url))
            except OSError:
                raise IngestError("index file not found") from None
            etag = f"local-{st.st_size}-{st.st_mtime_ns}"
            if can_skip and prev.get("etag") == etag:
                return _write_state(checked_at=now, error=None)
            if st.st_size > MAX_DOWNLOAD_BYTES:
                raise IngestError("the Fused assets index is unexpectedly large")
            shutil.copyfile(_local_path(url), tmp)
        else:
            etag = _download(url, tmp, prev.get("etag") if can_skip else None,
                             progress)
            if etag is None:  # 304 Not Modified
                return _write_state(checked_at=now, error=None)
        size = os.path.getsize(tmp)
        root = pseudo_root()
        rows = _load(cfg, tmp, root)
        st = _write_state(source_url=url, etag=etag, rows=rows, root=root,
                          fetched_at=time.time(), checked_at=time.time(),
                          error=None, bytes=size, reason=reason)
        if progress["reported"]:
            _report("done", detail=f"{rows:,} files")
        logger.info("remote index: ingested %d rows from %s (%s)", rows, url, reason)
        return st
    except Exception as e:  # noqa: BLE001 - recorded, previous store keeps serving
        if isinstance(e, IngestError):
            msg = str(e)
            logger.warning("remote index: ingest from %s failed: %s", url, msg)
        else:
            # Details (which may name local paths) stay in the log.
            msg = "could not load the Fused assets index"
            logger.exception("remote index: ingest from %s failed", url)
        if progress["reported"]:
            _report("error", message=msg)
        return _write_state(checked_at=time.time(), error=msg)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _download(url: str, dest: str, etag: str | None, progress: dict) -> str | None:
    """GET `url` into `dest` without following redirects. Returns the
    response ETag (raw, quotes kept, so it can be sent back verbatim), or
    None on 304. Sets `progress["reported"]` once a job row exists."""
    headers = {"User-Agent": "fused-render"}
    if etag:
        headers["If-None-Match"] = etag
    try:
        with httpx.stream("GET", url, headers=headers, timeout=FETCH_TIMEOUT_S,
                          follow_redirects=False) as resp:
            if resp.status_code == 304:
                return None
            if resp.is_redirect:
                raise IngestError("the Fused assets index URL redirected; refusing it")
            if resp.status_code in (403, 404):
                raise IngestError("the Fused assets index is not published yet")
            if resp.status_code != 200:
                raise IngestError(f"could not download the Fused assets index "
                                  f"(HTTP {resp.status_code})")
            new_etag = resp.headers.get("ETag") or ""
            total = resp.headers.get("Content-Length")
            total = int(total) if total and total.isdigit() else None
            if total is not None and total > MAX_DOWNLOAD_BYTES:
                raise IngestError("the Fused assets index is unexpectedly large")
            progress["reported"] = True
            _report("running", detail="downloading", done=0, total=total)
            # Content-Length counts the ENCODED body; compare only when the
            # body is not content-encoded (S3 serves parquet as is).
            if resp.headers.get("Content-Encoding"):
                total = None
            done, last = 0, 0.0
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes(1 << 20):
                    done += len(chunk)
                    if done > MAX_DOWNLOAD_BYTES:
                        raise IngestError("the Fused assets index is unexpectedly large")
                    f.write(chunk)
                    if time.monotonic() - last > 0.5:
                        last = time.monotonic()
                        _report("running", detail="downloading", done=done, total=total)
            if total is not None and done != total:
                raise IngestError("the Fused assets index download was cut short")
    except httpx.HTTPError as e:
        logger.warning("remote index: fetching %s failed: %s", url, e)
        raise IngestError("could not reach the Fused assets index") from None
    _report("running", detail="indexing", done=done, total=done)
    return new_etag or f"len-{done}"


def _validate(pf) -> list:
    """The column names to read, or IngestError."""
    meta = pf.schema_arrow.metadata or {}
    ver = (meta.get(b"schema_version") or b"").decode("utf-8", "replace")
    if ver != SCHEMA_VERSION:
        raise IngestError("the Fused assets index has an unsupported format version")
    names = pf.schema_arrow.names
    missing = [c for c in REQUIRED_COLUMNS if c not in names]
    if missing:
        raise IngestError(f"the Fused assets index is missing column(s): "
                          f"{', '.join(missing)}")
    n = pf.metadata.num_rows
    if n == 0:
        raise IngestError("the Fused assets index is empty")
    if n > MAX_ROWS:
        raise IngestError("the Fused assets index has too many rows")
    return list(REQUIRED_COLUMNS) + (["etag"] if "etag" in names else [])


def _load(cfg, parquet_path: str, root: str) -> int:
    """Validate `parquet_path`, feed every key through a `Sink` and compact
    it into the store under `root`. Returns the row count."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from fused_render.index import store

    try:
        pf = pq.ParquetFile(parquet_path)
    except Exception:  # noqa: BLE001 - pyarrow's message names the temp path
        raise IngestError("the Fused assets index is not valid parquet") from None
    cols = _validate(pf)
    shards_dir = os.path.join(cfg.runs_dir, f"remote-{uuid.uuid4().hex}")
    os.makedirs(shards_dir, exist_ok=True)
    try:
        sink = store.Sink(shards_dir, "remote", pa, pq, cfg.shard_rows)
        rows = _feed(sink, pf, cols, root, pa)
        sink.close()
        if store.compact(cfg, root, shards_dir, pa, pq) is None:
            raise IngestError("the index store is busy; try again")
        return rows
    finally:
        shutil.rmtree(shards_dir, ignore_errors=True)


class _Dir:
    __slots__ = ("path", "frows", "total", "mtime_ns", "n_subdirs", "h")

    def __init__(self, path: str):
        self.path = path
        self.frows: list = []
        self.total = 0
        self.mtime_ns = 0
        self.n_subdirs = 0
        self.h = hashlib.sha1()


def _feed(sink, pf, cols: list, root: str, pa) -> int:
    """Stream the (key-sorted) listing into `sink`, one `add` per folder.

    Keys under one folder prefix are contiguous in sorted order, but a
    folder's DIRECT files are not (`a/b.txt`, `a/c/d.txt`, `a/e.txt`), and
    the first-ingest compaction does not dedupe dirs rows — so folders are
    kept on a stack of the current key's ancestors and each is flushed once,
    when the stream leaves its prefix. Memory is one batch plus the open
    chain (capped at `MAX_OPEN_ROWS`), never the whole listing.

    Every row is checked first (see the module docstring); keys must be
    STRICTLY increasing, which the stack needs and which also rejects
    duplicates on the second copy.

    Rows mirror `index/scan.py`: path, dir, name, ext (lowercased, no dot),
    size, mtime (seconds). Pure string work — never `normpath` — so a hit's
    `rel` is the key verbatim."""
    stack = [_Dir(root)]
    open_rows = 0
    rows = 0
    prev_key = None
    has_etag = "etag" in cols
    ms = pa.timestamp("ms", tz="UTC")
    lm_type = pf.schema_arrow.field("last_modified").type
    if not pa.types.is_timestamp(lm_type):
        raise IngestError("the Fused assets index has a malformed last_modified column")

    def flush(d: _Dir) -> None:
        nonlocal open_rows
        open_rows -= len(d.frows)
        sink.add(d.path, "s", (d.h.hexdigest(), d.frows, d.total, d.mtime_ns,
                               d.n_subdirs))

    for b in pf.iter_batches(batch_size=BATCH_ROWS, columns=cols):
        keys = b.column("key").to_pylist()
        sizes = b.column("size").cast(pa.int64()).to_pylist()
        mtimes = b.column("last_modified").cast(ms).cast(pa.int64()).to_pylist()
        etags = b.column("etag").to_pylist() if has_etag else [None] * len(keys)
        for key, size, mt_ms, etag in zip(keys, sizes, mtimes, etags):
            if (not key or len(key) > MAX_KEY_BYTES or "\x00" in key
                    or len(key.encode("utf-8", "surrogatepass")) > MAX_KEY_BYTES):
                raise IngestError("the Fused assets index has a malformed key")
            if size is None or size < 0:
                raise IngestError("the Fused assets index has a malformed size")
            if prev_key is not None and key <= prev_key:
                raise IngestError("the Fused assets index is not sorted by key")
            prev_key = key
            if key.endswith("/"):
                continue  # folder marker: folders are implied by keys
            path = root + "/" + key
            d, _, name = path.rpartition("/")
            # Leave every open folder this key is not inside.
            while stack[-1].path != d and not d.startswith(stack[-1].path + "/"):
                flush(stack.pop())
            # Open the folders between the innermost open one and `d`.
            while stack[-1].path != d:
                top = stack[-1]
                end = d.find("/", len(top.path) + 1)
                child = d if end == -1 else d[:end]
                top.n_subdirs += 1
                top.h.update(f"{child[len(top.path) + 1:]}/|0|0\n"
                             .encode("utf-8", "replace"))
                stack.append(_Dir(child))
            mt_ns = int(mt_ms or 0) * 1_000_000
            cur = stack[-1]
            cur.frows.append((path, d, name,
                              posixpath.splitext(name)[1].lower().lstrip("."),
                              size, mt_ns / 1e9))
            cur.total += size
            cur.mtime_ns = max(cur.mtime_ns, mt_ns)
            cur.h.update(f"{name}|{size}|{etag or mt_ns}\n".encode("utf-8", "replace"))
            open_rows += 1
            rows += 1
            if open_rows > MAX_OPEN_ROWS:
                raise IngestError("the Fused assets index has a folder too large to load")
    while stack:
        flush(stack.pop())
    if rows == 0:
        raise IngestError("the Fused assets index has no object keys")
    return rows
