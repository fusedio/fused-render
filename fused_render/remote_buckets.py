"""Bucket indexes: which ones this account can read, and keeping each one's
local store at the server's version.

Three parts, top to bottom:

**Gateway.** The only code that talks to the Fused server about indexes:
`manifest()`, `snapshot(entry, dest)`, `changes(entry, since, dest)` and
`sign(entry, bucket, key)`. `FusedGateway` is the real one (fused-py, through
`_fused_bucket_index.py`). `DevGateway` reads a folder a script fills
(`scripts/bucket_index_dev.py`) and answers the same four calls, so the sync
below runs for real before the server routes exist; it is chosen by
`FUSED_RENDER_BUCKET_INDEX_DEV=<folder>`.

**Registry.** The manifest as last fetched, kept in
`<home>/index-remote/<env>/<account>/manifest.json`, and two lookups over it:
`indexes()` and `resolve(bucket, key)` (the index whose prefix covers the
address, longest first). Nothing else in fused-render names a bucket.

**Sync.** One store per index under `<...>/<index id>/`, in the store format
the readers in `remote_index.py` already use (files partitions, dirs.parquet,
partitions.json). First a snapshot, afterwards "changes since my version":
old rows whose key is not in the changes, plus the changes that are not
deleted. The whole store is rewritten by one DuckDB pass (`_write_store`) and
the manifest is swapped last, so readers keep answering from the previous
generation. `state.json` records the version after the swap; a crash between
the two re-applies the same changes on the next pass, which lands on the same
rows. One pass runs at a time, on one daemon thread, every `POLL_S`.

The store writer is its own DuckDB pass rather than `store.Sink` +
`store.compact`: those were sized for the local-disk index and run out of
memory on a 30-million-row bucket (measured 2026-11-27).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import quote

from fused_render import extras
from fused_render.remote_index import Index, pseudo_root
from fused_render.shell import storage

logger = logging.getLogger(__name__)

ENV_DEV = "FUSED_RENDER_BUCKET_INDEX_DEV"
ENV_POLL = "FUSED_RENDER_BUCKET_INDEX_POLL_S"
#: How often the manifest is read and moved indexes are synced.
POLL_S = 15 * 60
#: Rows per partition file. A million keeps a 30-million-row bucket at 30
#: files; `query.prune` skips the ones a folder or prefix cannot be in.
PART_ROWS = 1_000_000
SHIM_TIMEOUT = 60.0
DOWNLOAD_TIMEOUT = 120.0
#: A signed link is reused until this long before it expires.
SIGN_MARGIN_S = 300.0
SIGN_TTL_S = 3600.0
FEATURE = "S3 search"
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class GatewayError(Exception):
    """A short plain reason, safe to show (no local paths, no links)."""


# --- gateways -----------------------------------------------------------------


def _clean_entry(e) -> dict | None:
    """A manifest row with the fields the rest of this module reads, or None
    for one it cannot use (the id becomes a folder name, so it is checked)."""
    if not isinstance(e, dict):
        return None
    ident = str(e.get("id") or "")
    bucket = str(e.get("bucket") or e.get("bucket_name") or "")
    if not _ID_RE.fullmatch(ident) or not bucket or "/" in bucket:
        return None
    prefix = str(e.get("prefix") or "")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    version = e.get("version")
    return {"id": ident, "bucket": bucket, "prefix": prefix,
            "is_public": bool(e.get("is_public")),
            "version": int(version) if isinstance(version, int) else None,
            "region": str(e.get("region") or "") or None,
            "objects": e.get("objects") if isinstance(e.get("objects"), int) else None}


class DevGateway:
    """The plan's contract answered from a local folder:

        <dir>/manifest.json      {"indexes": [{id, bucket, prefix, is_public,
                                               region, version, oldest_version}]}
        <dir>/<id>/live.parquet  key, size, last_modified, ver, deleted

    `live.parquet` stands in for the server's table: one row per key, the
    version that last touched it, and a deleted marker. A snapshot is its
    non-deleted rows; "changes since v" is its rows with `ver > v`."""

    scope = ("dev", "dev")

    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))

    def _raw(self) -> list:
        data = storage.read_json(os.path.join(self.root, "manifest.json"))
        rows = data.get("indexes") if isinstance(data, dict) else None
        return rows if isinstance(rows, list) else []

    def manifest(self) -> list[dict]:
        return [c for c in (_clean_entry(e) for e in self._raw()) if c]

    def _live(self, entry: dict) -> str:
        path = os.path.join(self.root, entry["id"], "live.parquet")
        if not os.path.isfile(path):
            raise GatewayError("the index has no snapshot yet")
        return path

    def snapshot(self, entry: dict, dest: str) -> int | None:
        import duckdb

        live = self._live(entry)
        version = entry["version"]
        if version is None:
            return None
        con = duckdb.connect()
        try:
            con.execute(
                f"COPY (SELECT key, size, last_modified FROM read_parquet('{_sq(live)}') "
                f"WHERE NOT deleted AND ver <= {int(version)} ORDER BY key) "
                f"TO '{_sq(dest)}' (FORMAT parquet, COMPRESSION zstd)")
        finally:
            con.close()
        return version

    def changes(self, entry: dict, since: int, dest: str) -> tuple[str, int | None]:
        import duckdb

        version = entry["version"]
        oldest = next((e.get("oldest_version") for e in self._raw()
                       if isinstance(e, dict) and e.get("id") == entry["id"]), None)
        if version is None:
            return "missing", None
        if since == version:
            return "current", version
        if since > version or (isinstance(oldest, int) and since < oldest):
            return "resnapshot", version  # the plan's 409 and 410
        live = self._live(entry)
        con = duckdb.connect()
        try:
            con.execute(
                f"COPY (SELECT key, size, last_modified, deleted FROM read_parquet('{_sq(live)}') "
                f"WHERE ver > {int(since)} AND ver <= {int(version)} ORDER BY key) "
                f"TO '{_sq(dest)}' (FORMAT parquet, COMPRESSION zstd)")
        finally:
            con.close()
        return "changes", version

    def sign(self, entry: dict, bucket: str, key: str) -> tuple[str, float | None]:
        if entry.get("is_public") and entry.get("region"):
            return (f"https://{bucket}.s3.{entry['region']}.amazonaws.com/"
                    f"{quote(key, safe='/~')}"), None
        # A private bucket seeded into the dev folder is still signed for real.
        return _fused_sign(_auth_env(), bucket, key)


class FusedGateway:
    """The signed-in account's indexes, through fused-py."""

    def __init__(self, env: str, account: str):
        self.env = env
        self.scope = (env, account)

    def manifest(self) -> list[dict]:
        out = _shim(self.env, "manifest", {})
        return [c for c in (_clean_entry(e) for e in out.get("indexes") or []) if c]

    def _download(self, entry: dict, since: int | None, dest: str) -> tuple[int, int | None]:
        import httpx

        out = _shim(self.env, "resolve", {"index_id": entry["id"], "since": since})
        status, url = int(out.get("status") or 0), out.get("url")
        if status == 302 and isinstance(url, str) and url.startswith("https://"):
            try:
                with httpx.stream("GET", url, timeout=DOWNLOAD_TIMEOUT,
                                  follow_redirects=False) as r:
                    if r.status_code != 200:
                        raise GatewayError(f"the index download answered HTTP {r.status_code}")
                    with open(dest, "wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            f.write(chunk)
            except httpx.HTTPError:
                raise GatewayError("could not download the index") from None
        return status, out.get("version")

    def snapshot(self, entry: dict, dest: str) -> int | None:
        status, version = self._download(entry, None, dest)
        if status == 404:
            return None
        if status != 302:
            raise GatewayError(f"the snapshot request answered HTTP {status}")
        return version if version is not None else entry["version"]

    def changes(self, entry: dict, since: int, dest: str) -> tuple[str, int | None]:
        status, version = self._download(entry, since, dest)
        if status == 204:
            return "current", version if version is not None else since
        if status == 302:
            return "changes", version if version is not None else entry["version"]
        if status in (409, 410):
            return "resnapshot", version
        if status == 404:
            return "missing", None
        raise GatewayError(f"the changes request answered HTTP {status}")

    def sign(self, entry: dict, bucket: str, key: str) -> tuple[str, float | None]:
        return _fused_sign(self.env, bucket, key)


def _auth_env() -> str:
    from fused_render import canvases

    return canvases._auth_env()


def _shim(env: str, op: str, args: dict) -> dict:
    """Run `_fused_bucket_index.py <op>` as the signed-in user; its JSON."""
    from fused_render import canvases
    from fused_render.fusedcli import fused_cli

    cli = fused_cli()
    if cli is None or cli.external:
        raise GatewayError("the Fused package is not available")
    shim = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fused_bucket_index.py")
    try:
        proc = subprocess.run([sys.executable, shim, op], input=json.dumps(args),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=SHIM_TIMEOUT,
                              env=canvases._cli_env(cli, env), close_fds=False)
    except subprocess.TimeoutExpired:
        raise GatewayError("the Fused server did not answer in time") from None
    except OSError:
        raise GatewayError("could not run the Fused helper") from None
    if proc.returncode != 0:
        # stderr may hold a traceback with local paths: logged, never shown.
        logger.warning("bucket index shim %s failed: %s", op, proc.stderr[-2000:])
        raise GatewayError("the Fused server refused the request")
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise GatewayError("the Fused server sent an unreadable answer") from None
    return out if isinstance(out, dict) else {}


def _fused_sign(env: str, bucket: str, key: str) -> tuple[str, float | None]:
    out = _shim(env, "sign", {"path": f"s3://{bucket}/{key}"})
    url = out.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise GatewayError("the Fused server did not sign the object")
    # A presigned link carries its own lifetime; a public one never expires.
    return url, (time.time() + SIGN_TTL_S if "X-Amz-" in url else None)


def gateway():
    """The gateway for whoever is signed in, or None when nobody is."""
    dev = (os.environ.get(ENV_DEV) or "").strip()
    if dev:
        return DevGateway(dev)
    try:
        from fused_render import canvases

        env = _workbench_env()
        if not canvases._logged_in(env):
            return None
        account = canvases._listing_account(env)
        return FusedGateway(env, account[:16]) if account else None
    except Exception:  # noqa: BLE001 - "cannot tell who" reads as signed out
        logger.debug("bucket index: no gateway", exc_info=True)
        return None


def _workbench_env() -> str:
    from fused_render.fusedcli import workbench_env

    return workbench_env()


# --- registry -------------------------------------------------------------------

_guard = threading.Lock()
_cache: tuple[tuple, list[dict]] | None = None  # (scope, entries)
_sign_cache: dict[tuple, tuple[str, float | None]] = {}


def base_dir(scope: tuple) -> str:
    return os.path.join(storage.home_dir(), "index-remote", scope[0], scope[1])


def _entries(gw=None) -> list[dict]:
    """The saved manifest for the current account (memory, then disk)."""
    global _cache
    gw = gw or gateway()
    if gw is None:
        return []
    with _guard:
        if _cache is not None and _cache[0] == gw.scope:
            return _cache[1]
    data = storage.read_json(os.path.join(base_dir(gw.scope), "manifest.json"))
    rows = data.get("indexes") if isinstance(data, dict) else None
    entries = [c for c in (_clean_entry(e) for e in rows or []) if c]
    with _guard:
        _cache = (gw.scope, entries)
    return entries


def _index(scope: tuple, e: dict) -> Index:
    return Index(id=e["id"], bucket=e["bucket"], prefix=e["prefix"],
                 is_public=e["is_public"], dir=os.path.join(base_dir(scope), e["id"]),
                 root=pseudo_root(e["bucket"]))


def indexes() -> list[Index]:
    """Every index the signed-in account can read, public ones first."""
    gw = gateway()
    if gw is None:
        return []
    rows = sorted(_entries(gw), key=lambda e: (not e["is_public"], e["bucket"], e["prefix"]))
    return [_index(gw.scope, e) for e in rows]


def signed_in() -> bool:
    return gateway() is not None


def resolve(bucket: str, key: str = "") -> Index | None:
    """The index that answers for `s3://bucket/key`: the one with the longest
    prefix the key is under, else one whose prefix lies below the key (the
    folders leading down to an indexed prefix exist too). None when the
    account has no index there."""
    under, above = None, None
    for ix in indexes():
        if ix.bucket != bucket:
            continue
        if key.startswith(ix.prefix):
            if under is None or len(ix.prefix) > len(under.prefix):
                under = ix
        elif ix.prefix.startswith(key) and above is None:
            above = ix
    return under or above


def sign(bucket: str, key: str) -> tuple[str, float | None]:
    """`(https url, expires_at or None)` that reads `s3://bucket/key` with
    plain GETs. Kept in memory until near its expiry; never written or
    logged. Raises GatewayError."""
    gw = gateway()
    ix = resolve(bucket, key)
    if gw is None or ix is None:
        raise GatewayError("that bucket is not indexed for this account")
    cache_key = (gw.scope, bucket, key)
    with _guard:
        hit = _sign_cache.get(cache_key)
        if hit and (hit[1] is None or hit[1] - time.time() > SIGN_MARGIN_S):
            return hit
    entry = next((e for e in _entries(gw) if e["id"] == ix.id), None)
    if entry is None:
        raise GatewayError("that bucket is not indexed for this account")
    signed = gw.sign(entry, bucket, key)
    with _guard:
        if len(_sign_cache) > 2000:
            _sign_cache.clear()
        _sign_cache[cache_key] = signed
    return signed


# --- sync -------------------------------------------------------------------------

_wake = threading.Event()
_thread: threading.Thread | None = None
_syncing: set[str] = set()
_last_pass = 0.0
_pass_error: str | None = None


def _poll_s() -> float:
    try:
        return max(5.0, float(os.environ.get(ENV_POLL) or POLL_S))
    except ValueError:
        return float(POLL_S)


def start(reason: str = "startup") -> None:
    """Start the sync thread once, and run a pass now. Never raises."""
    global _thread
    try:
        with _guard:
            if _thread is None or not _thread.is_alive():
                _thread = threading.Thread(target=_loop, name="bucket-index-sync", daemon=True)
                _thread.start()
        _wake.set()
    except Exception:  # noqa: BLE001
        logger.exception("bucket index: could not start the sync thread (%s)", reason)


def kick() -> None:
    """Ask for a pass now ("refresh now", a route that found no store)."""
    start("kick")


def nudge() -> None:
    """Cheap, for request paths: make sure the thread is running, and wake it
    when nothing has ever been fetched for this account."""
    if _thread is None or not _thread.is_alive():
        start("request")
    elif not _entries() and time.time() - _last_pass > 30:
        _wake.set()


def _loop() -> None:
    while True:
        _wake.wait(_poll_s())
        _wake.clear()
        try:
            sync_pass()
        except Exception:  # noqa: BLE001 - the loop must outlive any one pass
            logger.exception("bucket index: sync pass crashed")


def sync_pass() -> None:
    """Read the manifest, drop stores that left it, sync every index whose
    version moved, one after another. Runs in the calling thread."""
    global _cache, _last_pass, _pass_error
    _last_pass = time.time()
    from fused_render.shell import prefs

    if not prefs.canvases_enabled() or not extras.available("index"):
        return
    gw = gateway()
    if gw is None:
        return
    try:
        entries = gw.manifest()
    except GatewayError as e:
        _pass_error = str(e)
        logger.warning("bucket index: manifest failed: %s", e)
        return
    except Exception:  # noqa: BLE001
        _pass_error = "could not read the list of indexed buckets"
        logger.exception("bucket index: manifest failed")
        return
    _pass_error = None
    base = base_dir(gw.scope)
    os.makedirs(base, exist_ok=True)
    storage.write_json(os.path.join(base, "manifest.json"), {"indexes": entries})
    with _guard:
        _cache = (gw.scope, entries)
    keep = {e["id"] for e in entries} | {"manifest.json"}
    for name in os.listdir(base):
        if name not in keep:
            shutil.rmtree(os.path.join(base, name), ignore_errors=True)
    for e in entries:
        ix = _index(gw.scope, e)
        with _guard:
            _syncing.add(ix.id)
        try:
            _sync_one(gw, e, ix)
        except Exception as err:  # noqa: BLE001 - recorded; the old store keeps serving
            msg = str(err) if isinstance(err, GatewayError) else "could not update this index"
            if not isinstance(err, GatewayError):
                logger.exception("bucket index: sync of %s failed", ix.id)
            _state_write(ix, checked_at=time.time(), error=msg)
            _report(ix, "error", message=msg)
        finally:
            with _guard:
                _syncing.discard(ix.id)


def _state_read(ix: Index) -> dict:
    s = storage.read_json(os.path.join(ix.dir, "state.json"))
    return s if isinstance(s, dict) else {}


def _state_write(ix: Index, **fields) -> dict:
    os.makedirs(ix.dir, exist_ok=True)
    s = _state_read(ix)
    s.update(fields)
    storage.write_json(os.path.join(ix.dir, "state.json"), s)
    return s


def _report(ix: Index, state: str, detail: str = "", message: str = "") -> None:
    try:
        from fused_render import jobs

        jobs.upsert({"id": f"sys:remote-index:{ix.id}", "kind": "download", "state": state,
                     "title": f"Updating S3 search: {ix.bucket}", "detail": detail,
                     "message": message, "origin": "S3"}, server=True)
    except Exception:  # noqa: BLE001 - reporting must never break the work
        logger.debug("bucket index: could not report the job", exc_info=True)


def _sync_one(gw, e: dict, ix: Index) -> None:
    from fused_render import remote_index

    st = _state_read(ix)
    have = remote_index.open_store(ix) is not None and isinstance(st.get("version"), int)
    now = time.time()
    if have and st["version"] == e["version"]:
        _state_write(ix, checked_at=now, error=None)
        return
    os.makedirs(ix.dir, exist_ok=True)
    tmp = os.path.join(ix.dir, f".sync-{uuid.uuid4().hex}")
    os.makedirs(tmp)
    try:
        if have:
            path = os.path.join(tmp, "changes.parquet")
            status, version = gw.changes(e, st["version"], path)
            if status == "current":
                _state_write(ix, checked_at=now, error=None)
                return
            if status == "missing":
                return
            if status == "changes":
                _report(ix, "running", detail="applying changes")
                rows = _write_store(ix, changes=path)
                _state_write(ix, version=version, rows=rows, synced_at=time.time(),
                             checked_at=time.time(), error=None, last="changes")
                _report(ix, "done", detail=f"{rows:,} files")
                logger.info("bucket index: %s -> v%s (%d rows, changes)", ix.id, version, rows)
                return
            # "resnapshot": ahead of the server or too far behind; start over.
        path = os.path.join(tmp, "snapshot.parquet")
        _report(ix, "running", detail="downloading")
        version = gw.snapshot(e, path)
        if version is None:
            return  # no snapshot yet; the next pass asks again
        _report(ix, "running", detail="indexing")
        rows = _write_store(ix, snapshot=path)
        _state_write(ix, version=version, rows=rows, synced_at=time.time(),
                     checked_at=time.time(), error=None, last="snapshot")
        _report(ix, "done", detail=f"{rows:,} files")
        logger.info("bucket index: %s -> v%s (%d rows, snapshot)", ix.id, version, rows)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _sq(s: str) -> str:
    return s.replace("'", "''")


def _write_store(ix: Index, *, snapshot: str | None = None, changes: str | None = None) -> int:
    """Write a whole new generation of `ix`'s store and swap it in. From a
    snapshot file, or from the current store with a changes file applied.
    Returns the row count."""
    from fused_render import remote_index
    from fused_render.index import query, store

    cfg = remote_index.cfg_for(ix)
    root = _sq(ix.root)
    os.makedirs(cfg.files_dir, exist_ok=True)
    with store.store_lock(cfg):
        old = store.read_manifest(cfg) or {}
        generation = int(old.get("generation") or 0) + 1
        con = store.background_connect(os.path.join(cfg.runs_dir, "tmp"))
        try:
            if snapshot is not None:
                src = (f"SELECT key, size, epoch(last_modified) AS mtime "
                       f"FROM read_parquet('{_sq(snapshot)}')")
            else:
                cut = len(ix.root) + 2  # SQL substr is 1-based; skip "<root>/"
                parts = old.get("partitions") or []
                prev = (f"SELECT substr(path, {cut}) AS key, size, mtime "
                        f"FROM {query.files_src(cfg, parts)}" if parts
                        else "SELECT NULL::VARCHAR AS key, 0::BIGINT AS size, 0::DOUBLE AS mtime WHERE false")
                ch = f"read_parquet('{_sq(changes)}')"
                src = (f"SELECT p.* FROM ({prev}) p ANTI JOIN {ch} c USING (key) "
                       f"UNION ALL SELECT key, size, epoch(last_modified) AS mtime "
                       f"FROM {ch} WHERE NOT deleted")
            # A key ending in "/" is a folder marker: folders come from keys.
            con.execute(
                "CREATE TEMP TABLE merged AS SELECT key, size::BIGINT AS size, "
                f"coalesce(mtime, 0)::DOUBLE AS mtime FROM ({src}) "
                "WHERE key IS NOT NULL AND key <> '' AND NOT ends_with(key, '/') ORDER BY key")
            total = con.execute("SELECT count(*) FROM merged").fetchone()[0]
            # `rel` is the folder part of the key ('' at the bucket root).
            row = ("SELECT '{r}/' || key AS path, "
                   "CASE WHEN rel = '' THEN '{r}' ELSE '{r}/' || rel END AS dir, "
                   "name, lower(regexp_extract(name, '^\\.*[^.].*\\.([^.]*)$', 1)) AS ext, "
                   "size, mtime, (len('{r}/' || key) - len(replace('{r}/' || key, '/', '')))::INTEGER AS depth, rid "
                   "FROM (SELECT key, size, mtime, rowid AS rid, "
                   "regexp_extract(key, '[^/]*$') AS name, "
                   "CASE WHEN contains(key, '/') THEN regexp_replace(key, '/[^/]*$', '') ELSE '' END AS rel "
                   "FROM merged)").replace("{r}", root)
            parts = []
            for i in range(-(-total // PART_ROWS)):
                name = f"part-{generation:06d}-000-{i:05d}.parquet"
                fp = _sq(os.path.join(cfg.files_dir, name))
                con.execute(
                    f"COPY (SELECT path, dir, name, ext, size, mtime, depth FROM ({row}) "
                    f"WHERE rid >= {i * PART_ROWS} AND rid < {(i + 1) * PART_ROWS} ORDER BY path) "
                    f"TO '{fp}' (FORMAT PARQUET, ROW_GROUP_SIZE 65536)")
                lo, hi, lo_f, hi_f, n = con.execute(
                    f"SELECT min(path), max(path), min(lower(path)), max(lower(path)), count(*) "
                    f"FROM read_parquet('{fp}')").fetchone()
                parts.append({"file": name, "min": lo, "max": hi,
                              "min_lower": lo_f, "max_lower": hi_f, "rows": n})
            # One row per folder that holds a file anywhere below it, the
            # bucket root included (the readers' coverage test needs it).
            con.execute(
                "COPY (WITH f AS (SELECT string_split(key, '/') AS p, size, mtime FROM merged), "
                "d AS (SELECT array_to_string(p[1:len(p) - 1], '/') AS rel, count(*) AS n, "
                "sum(size) AS s, max(mtime) AS m FROM f GROUP BY 1), "
                "anc AS (SELECT DISTINCT array_to_string(q[1:g], '/') AS rel FROM "
                "(SELECT DISTINCT p[1:len(p) - 1] AS q FROM f), range(0, len(q) + 1) t(g) "
                "UNION SELECT ''), "
                "sub AS (SELECT CASE WHEN contains(rel, '/') THEN regexp_replace(rel, '/[^/]*$', '') "
                "ELSE '' END AS parent, count(*) AS c FROM anc WHERE rel <> '' GROUP BY 1) "
                f"SELECT CASE WHEN a.rel = '' THEN '{root}' ELSE '{root}/' || a.rel END AS dir, "
                f"'g{generation}' AS sig, coalesce(d.n, 0)::INTEGER AS n_files, "
                "coalesce(d.s, 0)::BIGINT AS total_size, (coalesce(d.m, 0) * 1e9)::BIGINT AS mtime_ns, "
                "coalesce(sub.c, 0)::INTEGER AS n_subdirs, "
                f"(CASE WHEN a.rel = '' THEN 0 ELSE 1 + len(a.rel) - len(replace(a.rel, '/', '')) END "
                f"+ {ix.root.count('/')})::INTEGER AS depth "
                "FROM anc a LEFT JOIN d ON d.rel = a.rel LEFT JOIN sub ON sub.parent = a.rel "
                f"ORDER BY dir) TO '{_sq(cfg.dirs_parquet)}.new' (FORMAT PARQUET)")
        finally:
            con.close()
        # The swap: dirs first, the manifest last. Until the manifest lands
        # every reader answers from the previous generation's files.
        os.replace(cfg.dirs_parquet + ".new", cfg.dirs_parquet)
        store._write_manifest(cfg, {"updated": time.time(), "last_root": ix.root,
                                    "rows": total, "partitions": parts,
                                    "generation": generation})
        store._reclaim_partitions(cfg, keep=[p["file"] for p in parts]
                                  + [p["file"] for p in old.get("partitions") or []])
        query.forget_src_cols_for(cfg.dir)
    return int(total)


# --- status -------------------------------------------------------------------------


def status() -> dict:
    """`{signed_in, state, rows, fetched_at, error, indexes: [...]}`. The
    top-level fields sum the indexes, for callers that only ask "is S3 search
    usable": `state` is absent | fetching | ready | error."""
    from fused_render import remote_index

    if not extras.available("index"):
        return {"signed_in": signed_in(), "state": "error", "rows": 0, "fetched_at": None,
                "error": extras.message("index", FEATURE), "indexes": []}
    with _guard:
        syncing = set(_syncing)
    rows = []
    for ix in indexes():
        st = _state_read(ix)
        ready = remote_index.open_store(ix) is not None
        state = ("fetching" if ix.id in syncing and not ready else "ready" if ready
                 else "error" if st.get("error") else "absent")
        rows.append({"id": ix.id, "bucket": ix.bucket, "prefix": ix.prefix,
                     "url": f"s3://{ix.bucket}/{ix.prefix}", "is_public": ix.is_public,
                     "state": state, "syncing": ix.id in syncing,
                     "rows": int(st.get("rows") or 0) if ready else 0,
                     "version": st.get("version") if ready else None,
                     "synced_at": st.get("synced_at") if ready else None,
                     "error": st.get("error") or None})
    ready = [r for r in rows if r["state"] == "ready"]
    state = ("ready" if ready else "fetching" if syncing or (not rows and _thread is not None
                                                             and _pass_error is None and signed_in())
             else "error" if _pass_error or any(r["error"] for r in rows) else "absent")
    return {"signed_in": signed_in(), "state": state,
            "rows": sum(r["rows"] for r in ready),
            "fetched_at": max((r["synced_at"] or 0 for r in ready), default=None) or None,
            "error": _pass_error or next((r["error"] for r in rows if r["error"]), None),
            "indexes": rows}
