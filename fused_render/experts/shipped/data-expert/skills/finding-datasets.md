# Finding datasets
trigger: find data, which dataset, where is, is there data on, s3_search, list, folder, bucket, fused-asset, version, newest, latest, overture release, census, buildings, crops, elevation, land cover, weather, h3, hex

## The tools

Two tools reach the S3 data, the same ones a task session has: `mcp__fused_render__s3_search` and `mcp__fused_render__s3_read`. They exist only in your chat and when a task session asks you a question; when you learn from a transcript or tidy memory, you have none.

`s3_search` takes exactly one of:

- `query`: a substring of object keys and folder names, for example `{"query": "tract"}`. It returns the best-ranked folders and files, at most three hits per file name, and never lists a whole dataset. Folders whose name starts with a dot are hidden from a search. Use it to find where something lives.
- `path`: one folder, for example `{"path": "s3://fused-asset/census/"}`. It lists ONE level, folders first, then every file with `size` (bytes) and `modified`. `truncated: true` means more: call again with the returned `cursor` and the same `path`. `limit` goes up to 200 per page.

Both read fused-render's own index of the bucket, so they are instant and cost nothing. An `index_not_ready` error means the index is still loading: wait a few seconds and call again. The index is refreshed about once a day, so a file written today may not be in it yet.

## How to search

- Search for the thing, then list its folder. A search hit like `s3://fused-asset/census/tract_geom_2025/12.parquet` means the dataset is the folder `s3://fused-asset/census/tract_geom_2025/`: list it to see all its files and its sidecars.
- Search with several short words, one per call (`tract`, `census`, `tiger`), rather than one long phrase: names are abbreviations and use `_`, `-` and dates.
- Listing the root `s3://fused-asset/` shows about sixty top-level folders. They are a mix of curated datasets, demos and scratch space (next section).
- Sizes tell you how a dataset is stored before you read anything: hundreds of numbered `.parquet` files of tens of MB is a partitioned table; one small `.parquet` is a sample or a lookup table; files named by long numbers like `585729635325575167.parquet` are H3 cells (`dataset-layouts.md`).

## What is where (a snapshot, November 2026)

This map was made from a listing on 2026-11-18. The bucket changes; what you list now and what memory says win over it.

- `overture/<release>/theme=<theme>/type=<type>/`: Overture Maps releases by date (`2024-09-18.0` to `2026-08-19.0`), hive-partitioned by theme and type. The largest data in the bucket (about 15 TB). Themes are `addresses`, `base`, `buildings`, `divisions`, `places`, `transportation`, but not every release holds every theme (on 2026-11-18 the newest, `2026-08-19.0`, held only `buildings`; `2026-07-22.0` was the newest with all six; `2025-09-24.0-5000` is a variant, not a release): list the release.
- `census/`: US Census TIGER 2025 geometry (`tract_geom_2025/`, `block_geom_2025*/`) and H3 versions of blocks (`block_hex_2025_*`).
- `infra/`: US reference layers as partitioned tables: buildings (Overture, Microsoft, OSM, NSI, OAK), Overture transportation, base, places and admin, census blocks, block groups, tracts, roads, water, counties, states, zip codes, watersheds (`hydro_wbdhu*`), power stations.
- `hex/`: rasters and vectors converted to H3 cells: crop data layer (`cdls*`, `CDL`), elevation (`copernicus-dem-90m`, 3DEP), AlphaEarth embeddings (`aef_v1*`), ESA WorldCereal, LandScan population, census, Overture. Each usually has `overview/hex<N>.parquet` files at coarser resolutions.
- `data/`: a mixed shelf of datasets: ERA5 temperature (`era5/`, the largest), AIS ship positions (`ais/`), AlphaEarth embeddings (`aef/`), crop layers (`cdls/`, `GSM19_30m_soybean_2019/`, `WorldCereal2021/`), weather and fire (`gridmet_yearly/`, `seasfire_v3/`), broadband (`fcc_bdc_map_latest/`), TIGER (`tiger/`), soils, NYC 311, satellite altimetry (`jason3_water_levels/`), boundaries (`geoboundaries/`), land cover (`nlcd/`), population (`landscan/`), ACS summary files (`acs/`).
- `stac/`: STAC catalog tables (Microsoft Planetary Computer, Sentinel-2 L2A, NASA CMR): one row per scene with links to the imagery, not the imagery itself.
- `dem/usgs30m/`, `lulc/io*`, `pc_stac/`, `imagery/`: older (2023) STAC-style index tables pointing at rasters elsewhere.
- `misc/`: a large mix of one-off and personal folders (people's names, `hex/`, isochrones, Foursquare POIs `fsq_poi*`, flood rasters `sen1flood/`, soils `gSSURGO*`, CSVs). Check before trusting anything here.
- `demos/`, `samples/`, `docs/`: small files behind demos, docs and file-reader examples (`samples/file_readers/` has one file of each format).
- Not data: `fonts/`, `logo/`, `wheel/`, `wasm/`, `fused-js/`, `gcp/`, `node_exporter/`, `onboarding/`, `changelogs/`, `tmp/`.

## Picking a version

Many datasets exist several times. Prefer, in this order:

1. The one memory says to use.
2. The one the user's canvases read (Grep the canvas copies for its path).
3. The newest complete version: for dated releases the latest date that holds the theme you need; for `_v2`, `_v3`, `_v8` suffixes the highest number whose folder is complete (it has its sidecars and a similar file count to the one before).
4. A curated top-level folder over a personal or scratch one. Treat folders with `test`, `tmp`, `scratch`, `dummy`, `subset`, `sample`, `skipped`, a person's name or a numbered size suffix (`_10k`, `_1000`, `-5000`) as experiments or extracts unless memory or the user says otherwise; say so when you point at one.

When two candidates differ, say how (date, file count, size, columns) instead of choosing silently.

## Data outside the bucket

Today the tools reach `fused-asset` only; another bucket is refused (`unsupported_bucket`). A canvas UDF may read other places (a private bucket, an HTTP URL, a database); you can read its code and say what it reads, and on Fused a UDF may reach it, but your S3 tools cannot. Say which it is.
