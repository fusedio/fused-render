"""Reading the index — strictly read-only, and strictly parameterized.

Ported from OpenIndex's `query.py` MINUS its `sql` action. Inside a local
trusted page, handing duckdb a user's statement was consistent with runPython
executing arbitrary local Python anyway; behind an HTTP route it is an
arbitrary read/write surface with no allowlist and no read-only flag, so it is
gone rather than guarded. What remains — `stats` and `lookup` — builds SQL only
from escaped literals, an int-cast limit/offset and a fixed sort allowlist.

duckdb is imported inside each function, not at module top: this module is
imported by the server's router, and a call against a missing index should not
pay a duckdb import.

See specs/query.md.
"""
import os
import re

from fused_render.index.config import IndexConfig
from fused_render.index.ignore import norm
from fused_render.index.store import read_manifest

_DRIVE = re.compile(r"^[A-Za-z]:/")

SORTS = {
    "path": "path ASC", "size": "size DESC", "mtime": "mtime DESC",
    "name": "name ASC",
}

# Rows a single lookup may return, whatever the caller asks for.
MAX_LIMIT = 5_000


def _q(s: str) -> str:
    """A SQL string literal's contents (single quotes doubled)."""
    return s.replace("'", "''")


def prune(parts, prefix):
    """Partitions whose [min,max] path range can contain paths starting with
    `prefix` (paths are globally sorted, so the range test is exact, not a
    heuristic)."""
    if not prefix:
        return list(parts)
    hi = prefix + "￿"
    return [p for p in parts
            if p.get("min") is not None and p["max"] >= prefix and p["min"] <= hi]


def pattern_for(q: str):
    """Turn a user query into (like_pattern, prune_prefix).

    `*` is a wildcard; everything else is a literal substring match anywhere
    in the path. A query starting with `/`, a drive letter or `~` is anchored
    at the start, and its literal lead-in (up to the first `*`) prunes
    partitions."""
    q = norm(q)
    anchored = q.startswith("/") or q.startswith("~") or bool(_DRIVE.match(q))
    if q.startswith("~"):
        q = norm(os.path.expanduser(q))
    lit = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pat = lit.replace("*", "%")
    if not anchored:
        pat = "%" + pat
    if not pat.endswith("%"):
        pat += "%"
    prune_prefix = q.split("*", 1)[0] if anchored else ""
    return pat, prune_prefix


def _has_partitions(cfg: IndexConfig) -> bool:
    return os.path.isdir(cfg.files_dir) and any(
        f.endswith(".parquet") for f in os.listdir(cfg.files_dir))


def _sources(cfg: IndexConfig, parts) -> str:
    files = [_q(os.path.join(cfg.files_dir, p["file"])) for p in parts]
    return "read_parquet([" + ",".join(f"'{f}'" for f in files) + "])"


def lookup(cfg: IndexConfig, query: str = "", limit: int = 100, offset: int = 0,
           sort: str = "mtime") -> dict:
    """Files whose path matches `query`, with partition-pruning telemetry.
    An index that was never built answers `{empty: True}` rather than raising —
    "no index yet" is a state the UI renders, not an error."""
    m = read_manifest(cfg)
    if m is None:
        return {"empty": True, "location": cfg.dir, "rows": [], "total": 0,
                "partitions": [], "scanned_partitions": 0, "of_partitions": 0}
    import duckdb

    q = (query or "").strip().rstrip("/")
    pat, prune_prefix = pattern_for(q) if q else ("%", "")
    hit = prune(m["partitions"], prune_prefix)
    base = {"empty": False, "location": cfg.dir,
            "partitions": [p["file"] for p in hit],
            "scanned_partitions": len(hit),
            "of_partitions": len(m["partitions"])}
    if not hit:
        return {**base, "rows": [], "total": 0}
    where = (f"WHERE path ILIKE '{_q(pat)}' ESCAPE '\\'" if q else "WHERE 1=1")
    order = SORTS.get(sort, SORTS["mtime"])
    limit = max(0, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    src = _sources(cfg, hit)
    con = duckdb.connect()
    total = con.execute(f"SELECT count(*) FROM {src} {where}").fetchone()[0]
    rows = con.execute(
        f"SELECT path, dir, name, ext, size, mtime FROM {src} {where} "
        f"ORDER BY {order} LIMIT {limit} OFFSET {offset}").fetchall()
    cols = ["path", "dir", "name", "ext", "size", "mtime"]
    return {**base, "rows": [dict(zip(cols, r)) for r in rows],
            "total": int(total)}


def stats(cfg: IndexConfig, root: str = "") -> dict:
    """Totals + per-extension breakdown for ONE subtree — the explicit `root`,
    else the manifest's `last_root`. An index may hold several roots, so a
    whole-index total would be a number nobody asked for.

    Known cost (inherited): this reads every partition to group by extension;
    there is no cached rollup."""
    m = read_manifest(cfg)
    if m is None:
        return {"empty": True, "location": cfg.dir, "rows": 0, "dirs": 0,
                "total_size": 0, "types": [], "partitions": []}
    import duckdb

    con = duckdb.connect()
    root = norm(os.path.expanduser(root.strip())) if root.strip() else ""
    root = (root or m.get("last_root") or "").rstrip("/") or "/"
    pfx = (_q(root) + "/") if root != "/" else "/"
    inside = f"(dir = '{_q(root)}' OR dir LIKE '{pfx}%')"
    n_rows, total_size, n_dirs = 0, 0, 0
    types = []
    if os.path.exists(cfg.dirs_parquet):
        n_dirs = con.execute(
            f"SELECT count(*) FROM read_parquet('{_q(cfg.dirs_parquet)}') "
            f"WHERE {inside}").fetchone()[0]
    if _has_partitions(cfg):
        by_ext = con.execute(
            f"SELECT coalesce(nullif(ext, ''), 'no ext') e, count(*) n, "
            f"coalesce(sum(size), 0) s "
            f"FROM read_parquet('{_q(cfg.files_dir)}/*.parquet') "
            f"WHERE {inside} "
            f"GROUP BY 1 ORDER BY s DESC").fetchall()
        n_rows = sum(r[1] for r in by_ext)
        total_size = sum(r[2] for r in by_ext)
        top, rest = by_ext[:50], by_ext[50:]
        types = [{"ext": e, "n": int(n), "size": int(sz)} for e, n, sz in top]
        if rest:
            types.append({"ext": "other", "n": int(sum(r[1] for r in rest)),
                          "size": int(sum(r[2] for r in rest))})
    return {"empty": False, "location": cfg.dir, "rows": int(n_rows),
            "dirs": int(n_dirs), "total_size": int(total_size),
            "updated": m.get("updated"), "last_root": root,
            "partitions": m["partitions"], "types": types}
