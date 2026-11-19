# Dataset layouts
trigger: layout, partition, partitioned, hive, year=, theme=, _sample, _metadata, _manifest.json, _parts, .fused, overview, hex, h3, chunk, row group, table, geoparquet, stac, lance, shapefile, tif, cog, raster, csv, geojson, how is it stored, how many rows

How a dataset is stored decides how to read it. List the folder (`s3_search` with `path`) and match what you see against the layouts below. Say which one it is in your answer, and in memory.

## 1. A Fused table (a partitioned geo table)

    <table>/0.parquet 1.parquet ... N.parquet
    <table>/_sample
    <table>/_metadata

Written by Fused's ingest (`fused.ingest`). Rows are partitioned in space: each numbered file covers one area, and each row group inside it (a "chunk") a smaller one. Example: `s3://fused-asset/census/tract_geom_2025/` (census tracts; on 2026-11-18 it had 35 files, 1,100 chunks, 85,529 rows).

- `_metadata` is a Parquet file with no data of its own: its footer holds the schema and the row groups of the whole table, so its footer alone gives the table's columns and total row count (`peeking-into-files.md`).
- `_sample` is a small Parquet sample of the rows, with two extra columns, `file_id` and `chunk_id`. Its footer key `fused:_metadata_json` holds `num_rows`, `num_files` and `num_chunks` as plain JSON.
- Every file's footer has `geo` (GeoParquet: geometry column, encoding `WKB`, CRS) and `fused:job_meta`, the ingest job's settings, whose `input` lists the source files the table was made from (for the tracts, `https://www2.census.gov/geo/tiger/TIGER2025/TRACT/...zip`). That is where the data came from.
- Its row count without a UDF: the `_sample` footer (not `_metadata`'s: there the count is binary). Read `_sample`'s last 8 bytes for the footer length, then the whole footer, and Grep it for `"num_rows": \d+, "num_files": \d+, "num_chunks": \d+` (`peeking-into-files.md`, step 3). With a UDF: `profile_table.py`.
- Read it in a UDF with `fused.get_chunks_metadata(url)` (one row per chunk: its bbox, `file_id`, `chunk_id`, `num_rows`) and `fused.get_chunk_from_table(url, file_id, chunk_id, columns=[...])` (one chunk as a GeoDataFrame). Loading the whole table at once is rarely right: read the chunks that cover the area you need.

## 2. An H3 hex dataset

    <dataset>/[<key>=<value>/]585729635325575167.parquet ...   files named by an H3 cell id (a decimal integer)
    <dataset>/[<key>=<value>/]overview/hex3.parquet ... hex9.parquet
    <dataset>/[<key>=<value>/].fused/metadata.parquet
    [README.md]

A raster or vector converted to H3 cells, mostly under `s3://fused-asset/hex/`. Example: `s3://fused-asset/hex/cdls_v8/year=2024/` (the USDA crop data layer for 2024; on 2026-11-18 it had 183 data files, about 7.8 billion rows of `hex`, `data`, `cnt`, `cnt_total`).

- Each file holds the cells under one coarse cell, at a fine resolution. Columns are usually `hex` (the H3 cell as an unsigned 64-bit integer) and the value columns; for a categorical raster `data` is the class code and `cnt` / `cnt_total` the pixel counts behind it.
- `overview/hex<N>.parquet` are the same data aggregated to resolution N (3 is coarsest). They are small (hex3 of cdls_v8: about 30,000 rows, 225 KB) and carry `lat` / `lng` of each cell: read an overview first to see coverage and values.
- `.fused/metadata.parquet` indexes every row group by its H3 range (`file_path`, `row_group_index`, `h3_min`, `h3_max`, `h3_resolution`); Fused's H3 readers use it to read only the cells asked for.
- A `README.md` beside the data, when there is one, is the best description there is: read it first (`hex/aef_v1/year=2024/README.md` gives the area, the resolution, every column and how it was verified).
- When totalling rows, leave out `overview/` and dot or underscore folders: they repeat the data.

## 3. An H3 ingest with a manifest

    <dataset>/_manifest.json
    <dataset>/_parts/<partition>.json
    <dataset>/<key>=<value>/800ffffffffffff.parquet ...   files named by a resolution-0 H3 cell in hex
    <dataset>/.fused-h3-source/<partition>-<hash>/_read.json

Points or events indexed by H3, newer than layout 2. Examples: `s3://fused-asset/data/jason3_water_levels/points/`, `s3://fused-asset/data/ais/ais_h3_v3/`.

- `_manifest.json` is small plain JSON and answers most questions at once: `files`, total `rows`, `settings` (`h3_res`, `partition_by`, `sort_by`, `row_group_size`, compression), `partitions` with their types, and `folder_rows` (rows per partition). Read it whole with `s3_read` (it is a few KB; a dataset with many partitions has a larger one, so read a range first).
- `.fused-h3-source/<partition>/_read.json` holds the row count read from the source for that partition (`{"spec_hash": ..., "rows": ...}`): compare it with `folder_rows` to see whether a partition was ingested completely.
- A partition folder that is in the listing but not in the manifest is unfinished or abandoned. Say so.

## 4. Hive partitions in general

A folder named `<key>=<value>/` (`year=2024/`, `date=2024-07-01/`, `theme=buildings/type=building/`) is a partition: the key is a column of the data whose value is in the path, and is usually not stored in the files themselves. List the partition level to know which values exist (gaps in dates are common: `ais_h3_v3` skips days). DuckDB, `pyarrow.dataset` and Fused read them as one table; a partition column's type is guessed from the path (`year` becomes a number) and can clash with a column of the same name inside the files, as in `jason3_water_levels/points/`. The profiling UDF for a folder avoids that (`profiling-with-udfs.md`).

## 5. A STAC index table

Parquet whose columns are STAC items: `type`, `stac_version`, `id`, `geometry`, `bbox`, `links`, `assets` (a struct of `href`s), `collection`, `datetime`, `proj:epsg`, `proj:shape`, `proj:transform`. One row per scene or tile; the data itself is at each row's `assets.<name>.href`, often in another bucket. Examples: `s3://fused-asset/dem/usgs30m/`, `s3://fused-asset/stac/`. Report what the hrefs point at (format, where), not only the table.

## 6. Single files

- Parquet (`.parquet`, sometimes `.pq`): one table. A `pandas` key in its footer means it was written by pandas (the index may be a column).
- CSV (`.csv`): the first line is the header; read the first few KB (`peeking-into-files.md`). Large ones are in `misc/` and `data/`.
- GeoJSON (`.geojson`): one JSON document; the first KB shows the `type`, `crs` (if any) and the first feature's `properties` keys.
- Shapefile: several files with one name (`.shp`, `.shx`, `.dbf`, `.prj`, `.cpg`, `.sbn`); the `.prj` is a small text file with the CRS. Or a `.zip` of them.
- GeoTIFF (`.tif`): a raster, often a Cloud-Optimized GeoTIFF (COG) that can be read by window. A `.tif.aux.xml` beside it holds statistics in plain XML. Use the raster UDF for CRS, size, bands and nodata.
- NetCDF (`.nc`), Lance (`.lance` with `_transactions/` and `.manifest`), PMTiles (`.pmtiles`), vector tiles (`.pbf`), models (`.pth`, `.onnx`, `.pkl`): say what they are; the S3 tools only see their bytes.

## 7. What is not a dataset

`tmp/` (scratch text files), `wheel/`, `fonts/`, `logo/`, `wasm/`, demo videos and images. And inside a dataset: `_sample`, `_metadata`, `_manifest.json`, `_parts/`, `.fused/`, `.fused-h3-source/`, `overview/` are sidecars of it, not datasets of their own.
