"""One-off indexer for the public `fused-asset` bucket.

Walks the bucket anonymously (S3 ListObjectsV2 over plain HTTPS, stdlib
only) and writes ONE parquet file listing every object. fused-render
downloads that file and ingests it into its sibling search store
(`fused_render/remote_index.py`), which is how explorer home search finds
bucket keys without ever listing the bucket itself.

Output contract (fused_render/remote_index.py validates it):

- columns: ``key`` string, ``size`` int64, ``last_modified``
  timestamp[ms, UTC], ``etag`` string (quotes stripped),
  ``storage_class`` string
- sorted by ``key``, zstd, one file
- folder markers (keys ending in ``/``) dropped
- keys under EXCLUDED_PREFIXES dropped (internal / not for browsing)
- file-level key-value metadata: ``schema_version=1``, ``bucket``,
  ``region``, ``built_at`` (ISO UTC), ``excluded_prefixes`` (JSON list)

Two entry points, one core (``build_table``):

- CLI, run by a human::

      python scripts/fused_asset_index.py --out /tmp/fused-asset.parquet
      python scripts/fused_asset_index.py --out /tmp/census.parquet --prefix census/

- Fused UDF: paste this whole file into a canvas node and run ``udf``. It
  builds the same table and uploads it to OUTPUT_URL with
  ``fused.api.upload`` (the pattern the marketplace canvas uses to write
  into this bucket). ``fused`` is only touched inside ``udf``; the CLI
  never needs it installed.

The walk is split two folder levels deep and the pieces are listed
concurrently, so a full run takes ~4 minutes instead of the ~15 minutes a
single sequential walk needs. Transient failures (connection
resets, 5xx, SlowDown) are retried with backoff.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

BUCKET = "fused-asset"
REGION = "us-west-2"
ENDPOINT = f"https://{BUCKET}.s3.{REGION}.amazonaws.com/"
OUTPUT_URL = f"s3://{BUCKET}/fused-render-index/fused-asset.parquet"
SCHEMA_VERSION = 1
#: Top-level prefixes never indexed: internal staging, the marketplace's
#: own database, and this index itself.
EXCLUDED_PREFIXES = (
    ".beta/",
    ".beta-dev/",
    "fused-render-marketplace/",
    "fused-render-index/",
)

_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_ATTEMPTS = 8
_TIMEOUT = 60
_WORKERS = 16


# ---------------------------------------------------------------------------
# Listing (stdlib only)
# ---------------------------------------------------------------------------


def _get(params: dict) -> bytes:
    """One ListObjectsV2 request, retried on transient failures."""
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    delay = 1.0
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fused-asset-indexer"})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            # 4xx other than throttling is a real answer (bad prefix, bucket
            # gone private): fail loudly instead of retrying it.
            if e.code < 500 and e.code not in (408, 429):
                raise
            err: Exception = e
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            err = e
        if attempt == _ATTEMPTS:
            raise RuntimeError(f"listing failed after {_ATTEMPTS} attempts: {err}") from err
        time.sleep(delay)
        delay = min(delay * 2, 30.0)
    raise AssertionError("unreachable")


def _text(el, tag: str) -> str | None:
    child = el.find(_NS + tag)
    return child.text if child is not None else None


def _list_page(prefix: str, delimiter: str | None, token: str | None):
    """-> (objects, common_prefixes, next_token). Keys come URL-encoded
    (``encoding-type=url``) so a key with XML-hostile bytes still parses;
    S3 encodes them the way botocore decodes them (unquote_plus)."""
    params = {"list-type": "2", "encoding-type": "url", "max-keys": "1000"}
    if prefix:
        params["prefix"] = prefix
    if delimiter:
        params["delimiter"] = delimiter
    if token:
        params["continuation-token"] = token
    root = ET.fromstring(_get(params))
    objects = []
    for c in root.iter(_NS + "Contents"):
        key = urllib.parse.unquote_plus(_text(c, "Key") or "")
        objects.append(
            (
                key,
                int(_text(c, "Size") or 0),
                _text(c, "LastModified") or "",
                (_text(c, "ETag") or "").strip('"'),
                _text(c, "StorageClass") or "",
            )
        )
    prefixes = [
        urllib.parse.unquote_plus(_text(p, "Prefix") or "")
        for p in root.iter(_NS + "CommonPrefixes")
    ]
    truncated = (_text(root, "IsTruncated") or "").lower() == "true"
    nxt = _text(root, "NextContinuationToken") if truncated else None
    return objects, prefixes, nxt


def _list_all(prefix: str, delimiter: str | None):
    objects, prefixes, token = [], [], None
    while True:
        objs, prefs, token = _list_page(prefix, delimiter, token)
        objects.extend(objs)
        prefixes.extend(prefs)
        if not token:
            return objects, prefixes


def _excluded(key: str) -> bool:
    return key.startswith(EXCLUDED_PREFIXES)


def list_objects(prefix: str = "", workers: int = _WORKERS, log=print):
    """Every object under `prefix` (folder markers and excluded prefixes
    dropped), as ``(key, size, last_modified_iso, etag, storage_class)``.

    Lists two folder levels with a delimiter (collecting the loose keys at
    those levels), then walks each second-level prefix in full, all in a
    thread pool. Wall time is bounded by the biggest second-level folder
    (data/ais, ~40% of the bucket): ~4 minutes for the whole bucket. Deeper
    or adaptive splits were measured and were slower (thousands of tiny
    folders cost one request each)."""
    started = time.monotonic()
    objects: list = []
    units: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # The root is always split, so the excluded top-level prefixes are
        # never listed at all.
        top_objs, top_prefixes = _list_all(prefix, "/")
        objects.extend(top_objs)
        top_prefixes = [p for p in top_prefixes if not _excluded(p)]
        for objs, subs in pool.map(lambda p: _list_all(p, "/"), top_prefixes):
            objects.extend(objs)
            units.extend(subs)
        log(f"listing {len(units)} prefixes under {prefix or '<bucket root>'!r}")
        done = 0
        for objs, _ in pool.map(lambda p: _list_all(p, None), units):
            objects.extend(objs)
            done += 1
            if done % 50 == 0:
                log(f"  {done}/{len(units)} prefixes, {len(objects)} objects, "
                    f"{time.monotonic() - started:.0f}s")

    kept = [o for o in objects if not o[0].endswith("/") and not _excluded(o[0])]
    log(f"listed {len(objects)} objects, kept {len(kept)} "
        f"in {time.monotonic() - started:.0f}s")
    return kept


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


def _ms(iso: str) -> int | None:
    if not iso:
        return None
    dt = datetime.strptime(iso.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
    return int(dt.timestamp() * 1000)


def build_table(prefix: str = "", log=print):
    """The index table (pyarrow.Table) for `prefix` ("" = whole bucket)."""
    import pyarrow as pa

    rows = list_objects(prefix, log=log)
    # No dedupe needed: the delimiter listings return only the loose keys at
    # their own level and the full walks cover disjoint second-level
    # prefixes, so each key is listed exactly once (a retried page keeps
    # only its one successful response). The sort is required: the pieces
    # are collected level by level, not in key order, and fused-render's
    # ingest rejects a file whose
    # keys are not strictly increasing.
    rows.sort(key=lambda r: r[0])
    schema = pa.schema(
        [
            ("key", pa.string()),
            ("size", pa.int64()),
            ("last_modified", pa.timestamp("ms", tz="UTC")),
            ("etag", pa.string()),
            ("storage_class", pa.string()),
        ],
        metadata={
            "schema_version": str(SCHEMA_VERSION),
            "bucket": BUCKET,
            "region": REGION,
            "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "excluded_prefixes": json.dumps(list(EXCLUDED_PREFIXES)),
            "prefix": prefix,
        },
    )
    return pa.table(
        {
            "key": [r[0] for r in rows],
            "size": [r[1] for r in rows],
            "last_modified": [_ms(r[2]) for r in rows],
            "etag": [r[3] for r in rows],
            "storage_class": [r[4] for r in rows],
        },
        schema=schema,
    )


def write_parquet(table, out_path: str) -> None:
    """Atomic write: a reader never sees a half-written file."""
    import pyarrow.parquet as pq

    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".fused-asset-index.", suffix=".parquet", dir=out_dir)
    os.close(fd)
    try:
        pq.write_table(table, tmp, compression="zstd", row_group_size=64 * 1024)
        os.replace(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Fused UDF entry point (UNTESTED from the fused-render side)
# ---------------------------------------------------------------------------


class _NoFused:
    """Stand-in so ``@fused.udf`` below is a no-op when the CLI runs
    without the fused SDK installed."""

    @staticmethod
    def udf(fn=None, **_kw):
        return fn if fn is not None else (lambda f: f)


try:  # the Fused runtime injects `fused` (canvas code never imports it)
    fused  # type: ignore  # noqa: B018
except NameError:  # CLI: never import the SDK here
    fused = _NoFused()  # type: ignore


@fused.udf
def udf(output_url: str = OUTPUT_URL, prefix: str = ""):
    """Build the index and upload it to `output_url`. Returns a one-row
    summary. A full-bucket walk takes ~4 minutes, longer than a realtime
    UDF call is allowed: run it as a batch job."""
    import pandas as pd
    import fused as _fused  # the real SDK; only ever imported here

    table = build_table(prefix)
    with tempfile.TemporaryDirectory() as d:
        local = os.path.join(d, "fused-asset.parquet")
        write_parquet(table, local)
        size = os.path.getsize(local)
        with open(local, "rb") as f:
            _fused.api.upload(f.read(), output_url, timeout=600)
    return pd.DataFrame(
        [
            {
                "output_url": output_url,
                "rows": table.num_rows,
                "bytes": size,
                "built_at": table.schema.metadata[b"built_at"].decode(),
            }
        ]
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="local parquet path to write")
    ap.add_argument(
        "--prefix",
        default="",
        help="only index keys under this prefix (for testing), e.g. census/",
    )
    args = ap.parse_args(argv)

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    table = build_table(args.prefix, log=log)
    write_parquet(table, args.out)
    log(f"wrote {table.num_rows} rows, {os.path.getsize(args.out)} bytes -> {args.out}")
    return 0


# The stand-in check keeps a Fused runtime that execs this file as __main__
# from running the CLI (and argparse exiting) instead of exposing `udf`.
if __name__ == "__main__" and isinstance(fused, _NoFused):
    sys.exit(main())
