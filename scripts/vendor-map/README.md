# vendor-map

Build workspace for the Map Viewer's browser rendering stack, committed under
`fused_render/templates/vendor/`:

- `map.bundle.mjs` — everything `templates/map/template.html` draws with, as
  one ESM file (see `map-entry.mjs` for the exact exports):
  - `maplibre-gl` — the map (one copy, so the vector and Zarr layers register
    on the same instance the page creates)
  - `maplibre-gl-raster` — GeoTIFF/COG/VRT/STAC read by HTTP range request and
    drawn on the GPU via deck.gl; band choice, rescale, colormap, stretch,
    gamma and nodata are shader uniforms, so restyling never refetches
  - `maplibre-gl-vector` — GeoJSON as-is; GeoParquet, Shapefile, GeoPackage,
    FlatGeobuf, CSV, KML… through DuckDB-WASM, tiled in the browser. A file
    holding several layers (a GeoPackage with several tables, a zip of
    shapefiles, a KML with folders) is never expanded by the library: the page
    answers its `selectLayers` hook itself, so the library's own layer picker
    never opens, and the map document gets one layer per source layer, each
    with `options.source_layer` (the `split_layer` command)
  - `@carbonplan/zarr-layer` + `zarrita` — Zarr v2/v3 as a MapLibre custom layer
  - `pmtiles` — the `pmtiles://` protocol
  - `maplibre-gl-lidar` — LAS/LAZ/COPC/EPT point clouds through deck.gl's
    PointCloudLayer; COPC and EPT stream by viewport (octree nodes by range
    read), LAS/LAZ (<= 1.3) are read whole. The page makes one control per
    layer and hides its panel
- `map.worker.bundle.mjs` — the GeoTIFF tile decoder the raster reader runs off
  the main thread.
- `parquet.worker.bundle.mjs`: the streaming GeoParquet reader
  (`parquet-worker.mjs`, on `hyparquet` + `hyparquet-compressors`). See
  "Streaming a big GeoParquet" below.
- `laz-perf.wasm` — the LAZ decoder the point-cloud library loads; `build.sh`
  points the bundle at this copy instead of unpkg, so LAZ/COPC opens offline.
- `map.bundle.css` — MapLibre's, the vector library's and the point-cloud
  library's stylesheets.

## Point-cloud rendering

`pointcloud-gpu.mjs` adapts the pinned lidar manager while keeping its COPC/EPT
viewport loader. The viewer requests at most two octree nodes concurrently,
debounces viewport requests by 200 ms, and uses a two-million-point streaming
budget per cloud. Plain LAS/LAZ files still load whole.

Positions, intensity, classification and optional RGB use binary deck.gl
attributes, split into chunks of at most one million points. Style changes
reuse those buffers: a 256-entry colormap texture and class colour/visibility
texture drive the shaders, while elevation filtering and height offset are
uniforms. Hidden points are discarded in the picking pass too. Multiple style
updates are coalesced into one animation frame, and one bulk class mask covers
new streamed batches. Automatic percentile ranges sample at most 16,384 points;
their values may differ slightly from full-cloud percentiles.

Extra attributes stay in the loader's CPU arrays and are read only for the
picked point, rather than uploaded for rendering. The loader still decodes LAZ
and merges streamed batches using its existing implementation; this adapter
does not introduce worker decoding or lazy network reads of extra attributes.
Streaming batches can therefore still require new GPU uploads. The budget is
per cloud, so opening many clouds increases total memory use.

Run the buffer reuse, lookup and picking tests with:

```sh
node --test pointcloud-gpu.test.mjs
```

## Streaming a big GeoParquet

A remote GeoParquet, or a local one over 256 MB, is first offered to
`parquet.worker.bundle.mjs` rather than DuckDB. The worker reads the footer
(a HEAD for the size, then one range request for the last 512 KB) and indexes
every row group by its bounding box: the GeoParquet 1.1 `covering` column's
statistics, an Overture-style `bbox` struct's, or native Parquet geospatial
statistics. The layer is ready as soon as that index exists; the map shows the
row-group footprints until the view is close enough that the row groups under
it fit a 48 MB budget, then fetches only those (geometry plus the light
primitive columns, two at a time, nearest the centre first), decodes them in
the worker and hands each to MapLibre as its own GeoJSON source. A read whose
row group leaves the view before it lands is aborted, and a failed one is
retried after 10 s. Loaded row groups stay cached up to 256 MB of decoded
GeoJSON. Columns averaging more than 8 bytes a row (an id, a long text) are
left out of what is drawn and read for one feature when it is clicked.

The worker declines, and the file goes to DuckDB as before, when the geometry
is not WKB, is in a projected CRS, the row groups carry no longitude/latitude
bounding boxes, or they are too broad to narrow a view down (over the budget
in total, with one row group over it or the median row group covering more
than a quarter of the extent). The page logs the reason.

## Why the map renders in the browser

The previous viewer tiled everything in a Python daemon: a describe call, then
one HTTP round trip per 256 px tile, each rendered by rio-tiler or a hand-rolled
MVT encoder. Reading the data where it is drawn removes that server entirely:
a COG paints from its overviews and refines in place, a 1M-feature GeoParquet
is tiled by DuckDB inside the page, and a style change is a uniform update.
Python (`templates/map/prepare.py`) is left with only what a browser cannot
read — striped/non-TIFF rasters (to COG), NetCDF/HDF5 (to Zarr), a user's `.py`.

## Runtime network use

deck.gl, MapLibre and the readers are all in the bundle. Two things still come
from a CDN on first use, by the vector library's design: DuckDB-WASM
(jsDelivr) and its `spatial` extension (extensions.duckdb.org). Offline, the
page falls back to `prepare.py`'s GeoJSON conversion for vectors, which lists
a multi-layer file's layers the same way and then converts each one.

## Rebuild

```sh
./build.sh
```

Needs `bun` on PATH. Versions are pinned in `package.json`; the `overrides`
block holds every `@deck.gl/*` package at 9.4.0 (the point-cloud library needs
9.4; the raster library accepts it) and every `@luma.gl/*` package at the
version deck.gl 9.4 was built against — a mixed luma.gl fails at bundle time (missing shader-plugin exports)
or at run time ("luma.gl has already been initialized"). Only the built files
are committed; `node_modules/` is git-ignored.

Two build details are load-bearing:

- `--alias:module=./node-stub.mjs` — a dependency imports Node's `module` behind
  a browser-false runtime check, but esbuild still has to resolve it.
- The decoder worker is bundled from its package file directly (a merely
  imported worker is tree-shaken away: its package claims `"sideEffects":
  false`), and the pool's `new URL("./worker.js", import.meta.url)` is
  rewritten to `./map.worker.bundle.mjs` after bundling; esbuild leaves that URL
  untouched, and `build.sh` fails if the pattern ever stops matching.
