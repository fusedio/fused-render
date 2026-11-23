# Profiling data with UDFs
trigger: run a udf on data, profile, schema, column types, row count, sample rows, value counts, distinct values, min max, extent, bbox, coverage, crs, raster, tif, table, chunks, get_chunks_metadata, get_chunk_from_table, partitions, how many rows

When the footer, the manifest and the listing do not answer the question (the column TYPES, real sample rows, which values a column takes, a table's extent, a raster's CRS and nodata), run one of the UDFs below on Fused. They read the data where it lives, read as little as they can and return a small JSON-like dict.

How to run one (the exact forms and rules are in `running-udfs.md`):

1. Look in your scratch folder (`.fused/udfs/`, full path in the message that starts your session) for the file; if it is not there, Write it there with the name given below, exactly as below.
2. Run it by path, with the parameters for your dataset:

       fused workbench --env prod run --engine remote scratch /full/path/to/.fused/udfs/profile_parquet.py --path s3://fused-asset/census/tract_geom_2025/0.parquet

   Use an environment you were given (the message names them). `scratch` is a placeholder canvas name; the file is the UDF.
3. Report the numbers you got and the command. Put what describes the dataset into memory in your chat (one line per fact, starting with the dataset's path).

Rules for these runs:

- Return a dict, never a DataFrame: the CLI prints a DataFrame as its truncated text (`...` in place of columns and rows), and a dict as JSON. Keep it small: no geometry bytes, values cut to 80 characters, at most a few sample rows.
- Read one file, one chunk or one column, not the whole dataset. A UDF that reads a whole multi-GB table times out, costs the user money and tells you nothing a sample would not.
- The same command with the same parameters may come back from Fused's cache at once; pass `--no-cache` only when you need a fresh read.
- If a run fails, quote the error. A missing package or a permission error on a path is an answer too (the data may not be readable from Fused): say so and put it in memory under `## Does not work`.

## One Parquet file: `profile_parquet.py`

Columns and types, row count, row groups, metadata keys, CRS and `n` sample rows (geometry shown by its size). Parameters: `--path s3://...parquet`, `--n 5`.

```python
@fused.udf
def udf(path: str = "s3://fused-asset/census/tract_geom_2025/0.parquet", n: int = 5):
    # One Parquet file: its columns and types, row count, metadata keys, CRS, and n sample rows.
    import json
    import pyarrow.parquet as pq
    f = pq.ParquetFile(path)
    schema, meta = f.schema_arrow, f.metadata
    kv = {k.decode(): v.decode("utf-8", "replace") for k, v in (meta.metadata or {}).items()}
    crs = None
    if "geo" in kv:
        geo = json.loads(kv["geo"])
        col = geo.get("columns", {}).get(geo.get("primary_column"), {})
        crs = (col.get("crs") or {}).get("name") or "OGC:CRS84 (GeoParquet default)"
    rows = f.read_row_group(0).slice(0, n).to_pylist() if meta.num_row_groups else []

    def cut(v):  # a value as JSON can show it: bytes (a WKB geometry) by size, long text cut at 80 characters
        if isinstance(v, (bytes, bytearray)):
            return f"<{len(v)} bytes>"
        if isinstance(v, (int, float, bool, type(None))):
            return v
        return str(v) if len(str(v)) <= 80 else str(v)[:80] + "…"
    return {"rows": meta.num_rows, "row_groups": meta.num_row_groups,
            "columns": {fld.name: str(fld.type) for fld in schema},
            "metadata_keys": sorted(k for k in kv if k != "ARROW:schema"), "crs": crs,
            "sample": [{k: cut(v) for k, v in r.items()} for r in rows]}
```

## A Fused table: `profile_table.py`

For a folder of `0.parquet ... N.parquet` with `_sample` and `_metadata` (`dataset-layouts.md` layout 1): total rows, files, chunks, the bounding box of all chunks, the columns as GeoPandas reads them and the CRS, from the chunk metadata and one chunk. Parameter: `--url s3://.../<table>/`.

```python
@fused.udf
def udf(url: str = "s3://fused-asset/census/tract_geom_2025/"):
    # A Fused table (a folder of 0.parquet, 1.parquet, ... with _sample and _metadata): its size and extent from the
    # chunk metadata, and its columns from one chunk, without reading the table.
    chunks = fused.get_chunks_metadata(url)
    first = fused.get_chunk_from_table(url, chunks.file_id.iloc[0], chunks.chunk_id.iloc[0])
    return {"rows": int(chunks.num_rows.sum()), "files": int(chunks.file_id.nunique()), "chunks": len(chunks),
            "bbox": [round(float(chunks.bbox_minx.min()), 4), round(float(chunks.bbox_miny.min()), 4),
                     round(float(chunks.bbox_maxx.max()), 4), round(float(chunks.bbox_maxy.max()), 4)],
            "columns": {c: str(t) for c, t in first.dtypes.items()},
            "crs": first.crs.to_string() if getattr(first, "crs", None) is not None else None}
```

## A folder of Parquet files: `profile_folder.py`

Any folder of `.parquet` files, partitioned (`year=2024/`) or not, H3 datasets included: file count, total rows from the footers (no data read), the partition keys and values in the paths, and the first file's columns. Skips sidecars and `overview/`. Parameters: `--url s3://.../<folder>/`, `--max_files 400` (rows are summed over the first `max_files` files; `rows_counted_in` says how many).

```python
@fused.udf
def udf(url: str = "s3://fused-asset/data/jason3_water_levels/points/", max_files: int = 400):
    # A folder of Parquet files at any depth (hive partitions such as `year=2024/` included): the files, the
    # partition keys and values in their paths, the columns of the first file and the total row count from the
    # footers. Reads footers only, no data. Skips `.`/`_` names (sidecars such as _sample, _metadata, .fused/) and
    # `overview/` folders (lower-resolution copies of an H3 dataset), which would count rows twice.
    import pyarrow.fs as pafs
    import pyarrow.parquet as pq
    fs, root = pafs.FileSystem.from_uri(url)
    infos = fs.get_file_info(pafs.FileSelector(root, recursive=True))
    skip = lambda rel: any(s.startswith((".", "_")) or s == "overview" for s in rel.split("/"))
    files = sorted(i.path for i in infos if i.is_file and i.path.endswith(".parquet") and not skip(i.path[len(root):].strip("/")))
    parts = {}
    for p in files:
        for seg in p[len(root):].strip("/").split("/")[:-1]:
            if "=" in seg:
                k, v = seg.split("=", 1)
                parts.setdefault(k, set()).add(v)
    counted = files[:max_files]
    rows = sum(pq.read_metadata(p, filesystem=fs).num_rows for p in counted)
    first = pq.read_schema(files[0], filesystem=fs) if files else None
    return {"files": len(files), "rows": rows, "rows_counted_in": len(counted),
            "columns": {f.name: str(f.type) for f in first} if first else {},
            "partitions": {k: sorted(v)[:20] + (["…"] if len(v) > 20 else []) for k, v in parts.items()},
            "first_files": ["s3://" + p for p in files[:3]]}
```

## One raster: `profile_raster.py`

CRS, width and height, band count and dtypes, nodata, resolution, bounds, overviews, block shape and the first tags, from the header only. Parameter: `--path s3://...tif`.

```python
@fused.udf
def udf(path: str = "s3://fused-asset/demos/seismic_data/v2023_1_pga_475_rock_3min.tif"):
    # One raster (GeoTIFF / COG): CRS, size, bounds, bands, dtype, nodata, overviews; reads the header only.
    import rasterio
    with rasterio.open(path) as r:
        return {"crs": str(r.crs), "width": r.width, "height": r.height, "count": r.count,
                "dtypes": list(r.dtypes), "nodata": r.nodata, "res": list(r.res),
                "bounds": [round(b, 6) for b in r.bounds], "overviews": r.overviews(1),
                "blocks": [list(b) for b in r.block_shapes], "tags": dict(list(r.tags().items())[:10])}
```

## One column's values: `column_values.py`

One column of one Parquet file: type, nulls, min / max / mean when numeric, number of distinct values and the most common ones with counts. Use it to learn what codes a column holds (crop classes, land cover classes, categories). Parameters: `--path s3://...parquet`, `--column data`, `--top 20`. For an H3 dataset, run it on an `overview/hex<N>.parquet` first: it is small and covers everything.

```python
@fused.udf
def udf(path: str = "s3://fused-asset/hex/cdls_v8/year=2024/overview/hex3.parquet", column: str = "data", top: int = 20):
    # One column of one Parquet file (read alone): type, nulls, min / max / mean when numeric, distinct count and the
    # most common values with their counts.
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    col = pq.read_table(path, columns=[column]).column(column)
    out = {"rows": len(col), "type": str(col.type), "nulls": col.null_count}
    if pa.types.is_integer(col.type) or pa.types.is_floating(col.type):
        mm = pc.min_max(col)
        out.update(min=mm["min"].as_py(), max=mm["max"].as_py(), mean=pc.mean(col).as_py())
    vc = pc.value_counts(col)
    vc = sorted(((v["values"], v["counts"]) for v in vc.to_pylist()), key=lambda x: -x[1])
    out["distinct"] = len(vc)
    out["top"] = [[str(v)[:80], c] for v, c in vc[:top]]
    return out
```

## Writing your own

Start from the closest one above. Keep the same habits: one function decorated with `@fused.udf`, imports inside it, typed parameters with defaults, a dict out, and the smallest read that answers the question (`columns=[...]`, one row group, one chunk, a bounding box, a short date range). Name the file for what it does and the dataset it is for (`count_ais_points_by_date.py`), not `test.py`.
