"""Fill and mutate a local "dev backend" folder for multi-bucket S3 indexes.

`fused_render/remote_buckets.py` reads this folder through `DevGateway` when
`FUSED_RENDER_BUCKET_INDEX_DEV=<folder>` is set, so the sync can be exercised
before the Fused server routes exist. Folder format (DevGateway's contract):

    <dir>/manifest.json      {"indexes": [{id, bucket, prefix, is_public, region,
                                           version, oldest_version, objects}]}
    <dir>/<id>/live.parquet  key VARCHAR, size BIGINT,
                             last_modified TIMESTAMP WITH TIME ZONE,
                             ver BIGINT, deleted BOOLEAN

`live.parquet` stands in for the server's table: one row per key, `ver` = the
version that last touched it, `deleted` = tombstone. Every mutation batch is
one new version. Writes are atomic: `live.parquet.new` then `manifest.json.new`,
each swapped in with os.replace, parquet first and manifest last.

After a `rewind`, the next mutation continues from max(manifest version,
max(ver)) + 1, so a version number is never handed out twice.

Usage (the default --dir is $FUSED_RENDER_BUCKET_INDEX_DEV, else
~/fused-bucket-index-dev):

    python scripts/bucket_index_dev.py add-fused-asset
    python scripts/bucket_index_dev.py add --id big --bucket my-bucket --prefix data/ --synthetic 1000000
    python scripts/bucket_index_dev.py add --id mine --bucket b --from-parquet listing.parquet
    python scripts/bucket_index_dev.py put --id big data/new/file.csv --size 1234
    python scripts/bucket_index_dev.py rm --id big data/a.csv data/b.csv
    python scripts/bucket_index_dev.py churn --id big --n 1000
    python scripts/bucket_index_dev.py purge --id big
    python scripts/bucket_index_dev.py rewind --id big --to 3
    python scripts/bucket_index_dev.py remove --id big
    python scripts/bucket_index_dev.py status
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request

import duckdb

ENV_DEV = "FUSED_RENDER_BUCKET_INDEX_DEV"
FUSED_ASSET_URL = ("https://fused-asset.s3.us-west-2.amazonaws.com/"
                   "fused-render-index/fused-asset.parquet")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
LIVE_COLS = ("key::VARCHAR AS key, size::BIGINT AS size, "
             "last_modified::TIMESTAMPTZ AS last_modified, ver::BIGINT AS ver, "
             "deleted::BOOLEAN AS deleted")


class Fail(Exception):
    pass


def sq(s: str) -> str:
    return s.replace("'", "''")


# --- folder ---------------------------------------------------------------------


def manifest_path(root: str) -> str:
    return os.path.join(root, "manifest.json")


def live_path(root: str, ident: str) -> str:
    return os.path.join(root, ident, "live.parquet")


def load(root: str) -> list[dict]:
    try:
        with open(manifest_path(root), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    rows = data.get("indexes") if isinstance(data, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def save(root: str, rows: list[dict]) -> None:
    os.makedirs(root, exist_ok=True)
    tmp = manifest_path(root) + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"indexes": rows}, f, indent=2)
        f.write("\n")
    os.replace(tmp, manifest_path(root))


def find(rows: list[dict], ident: str) -> dict:
    for r in rows:
        if r.get("id") == ident:
            return r
    raise Fail(f"no index {ident!r} (see `status`)")


def connect(root: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    spill = os.path.join(root, ".duckdb-tmp")
    os.makedirs(spill, exist_ok=True)
    con.execute(f"SET temp_directory = '{sq(spill)}'")
    return con


def write_live(con, root: str, ident: str, select_sql: str) -> None:
    """COPY `select_sql` (already in live columns) to live.parquet atomically."""
    os.makedirs(os.path.join(root, ident), exist_ok=True)
    dest = live_path(root, ident)
    con.execute(f"COPY (SELECT {LIVE_COLS} FROM ({select_sql})) TO '{sq(dest)}.new' "
                "(FORMAT parquet, COMPRESSION zstd)")
    os.replace(dest + ".new", dest)


def counts(con, root: str, ident: str, version: int) -> tuple[int, int, int, int]:
    """(objects visible at `version`, tombstones, all rows, max ver)."""
    live, dead, total, top = con.execute(
        f"SELECT count(*) FILTER (WHERE NOT deleted AND ver <= {int(version)}), "
        "count(*) FILTER (WHERE deleted), count(*), coalesce(max(ver), 0) "
        f"FROM read_parquet('{sq(live_path(root, ident))}')").fetchone()
    return int(live), int(dead), int(total), int(top)


def finish(con, root: str, rows: list[dict], entry: dict) -> tuple[int, int]:
    """Refresh `objects` and write the manifest (always after the parquet)."""
    live, dead, _, _ = counts(con, root, entry["id"], entry["version"])
    entry["objects"] = live
    save(root, rows)
    return live, dead


def check_dedup(con, src: str) -> None:
    n, d = con.execute(f"SELECT count(*), count(DISTINCT key) FROM ({src})").fetchone()
    if n != d:
        raise Fail(f"internal: {n - d} duplicate keys")


def norm_prefix(p: str) -> str:
    p = (p or "").lstrip("/")
    if p and not p.endswith("/"):
        p += "/"
    return p


def check_key(entry: dict, key: str) -> str:
    if not key or key.endswith("/"):
        raise Fail(f"bad key {key!r}: empty or a folder marker")
    prefix = entry.get("prefix") or ""
    if prefix and not key.startswith(prefix):
        raise Fail(f"key {key!r} is outside the index prefix {prefix!r} (keys are full bucket keys)")
    return key


# --- adding -----------------------------------------------------------------------


def register(root: str, ident: str, bucket: str, prefix: str, public: bool,
             region: str | None, build) -> None:
    """Create or replace index `ident` at version 1; `build(con)` returns the
    SELECT that yields key/size/last_modified rows."""
    if not ID_RE.fullmatch(ident):
        raise Fail(f"bad id {ident!r}: must match {ID_RE.pattern}")
    if not bucket or "/" in bucket:
        raise Fail(f"bad bucket {bucket!r}")
    con = connect(root)
    try:
        src = build(con)
        # One row per key (newest wins), no folder markers, no NULLs.
        body = (f"SELECT key, coalesce(size, 0) AS size, "
                f"coalesce(last_modified, TIMESTAMPTZ '1970-01-01 00:00:00+00') AS last_modified, "
                f"1 AS ver, false AS deleted FROM ({src}) "
                "WHERE key IS NOT NULL AND key <> '' AND NOT ends_with(key, '/') "
                "QUALIFY row_number() OVER (PARTITION BY key ORDER BY last_modified DESC NULLS LAST) = 1 "
                "ORDER BY key")
        write_live(con, root, ident, body)
        rows = [r for r in load(root) if r.get("id") != ident]
        entry = {"id": ident, "bucket": bucket, "prefix": prefix, "is_public": public,
                 "region": region, "version": 1, "oldest_version": 1, "objects": 0}
        rows.append(entry)
        live, _ = finish(con, root, rows, entry)
    finally:
        con.close()
    print(f"added {ident} (s3://{bucket}/{prefix}, {'public' if public else 'private'}): "
          f"{live:,} objects, version 1")


def cmd_add_fused_asset(args) -> None:
    tmpdir = tempfile.mkdtemp(prefix="bucket-index-dl-")
    try:
        path = os.path.join(tmpdir, "fused-asset.parquet")
        req = urllib.request.Request(FUSED_ASSET_URL, headers={"User-Agent": "fused-render"})
        with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)

        def build(con):
            return (f"SELECT key::VARCHAR AS key, size::BIGINT AS size, "
                    f"last_modified::TIMESTAMPTZ AS last_modified FROM read_parquet('{sq(path)}')")

        register(args.dir, "fused-asset", "fused-asset", "", True, "us-west-2", build)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


EXTS = ["parquet", "parquet", "parquet", "tif", "tif", "json", "csv", "csv",
        "geojson", "png", "zarr", "txt", "gz", "nc"]
STEMS = ["part", "tile", "scene", "export", "chunk", "data", "results", "frame",
         "summary", "batch", "features", "raster", "events", "metrics", "sample"]
TOPS = ["raw", "processed", "tiles", "exports", "models", "landsat", "sentinel2",
        "census", "weather", "users", "logs", "archive", "staging", "ml", "imagery",
        "vector", "reports", "cache", "public", "scratch", "derived", "notebooks",
        "uploads", "backups", "benchmarks", "overture", "buildings", "roads",
        "soil", "crops"]
SECS = ["batch", "tile", "user", "run", "scene", "region", "job", "shard"]


def synthetic_sql(n: int, prefix: str) -> str:
    """N unique, plausible keys under `prefix`, generated entirely in SQL."""
    n_top = max(3, min(50, n // 20000))
    n_sec = max(1, n // (n_top * 400))
    lst = lambda xs: "[" + ", ".join(f"'{x}'" for x in xs) + "]"  # noqa: E731
    return f"""
    WITH b AS (
      SELECT i,
        (hash(i, 'top') % {n_top})::BIGINT AS t,
        (hash(i, 'sec') % {n_sec})::BIGINT AS s,
        (hash(i, 'depth') % 4)::BIGINT AS d,
        hash(i, 'flat') % 200 = 0 AS flat,
        (hash(i, 'u') % 1000000) / 1000000.0 AS u,
        (hash(i, 'when') % 1000000) / 1000000.0 AS w,
        {lst(EXTS)}[1 + (hash(i, 'ext') % {len(EXTS)})::BIGINT] AS ext,
        {lst(STEMS)}[1 + (hash(i, 'stem') % {len(STEMS)})::BIGINT] AS stem
      FROM range({int(n)}) r(i)
    ), c AS (
      SELECT *,
        TIMESTAMPTZ '2019-01-01 00:00:00+00' + to_microseconds((w * 7 * 365 * 86400e6)::BIGINT) AS lm,
        {lst(TOPS)}[1 + t % {len(TOPS)}]
          || CASE WHEN t >= {len(TOPS)} THEN '-' || (t // {len(TOPS)})::VARCHAR ELSE '' END AS top
      FROM b
    ), k AS (
      SELECT *,
        top || CASE WHEN flat THEN '' ELSE
          '/' || {lst(SECS)}[1 + t % {len(SECS)}] || '_' || lpad(s::VARCHAR, 4, '0')
          || CASE d WHEN 0 THEN ''
                    WHEN 1 THEN strftime(lm, '/%Y')
                    WHEN 2 THEN strftime(lm, '/year=%Y/month=%m')
                    ELSE strftime(lm, '/%Y/%m/%d') END
        END AS folder
      FROM c
    )
    SELECT
      '{sq(prefix)}' || folder || '/' || stem || '_' || lpad(i::VARCHAR, 8, '0')
        || CASE WHEN ext = 'zarr' THEN '.zarr/' || (i % 7)::VARCHAR || '.' || (i % 5)::VARCHAR
                ELSE '.' || ext END AS key,
      (CASE ext WHEN 'json' THEN exp(4 + u * 8) WHEN 'csv' THEN exp(6 + u * 12)
                WHEN 'txt' THEN exp(3 + u * 8) WHEN 'tif' THEN exp(14 + u * 6)
                WHEN 'zarr' THEN exp(10 + u * 6) ELSE exp(8 + u * 12) END)::BIGINT AS size,
      lm AS last_modified
    FROM k"""


def cmd_add(args) -> None:
    prefix = norm_prefix(args.prefix)
    if args.from_parquet:
        path = os.path.abspath(os.path.expanduser(args.from_parquet))
        if not os.path.isfile(path):
            raise Fail(f"no such file: {path}")

        def build(con):
            src = (f"SELECT key::VARCHAR AS key, size::BIGINT AS size, "
                   f"last_modified::TIMESTAMPTZ AS last_modified FROM read_parquet('{sq(path)}')")
            if prefix:
                total, keep = con.execute(
                    f"SELECT count(*), count(*) FILTER (WHERE starts_with(key, '{sq(prefix)}')) "
                    f"FROM ({src})").fetchone()
                if total != keep:
                    print(f"note: dropped {total - keep:,} of {total:,} rows outside prefix {prefix!r}")
                if keep == 0:
                    raise Fail(f"no key in {path} starts with {prefix!r} (keys are full bucket keys)")
                src = f"SELECT * FROM ({src}) WHERE starts_with(key, '{sq(prefix)}')"
            return src
    else:
        if args.synthetic < 1:
            raise Fail("--synthetic needs N >= 1")

        def build(con):
            return synthetic_sql(args.synthetic, prefix)

    register(args.dir, args.id, args.bucket, prefix, args.public, args.region or None, build)


# --- mutations ----------------------------------------------------------------------


def next_version(con, root: str, entry: dict) -> int:
    """A version never handed out before: past any rows a rewind left ahead."""
    _, _, _, top = counts(con, root, entry["id"], entry["version"])
    return max(int(entry["version"]), top) + 1


def apply_changes(con, root: str, rows: list[dict], entry: dict, new: int) -> tuple[int, int]:
    """Rewrite live.parquet as `live ANTI JOIN chg` + chg at version `new`.
    `chg` is a temp table (key, size, last_modified, deleted), one row per key."""
    live = f"read_parquet('{sq(live_path(root, entry['id']))}')"
    body = (f"SELECT key, size, last_modified, ver, deleted FROM {live} l ANTI JOIN chg USING (key) "
            f"UNION ALL SELECT key, size, last_modified, {int(new)} AS ver, deleted FROM chg")
    write_live(con, root, entry["id"], body)
    old = entry["version"]
    entry["version"] = new
    out = finish(con, root, rows, entry)
    if new != old + 1:
        print(f"note: version jumped {old} -> {new} (rows from before a rewind reach up to v{new - 1})")
    return out


def open_entry(args):
    rows = load(args.dir)
    entry = find(rows, args.id)
    if not os.path.isfile(live_path(args.dir, entry["id"])):
        raise Fail(f"{args.id} has no live.parquet")
    return rows, entry


def cmd_put(args) -> None:
    rows, entry = open_entry(args)
    key = check_key(entry, args.key)
    con = connect(args.dir)
    try:
        live = f"read_parquet('{sq(live_path(args.dir, entry['id']))}')"
        old = con.execute(f"SELECT size, deleted FROM {live} WHERE key = ?", [key]).fetchone()
        size = args.size if args.size is not None else (old[0] if old else 1024)
        con.execute("CREATE TEMP TABLE chg AS SELECT ?::VARCHAR AS key, ?::BIGINT AS size, "
                    "now()::TIMESTAMPTZ AS last_modified, false AS deleted", [key, int(size)])
        new = next_version(con, args.dir, entry)
        live_n, _ = apply_changes(con, args.dir, rows, entry, new)
    finally:
        con.close()
    what = "updated" if old and not old[1] else ("revived" if old else "added")
    print(f"put {args.id}: {what} {key} ({size:,} B); {live_n:,} objects, version {new}")


def cmd_rm(args) -> None:
    rows, entry = open_entry(args)
    keys = [check_key(entry, k) for k in args.keys]
    con = connect(args.dir)
    try:
        live = f"read_parquet('{sq(live_path(args.dir, entry['id']))}')"
        con.execute("CREATE TEMP TABLE ks (key VARCHAR)")
        con.executemany("INSERT INTO ks VALUES (?)", [[k] for k in dict.fromkeys(keys)])
        con.execute(f"CREATE TEMP TABLE chg AS SELECT l.key, l.size, l.last_modified, true AS deleted "
                    f"FROM {live} l SEMI JOIN ks USING (key) WHERE NOT l.deleted")
        hit = {r[0] for r in con.execute("SELECT key FROM chg").fetchall()}
        for k in dict.fromkeys(keys):
            if k not in hit:
                print(f"note: {k} is not a live key; ignored")
        if not hit:
            print(f"rm {args.id}: nothing removed; version stays {entry['version']}")
            return
        new = next_version(con, args.dir, entry)
        live_n, dead = apply_changes(con, args.dir, rows, entry, new)
    finally:
        con.close()
    print(f"rm {args.id}: tombstoned {len(hit)} key(s); {live_n:,} objects, {dead:,} tombstones, version {new}")


def cmd_churn(args) -> None:
    rows, entry = open_entry(args)
    if args.n < 1:
        raise Fail("--n must be >= 1")
    n_upd, n_del = args.n // 2, args.n // 4
    n_new = args.n - n_upd - n_del
    prefix = entry.get("prefix") or ""
    con = connect(args.dir)
    try:
        new = next_version(con, args.dir, entry)
        live = f"read_parquet('{sq(live_path(args.dir, entry['id']))}')"
        visible = f"SELECT * FROM {live} WHERE NOT deleted AND ver <= {int(entry['version'])}"
        # One sample, split into disjoint update and delete sets.
        con.execute(f"CREATE TEMP TABLE pick AS SELECT key, size, last_modified, "
                    f"row_number() OVER () AS rn FROM ({visible}) "
                    f"USING SAMPLE reservoir({n_upd + n_del} ROWS)")
        # Folders that already hold files, for most of the new keys.
        con.execute(f"CREATE TEMP TABLE dirs AS SELECT regexp_replace(key, '[^/]*$', '') AS d, "
                    f"row_number() OVER () AS rn FROM ({visible}) "
                    f"USING SAMPLE reservoir({max(n_new, 1)} ROWS)")
        n_dirs = con.execute("SELECT count(*) FROM dirs").fetchone()[0]
        # A couple of keys in a brand-new folder (all of them when there is
        # no existing folder to put them in).
        n_fresh = n_new if n_dirs == 0 else (2 if n_new > 2 else 0)
        exts = "['parquet', 'csv', 'json', 'tif', 'geojson']"
        fresh_dir = f"{prefix}churn-new-v{new}/"
        con.execute(f"""
            CREATE TEMP TABLE chg AS
            SELECT key, (size * (0.5 + random()))::BIGINT + 1 AS size,
                   now()::TIMESTAMPTZ AS last_modified, false AS deleted
              FROM pick WHERE rn <= {n_upd}
            UNION ALL
            SELECT key, size, last_modified, true AS deleted FROM pick WHERE rn > {n_upd}
            UNION ALL
            SELECT CASE WHEN i < {n_fresh} THEN '{sq(fresh_dir)}'
                        ELSE coalesce((SELECT d FROM dirs WHERE dirs.rn = 1 + (i % {max(n_dirs, 1)})),
                                      '{sq(prefix)}') END
                   || 'churn_v{new}_' || lpad(i::VARCHAR, 6, '0') || '.'
                   || {exts}[1 + (i % 5)] AS key,
                   (exp(6 + random() * 10))::BIGINT AS size,
                   now()::TIMESTAMPTZ AS last_modified, false AS deleted
              FROM range({n_new}) r(i)""")
        check_dedup(con, "SELECT key FROM chg")
        got_upd, got_del, got_new = con.execute(
            f"SELECT count(*) FILTER (WHERE rn <= {n_upd}), count(*) FILTER (WHERE rn > {n_upd}), "
            f"{n_new} FROM pick").fetchone()
        live_n, dead = apply_changes(con, args.dir, rows, entry, new)
    finally:
        con.close()
    print(f"churn {args.id}: {got_upd:,} updated, {got_del:,} deleted, {got_new:,} new "
          f"({n_fresh} in new folder {fresh_dir}); {live_n:,} objects, {dead:,} tombstones, version {new}")


def cmd_purge(args) -> None:
    rows, entry = open_entry(args)
    con = connect(args.dir)
    try:
        _, before, _, _ = counts(con, args.dir, entry["id"], entry["version"])
        live = f"read_parquet('{sq(live_path(args.dir, entry['id']))}')"
        write_live(con, args.dir, entry["id"], f"SELECT * FROM {live} WHERE NOT deleted")
        entry["oldest_version"] = entry["version"]
        live_n, _ = finish(con, args.dir, rows, entry)
    finally:
        con.close()
    print(f"purge {args.id}: dropped {before:,} tombstones; oldest_version = version = "
          f"{entry['version']} ({live_n:,} objects)")


def cmd_rewind(args) -> None:
    rows, entry = open_entry(args)
    cur = int(entry["version"])
    if args.to < 1:
        raise Fail("--to must be >= 1")
    if args.to >= cur:
        raise Fail(f"--to must be below the current version {cur}")
    oldest = int(entry.get("oldest_version") or 1)
    if args.to < oldest:
        # Tombstones up to oldest_version were purged, so a server "at" a
        # version below it could not answer changes from there honestly.
        raise Fail(f"--to must be >= oldest_version {oldest} (purged history cannot come back)")
    entry["version"] = args.to
    con = connect(args.dir)
    try:
        live_n, _ = finish(con, args.dir, rows, entry)
    finally:
        con.close()
    print(f"rewind {args.id}: version {cur} -> {args.to} (rows untouched; "
          f"{live_n:,} objects visible at v{args.to}); a client at v{cur} is now ahead")


def cmd_remove(args) -> None:
    rows = load(args.dir)
    find(rows, args.id)
    save(args.dir, [r for r in rows if r.get("id") != args.id])  # manifest first
    shutil.rmtree(os.path.join(args.dir, args.id), ignore_errors=True)
    print(f"remove {args.id}: gone from the manifest and its folder deleted")


def cmd_status(args) -> None:
    rows = load(args.dir)
    if not rows:
        print(f"no indexes in {args.dir}")
        return
    con = connect(args.dir)
    out = [("id", "bucket/prefix", "public", "version", "oldest", "live rows", "tombstones")]
    try:
        for e in rows:
            ident = str(e.get("id"))
            if os.path.isfile(live_path(args.dir, ident)):
                live, dead, _, _ = counts(con, args.dir, ident, int(e.get("version") or 0))
                lv, dd = f"{live:,}", f"{dead:,}"
            else:
                lv = dd = "-"
            out.append((ident, f"{e.get('bucket')}/{e.get('prefix') or ''}",
                        "yes" if e.get("is_public") else "no", str(e.get("version")),
                        str(e.get("oldest_version")), lv, dd))
    finally:
        con.close()
    w = [max(len(r[i]) for r in out) for i in range(len(out[0]))]
    for j, r in enumerate(out):
        print("  ".join(c.rjust(w[i]) if i >= 3 else c.ljust(w[i]) for i, c in enumerate(r)).rstrip())
        if j == 0:
            print("  ".join("-" * x for x in w))
    print(f"{len(rows)} index(es) in {args.dir}")


# --- cli --------------------------------------------------------------------------


def main(argv=None) -> int:
    default_dir = (os.environ.get(ENV_DEV) or "").strip() or "~/fused-bucket-index-dev"
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dir", default=default_dir,
                   help=f"dev backend folder (default: ${ENV_DEV} or ~/fused-bucket-index-dev)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add-fused-asset", help="add the public fused-asset index from its real listing")
    s.set_defaults(fn=cmd_add_fused_asset)

    s = sub.add_parser("add", help="add (or replace) an index at version 1")
    s.add_argument("--id", required=True)
    s.add_argument("--bucket", required=True)
    s.add_argument("--prefix", default="")
    s.add_argument("--public", action="store_true")
    s.add_argument("--region", default="us-west-2")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--from-parquet", metavar="PATH")
    g.add_argument("--synthetic", type=int, metavar="N")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("put", help="upsert one key (version +1)")
    s.add_argument("--id", required=True)
    s.add_argument("key")
    s.add_argument("--size", type=int)
    s.set_defaults(fn=cmd_put)

    s = sub.add_parser("rm", help="tombstone keys (version +1)")
    s.add_argument("--id", required=True)
    s.add_argument("keys", nargs="+")
    s.set_defaults(fn=cmd_rm)

    s = sub.add_parser("churn", help="one random batch of updates/deletes/adds (version +1)")
    s.add_argument("--id", required=True)
    s.add_argument("--n", type=int, default=1000)
    s.set_defaults(fn=cmd_churn)

    s = sub.add_parser("purge", help="drop tombstones; oldest_version = version")
    s.add_argument("--id", required=True)
    s.set_defaults(fn=cmd_purge)

    s = sub.add_parser("rewind", help="set the manifest version back (client-ahead case)")
    s.add_argument("--id", required=True)
    s.add_argument("--to", type=int, required=True)
    s.set_defaults(fn=cmd_rewind)

    s = sub.add_parser("remove", help="delete the index (lost access)")
    s.add_argument("--id", required=True)
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("status", help="list the indexes")
    s.set_defaults(fn=cmd_status)

    args = p.parse_args(argv)
    args.dir = os.path.abspath(os.path.expanduser(args.dir))
    try:
        args.fn(args)
    except Fail as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
