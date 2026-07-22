"""Prototype: coalesced/parallel column-chunk fetch vs DuckDB ranged HTTP.

Finding so far (via query profiling): DuckDB DOES prune row groups on a
file_row_number IN-list (it rewrites it to a >=/<= range filter) and reads
exactly one column chunk (~17.4MB). The cost is throughput: a single stream
through the rclone serve to S3 runs at only ~1.5-2.5MB/s. So the lever is
PARALLELISM over the chunk's byte range, not just coalescing.

Method (fresh DuckDB connection per phase; each variant targets a DIFFERENT
row group so the serve's VFS cache can't cross-warm them):
  A. baseline  - reader.py phase-two style over HTTP: WHERE file_row_number
                 IN (<100 positions in row group RG_A>) projecting BIG_COL
  B. coalesced-1 - one contiguous range GET of BIG_COL's chunk in RG_B ->
                 sparse local file -> same IN-list query locally
  C. coalesced-8 - same as B for RG_C but the chunk is fetched with 8
                 concurrent range GETs
  verify       - re-fetch variant C's rows over HTTP and compare
"""

import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import duckdb

URL = "http://127.0.0.1:53529/year=2024/quarter=3/2024-07-01_performance_fixed_tiles.parquet"
BIG_COL = "tile"
RG_A = 2  # row group for the HTTP baseline
RG_B = 5  # row group for single-stream coalesced
RG_C = 8  # row group for 8-stream coalesced
N_STREAMS = 8
N_ROWS = 100
SPARSE = "/tmp/coalesced_proto_sparse.parquet"


def http_con():
    con = duckdb.connect(":memory:")
    con.execute("LOAD httpfs")
    con.execute("SET enable_object_cache=true")
    con.execute("SET threads=32")
    return con


def range_get(url, start, length):
    req = urllib.request.Request(url)
    req.add_header("Range", f"bytes={start}-{start + length - 1}")
    with urllib.request.urlopen(req) as r:
        data = r.read()
    assert len(data) == length, (len(data), length)
    return data


def head_size(url):
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req) as r:
        return int(r.headers["Content-Length"])


def parallel_get(url, start, length, streams):
    part = (length + streams - 1) // streams

    def piece(i):
        off = start + i * part
        return range_get(url, off, min(part, start + length - off))

    with ThreadPoolExecutor(streams) as ex:
        return b"".join(ex.map(piece, range(streams)))


def timed(label, fn, nbytes=None):
    t0 = time.time()
    out = fn()
    dt = time.time() - t0
    rate = f"  ({nbytes / dt / 1e6:5.1f} MB/s)" if nbytes else ""
    print(f"{label:<28} {dt:8.2f}s{rate}")
    return out, dt


def main():
    # -- 1. metadata over HTTP (footer read; both paths pay this once) -------
    def read_meta():
        con = http_con()
        rows = con.execute(
            "SELECT row_group_id, row_group_num_rows, path_in_schema,"
            "       data_page_offset, dictionary_page_offset,"
            "       total_compressed_size "
            "FROM parquet_metadata(?)",
            [URL],
        ).fetchall()
        con.close()
        return rows

    meta, t_meta = timed("metadata (HTTP footer)", read_meta)

    # row-group start positions (file_row_number space) and chunk ranges
    rg_rows = {}  # rg_id -> num_rows
    chunks = {}  # (rg_id, col) -> (start_byte, length)
    for rg, nrows, col, dpo, dico, size in meta:
        rg_rows[rg] = nrows
        start = min(x for x in (dpo, dico) if x is not None and x > 0)
        chunks[(rg, col)] = (start, size)

    rg_start = {}
    pos = 0
    for rg in sorted(rg_rows):
        rg_start[rg] = pos
        pos += rg_rows[rg]

    for rg in (RG_A, RG_B, RG_C):
        s, ln = chunks[(rg, BIG_COL)]
        print(f"  rg{rg} '{BIG_COL}' chunk: offset={s} size={ln / 1e6:.1f}MB rows={rg_rows[rg]}")

    def in_list(rg):
        base = rg_start[rg]
        return ",".join(str(base + i) for i in range(N_ROWS))

    def http_query(rg):
        con = http_con()
        q = (
            f"SELECT file_row_number, {BIG_COL} "
            f"FROM read_parquet('{URL}', file_row_number=true) "
            f"WHERE file_row_number IN ({in_list(rg)})"
        )
        out = con.execute(q).fetchall()
        con.close()
        return out

    size = head_size(URL)
    tail = range_get(URL, size - 8, 8)
    assert tail[4:] == b"PAR1"
    footer_len = int.from_bytes(tail[:4], "little")
    footer = range_get(URL, size - 8 - footer_len, footer_len + 8)

    def coalesced(rg, streams):
        cstart, clen = chunks[(rg, BIG_COL)]
        if streams == 1:
            chunk = range_get(URL, cstart, clen)
        else:
            chunk = parallel_get(URL, cstart, clen, streams)

        with open(SPARSE, "wb") as f:
            f.write(b"PAR1")
            f.seek(cstart)
            f.write(chunk)
            f.seek(size - 8 - footer_len)
            f.write(footer)

        con = duckdb.connect(":memory:")
        q = (
            f"SELECT file_row_number, {BIG_COL} "
            f"FROM read_parquet('{SPARSE}', file_row_number=true) "
            f"WHERE file_row_number IN ({in_list(rg)})"
        )
        out = con.execute(q).fetchall()
        con.close()
        return out

    csize = lambda rg: chunks[(rg, BIG_COL)][1]

    base_rows, t_base = timed(f"A: duckdb HTTP (rg{RG_A})", lambda: http_query(RG_A), csize(RG_A))
    coal1_rows, t_coal1 = timed(
        f"B: coalesced x1 (rg{RG_B})", lambda: coalesced(RG_B, 1), csize(RG_B)
    )
    coal8_rows, t_coal8 = timed(
        f"C: coalesced x{N_STREAMS} (rg{RG_C})", lambda: coalesced(RG_C, N_STREAMS), csize(RG_C)
    )
    for r in (base_rows, coal1_rows, coal8_rows):
        assert len(r) == N_ROWS

    ref_rows, _ = timed(f"verify HTTP (rg{RG_C}, warm)", lambda: http_query(RG_C))
    match = sorted(coal8_rows) == sorted(ref_rows)
    print(f"\ncorrectness: {'MATCH' if match else 'MISMATCH'}")
    print(f"A -> B speedup: {t_base / t_coal1:.1f}x | A -> C speedup: {t_base / t_coal8:.1f}x")
    if not match:
        sys.exit(1)


if __name__ == "__main__":
    os.path.exists(SPARSE) and os.remove(SPARSE)
    main()
