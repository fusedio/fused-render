# zarr_aoi — hosted-Zarr AOI streaming preview for Fused Render

Preview terabyte-scale Zarr stores (local, mounted, or `s3://...`) by streaming
only the chunk byte-ranges that intersect the current viewport. Includes a
"📊 stats" panel showing exactly how many requests/bytes were streamed vs the
logical dataset size, native-pixel click probes with per-read cost, a time/level
slider with background next-chunk prefetch, and shareable view URLs
(`?ll=lon,lat&z=6&index=...&stretch=lo,hi&cmap=turbo`).

## Install

1. Copy (or symlink) this folder to `~/.fused-render/templates/zarr_aoi/`
2. In `~/.fused-render/templates/registry.json`, add:

   ```json
   ".zarr":  ["zarr_aoi"],
   ".zarr/": ["zarr_aoi"]
   ```

3. Open any `.zarr` store in Fused Render (double-click in the file explorer,
   or paste an `s3://bucket/store.zarr` URL in the template's path box).

No manual Python setup: the tile daemon builds its own venv on first run via
`uv` (`~/.cache/fused-render-zarraoi/venv` — zarr 3, s3fs, numpy, crc32c), so
zarr v2 + v3, sharding, and zstd all work regardless of what's bundled with
the app. Requires `uv` on PATH or at `~/.local/bin/uv`.

## Files

- `template.html` — MapLibre viewer UI
- `tile_server.py` — persistent localhost tile daemon (instrumented, caching,
  parallel ranged fetch for big chunks)
- `browse.py` — file-explorer helper
- `icon.svg`

## Good public test stores (anonymous S3, us-west-2)

- 2D + pyramid: `s3://us-west-2.opendata.source.coop/mindearth/wsf/World_WSF_20160701-20260101.zarr` (8 TB, zarr v3, sharded)
- 3D multi-var timeseries: `s3://mur-sst/zarr-v1` (SST, 6,443 daily steps)
- 4D: `s3://cmip6-pds/CMIP6/CMIP/NCAR/CESM2/historical/r10i1p1f1/Amon/ua/gn/v20190313`

Note: stores without overview pyramids can't stream a world view (the template
shows a "zoom in" banner instead), and time-jump latency is dictated by the
store's chunk size — the stats panel warns when chunks exceed 32 MB.
