# pyramid — overview-pyramid inspector & fixer for GeoTIFFs

Second `.tif`/`.tiff` mode (after the default geotiff viewer). Makes a file's
resolution pyramid tangible, and fixes files that don't have one.

## What it shows

- **3D pyramid** — every resolution level rendered as a real layer (true data,
  not a mockup): dimensions, ground resolution, bytes on disk, internal tile
  grid straight from the IFDs. Click a level to focus, click again to inspect
  face-on, ↑/↓ to step levels, drag to orbit.
- **Same-ground patch** — the same geographic window read from each level at
  its native resolution, so you can *see* what each level throws away.
- **On the map** — swipe any two levels against each other on a basemap at
  their true resolutions (via the geotiff daemon's `/ltile` endpoint), plus an
  "auto" mode that picks the level a COG reader would fetch at the current
  zoom.
- **COG health & fix** — rio-cogeo validation verdict; when the file has no
  pyramid (or an invalid layout) a fix panel offers:
  - *Add overviews in place* (gdaladdo-style append, keeps the file's codec)
  - *Write a proper COG copy* (rio-cogeo `cog_translate` + validate), with
    per-codec size predictions sampled from the file's real blocks
  - long jobs run detached (runPython has a 30s budget) with a progress modal
    polling a status file under `~/.cache/fused-render-compressbench/jobs/`.

## Files

| file | role |
|---|---|
| `template.html` | the view |
| `overview_pyramid.py` | reader: analyze / predict / build / cogify / status |
| `icon.svg` | mode-switcher icon |

The reader manages its own uv venv (`~/.cache/fused-render-compressbench`,
rasterio + rio-cogeo + tifffile + pillow) so it works from the app's bundled
python. The map tab reuses the **geotiff template's tile daemon** via the
relative path `../geotiff/tile_server.py` — one shared daemon, no respawn
fights between the two modes.
