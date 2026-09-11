"""The on-device Hub catalog: one parquet pool per capability, queried with a
fresh DuckDB connection per call. SPEC docs/HUB_CATALOG_SPEC.md, D1236+.

Ported patterns, not code, from `fused_render/index/store.py`: `store_lock`
(an OS file lock held by an open fd, not a lockfile-existence protocol, so a
crashed writer releases it with its process), generation-numbered files with
a manifest written LAST so a reader mid-swap still sees the previous complete
generation, and a fresh DuckDB connection per query (see that module's own
docstring for why no connection is long-lived). The index's own row SCHEMA
is deliberately not reused — a Hub row and a filesystem row share nothing.

**Row shape.** Each pool row carries a handful of columns DuckDB can filter/
sort/facet on directly (id, capability, format, downloads, likes,
lastModified, createdAt, libraryName, gated, private) plus the FULL raw Hub
row as a `raw` JSON string column. Query-time code (`_model_row` in
`hub_models.py`) already knows how to turn one of those raw dicts into a
response row — that function's own drop rules and D780 scoring stay
UNCHANGED and keep running at query time, over `json.loads(raw)`, rather than
this module trying to keep a second, parquet-native copy of every field
`_EXPAND` might ever carry in sync with that function. A schema change in the
Hub's response (a new `_EXPAND` field) therefore costs nothing here: it shows
up in `raw` on the next rebuild and `_model_row` picks it up like always.
"""
from __future__ import annotations

import contextlib
import json
import os
import time

from fused_render.ai.hub_catalog_config import HubCatalogConfig, load_config

#: Manifest schema version. Bump alongside any incompatible manifest shape
#: change; `read_manifest` treats an unrecognised version as "no catalog".
VERSION = 1


@contextlib.contextmanager
def store_lock(cfg: HubCatalogConfig):
    """Mutual exclusion for catalog WRITERS (pool builds, metadata compaction).

    Ported verbatim in spirit from `index/store.py:store_lock` — see that
    docstring for the full reasoning (OS lock over a lockfile-existence
    protocol; readers never take it). Two dev servers sharing a home dir
    (D-note: multiple servers sharing FUSED_RENDER_HOME is an explicit
    constraint in the spec) must not both build the same capability's pool
    at once and race the manifest write."""
    os.makedirs(cfg.dir, exist_ok=True)
    f = open(os.path.join(cfg.dir, ".catalog.lock"), "a")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def read_manifest(cfg: HubCatalogConfig) -> dict:
    """The manifest, or `{"version": VERSION, "capabilities": {}}` if none
    has ever been written — never raises, mirroring `index/store.py:
    read_manifest`'s "no scan yet" contract."""
    try:
        with open(cfg.manifest_json) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("capabilities"), dict):
        return {"version": VERSION, "capabilities": {}}
    return data


def _write_manifest(cfg: HubCatalogConfig, manifest: dict) -> None:
    os.makedirs(cfg.dir, exist_ok=True)
    tmp = cfg.manifest_json + ".new"
    with open(tmp, "w") as f:
        json.dump(manifest, f)
    os.replace(tmp, cfg.manifest_json)


def pool_entry(cfg: HubCatalogConfig, capability: str) -> dict | None:
    """This capability's manifest entry, or None if no pool has been built."""
    return read_manifest(cfg)["capabilities"].get(capability)


def pool_exists(cfg: HubCatalogConfig, capability: str) -> bool:
    """Whether a BUILT pool exists for `capability` — the branch the search
    route uses to pick the catalog path over the live-Hub fallback. An entry
    whose file is missing on disk (a hand-cleared cache dir, a half-finished
    first build that crashed before writing the file but after some other
    write raced the manifest — should not happen under `store_lock`, but this
    is the cheap defence) reads as "no pool", so the route falls back safely
    rather than opening a file that is not there."""
    entry = pool_entry(cfg, capability)
    if not entry or not entry.get("file"):
        # A block set before any pool ever built (`set_blocked_until` writes
        # an entry with no "file" key) is not a pool: the search route must
        # still fall back to the live path, it just should not retry the
        # build until the block clears.
        return False
    return os.path.exists(os.path.join(cfg.pools_dir, entry["file"]))


def is_blocked(cfg: HubCatalogConfig, capability: str) -> bool:
    """Whether this capability's builder is sitting out a 429 backoff window
    the Hub told it to. Read fresh every time (no in-memory cache) so a
    restarted server or a second process sharing the home dir both honour
    the SAME blocked-until timestamp."""
    entry = pool_entry(cfg, capability)
    if not entry:
        return False
    blocked_until = entry.get("blockedUntil")
    return isinstance(blocked_until, (int, float)) and time.time() < blocked_until


def set_blocked_until(cfg: HubCatalogConfig, capability: str, until: float) -> None:
    """Persist a 429 backoff deadline for `capability`, keeping any existing
    pool entry (file/generation/rows) untouched — a rate limit mid-build
    means "stop asking for now", not "forget what was already fetched"."""
    with store_lock(cfg):
        manifest = read_manifest(cfg)
        entry = dict(manifest["capabilities"].get(capability) or {})
        entry["blockedUntil"] = until
        manifest["capabilities"][capability] = entry
        _write_manifest(cfg, manifest)


def _row_columns(rows: list[dict]) -> dict:
    """Pool rows -> the columnar dict `pyarrow.table` wants. Missing/wrong-typed
    fields degrade to a safe default rather than raising: a malformed Hub row
    should cost that one row a blank facet value, never abort the whole
    build (the same tolerance `_model_row` itself already applies)."""
    def s(v):
        return v if isinstance(v, str) else None

    def n(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    def gated_str(v):
        """The Hub's real `gated` values are `False`, `True`, `"auto"`, or
        `"manual"` — a mix of `bool` and `str` in the same field across
        different repos. `pa.table`'s type inference cannot pick a single
        Arrow type for a Python list mixing both, and raises for any pool
        containing at least one of each (every non-trivial capability).
        Normalise to ONE explicit string type instead: `False`/missing ->
        `""` (not gated), `True` -> `"manual"` (the Hub's own bool shorthand
        for "gated, no auto-approval flow"), any other string passed through
        as-is (D1241)."""
        if isinstance(v, str):
            return v
        return "manual" if v is True else ""

    cols = {"id": [], "capability": [], "format": [], "downloads": [],
            "likes": [], "lastModified": [], "createdAt": [], "libraryName": [],
            "gated": [], "private": [], "raw": []}
    for row in rows:
        raw = row.get("raw") or {}
        cols["id"].append(s(raw.get("id")) or "")
        cols["capability"].append(s(row.get("capability")) or "")
        cols["format"].append(s(row.get("format")) or "")
        cols["downloads"].append(int(n(raw.get("downloads"))))
        cols["likes"].append(int(n(raw.get("likes"))))
        cols["lastModified"].append(s(raw.get("lastModified")) or "")
        cols["createdAt"].append(s(raw.get("createdAt")) or "")
        cols["libraryName"].append(s(raw.get("library_name")) or "")
        cols["gated"].append(gated_str(raw.get("gated")))
        cols["private"].append(bool(raw.get("private")))
        cols["raw"].append(json.dumps(raw))
    return cols


def write_pool(cfg: HubCatalogConfig, capability: str, rows: list[dict], *,
               build_seconds: float | None = None, pages: int | None = None,
               started_at: float | None = None,
               formats: tuple[str, ...] | None = None) -> dict:
    """Write a fresh generation of `capability`'s pool and swap the manifest
    in — the whole operation under `store_lock`, so two writers (two dev
    servers on the same home dir, or a retriggered build racing the daily
    delta) serialize rather than corrupt each other's file or manifest entry.

    `rows`: `[{"capability": ..., "format": ..., "raw": <raw Hub row dict>}]`.
    `build_seconds`/`pages`/`started_at` are OPTIONAL instrumentation the
    caller (`hub_catalog_builder.py`) supplies to record how long a build
    took, how many Hub list-endpoint pages it fetched, and when it started —
    manifest schema stays version 1 (they're written only when the caller
    passes them, so `pool_entry`/`pool_exists`/`query_pool` and any older
    manifest entry that predates this instrumentation all keep working with
    them simply absent).
    Returns the manifest entry written. The PREVIOUS generation's file is
    deleted only after the new one is durably swapped in via the manifest —
    the atomic-swap-then-reclaim shape `index/store.py:compact` uses, scaled
    down to "one file in, one file out" since a pool is a single parquet
    rather than a set of size-bounded partitions."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    with store_lock(cfg):
        manifest = read_manifest(cfg)
        prev = manifest["capabilities"].get(capability) or {}
        generation = int(prev.get("generation") or 0) + 1
        os.makedirs(cfg.pools_dir, exist_ok=True)
        filename = f"pool-{capability}-{generation:06d}.parquet"
        path = os.path.join(cfg.pools_dir, filename)
        # An explicit schema, not `pa.table`'s own type inference: every
        # column here is already normalised to one Python type per value by
        # `_row_columns` (see `gated_str`'s docstring for why `gated`
        # specifically needed that), but declaring the schema up front means
        # a future column that mixes types the same way fails loudly at the
        # point it is added to `_row_columns`, not with an opaque pyarrow
        # error the first time a real pool happens to contain both variants.
        schema = pa.schema([
            ("id", pa.string()),
            ("capability", pa.string()),
            ("format", pa.string()),
            ("downloads", pa.int64()),
            ("likes", pa.int64()),
            ("lastModified", pa.string()),
            ("createdAt", pa.string()),
            ("libraryName", pa.string()),
            ("gated", pa.string()),
            ("private", pa.bool_()),
            ("raw", pa.string()),
        ])
        table = pa.table(_row_columns(rows), schema=schema)
        pq.write_table(table, path)

        entry = {"file": filename, "generation": generation, "rows": len(rows),
                  "updated": time.time(), "blockedUntil": None}
        if build_seconds is not None:
            entry["buildSeconds"] = build_seconds
        if pages is not None:
            entry["pages"] = pages
        if started_at is not None:
            entry["startedAt"] = started_at
        if formats is not None:
            # C3 (bugbot): the exact set of Hub `filter=` format tags this
            # build paged, so a later `ensure_build_started` can tell a pool
            # built before a second runner/format became available on this
            # machine from one that already covers it, and trigger a rebuild
            # rather than serving a permanently narrower pool forever.
            entry["formats"] = list(formats)
        manifest["capabilities"][capability] = entry
        _write_manifest(cfg, manifest)

        prev_file = prev.get("file")
        if prev_file and prev_file != filename:
            try:
                os.unlink(os.path.join(cfg.pools_dir, prev_file))
            except OSError:
                pass
        return entry


def query_pool(cfg: HubCatalogConfig, capability: str, *,
               where: str | None = None) -> list[dict]:
    """Every raw Hub row dict in `capability`'s pool, optionally narrowed by
    a SQL `WHERE` clause fragment (columns: id, capability, format,
    downloads, likes, lastModified, createdAt, libraryName, gated, private —
    NOT `raw`, which stays an opaque JSON string at this layer). Returns `[]`
    when no pool has been built.

    A FRESH DuckDB connection every call, never a cached/shared one — see
    `index/store.py:background_connect`'s docstring for why a long-lived
    connection over a store that gets rewritten (generation swap) is the
    wrong shape here too, and because this module has no query-throttling
    story of its own yet (the whole pool is small enough per capability that
    one has not been needed) it deliberately does not import
    `index.store.search_threads`/`compaction_threads` — a future caller that
    needs a cap should ask this function to grow one rather than import
    the index engine's."""
    import duckdb

    entry = pool_entry(cfg, capability)
    if not entry or not entry.get("file"):
        # Mirrors `pool_exists`'s own check: a manifest entry can exist with
        # no "file" key yet (`set_blocked_until` writes one before any pool
        # has ever been built), and `entry["file"]` would raise `KeyError`
        # for that case instead of the "no pool" `[]` every other no-pool
        # path returns.
        return []
    path = os.path.join(cfg.pools_dir, entry["file"])
    if not os.path.exists(path):
        return []
    con = duckdb.connect()
    try:
        sql = f"SELECT raw FROM read_parquet('{path}')"
        if where:
            sql += f" WHERE {where}"
        result = con.execute(sql).fetchall()
    finally:
        con.close()
    rows = []
    for (raw_json,) in result:
        try:
            parsed = json.loads(raw_json)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def delete_catalog(cfg: HubCatalogConfig) -> None:
    """Remove the whole catalog — pools, manifest, lock-adjacent state.
    Missing files are not an error (mirrors `index.store.delete_store`)."""
    import shutil

    with store_lock(cfg):
        shutil.rmtree(cfg.pools_dir, ignore_errors=True)
        try:
            os.unlink(cfg.manifest_json)
        except OSError:
            pass
