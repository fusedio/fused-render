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
    FlatGeobuf, CSV, KML… through DuckDB-WASM, tiled in the browser
  - `@carbonplan/zarr-layer` + `zarrita` — Zarr v2/v3 as a MapLibre custom layer
  - `pmtiles` — the `pmtiles://` protocol
  - `maplibre-gl-lidar` — LAS/LAZ/COPC/EPT point clouds through deck.gl's
    PointCloudLayer; COPC and EPT stream by viewport (octree nodes by range
    read), LAS/LAZ (<= 1.3) are read whole. The page makes one control per
    layer and hides its panel
- `map.worker.bundle.mjs` — the GeoTIFF tile decoder the raster reader runs off
  the main thread.
- `laz-perf.wasm` — the LAZ decoder the point-cloud library loads; `build.sh`
  points the bundle at this copy instead of unpkg, so LAZ/COPC opens offline.
- `map.bundle.css` — MapLibre's and the point-cloud library's stylesheets.

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
page falls back to `prepare.py`'s GeoJSON conversion for vectors.

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
