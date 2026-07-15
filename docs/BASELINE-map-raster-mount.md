# Baseline: map template raster path, opening a COG from a mount

Measured **before** the HTTP-range-read change, on branch `worktree-map-raster-http-range`
(rebased onto `origin/main` @ 7475a09), dev server via `scripts/dev.sh --port 9099`
under a uv venv (Python 3.12, `pip install -e ".[bundled]"`).

## Setup
- Test data: `sentinel` mount = `aws-open:sentinel-cogs/sentinel-s2-l2a-cogs/13/S/DV/2023/6`
  (real Sentinel-2 L2A COGs, tiled, with overviews). Live rclone HTTP serve present.
- Reads today go through the **kernel NFS mount** (rasterio.open on the local
  mountpoint path) — no HTTP range / `/vsicurl`.
- Harness: `scratchpad/bench_map_raster.py` drives the real `/api/run` map_render
  flow + the vector_tile_server daemon's `/rtile` endpoints.
- Method: warm the daemon with one COG (absorbs spawn + rasterio/duckdb/geopandas
  import ≈ 4.6s one-time), then measure a **cold, previously-untouched** COG on the
  warm daemon so `t_open` reflects the mount read, not process startup.
  - `t_open`   = POST /api/run → raster_tiles descriptor (triggers daemon `_ropen`:
    rasterio.open + WarpedVRT + overview-header reads over the mount)
  - `t_tiles9` = fetch a 3×3 grid of `/rtile` PNGs at z10 (cold rtile cache;
    `_rstretch` 512px overview read + per-tile windowed reads)

## Numbers (cold file, warm daemon, current NFS-mount path)

| COG  | t_open (s) | t_tiles9 (s) |
|------|-----------:|-------------:|
| B04  | 1.935      | 0.559        |
| B08  | 4.284      | 1.091        |
| B11  | 0.807      | 0.646        |
| B12  | 3.136      | 0.541        |

- Cold open: **~0.8–4.3s** (median ~2.5s), high variance — S3/rclone network jitter.
- 9-tile screenful: **~0.5–1.1s**.
- One-time daemon spawn+import (excluded above): ~4.6s.

## Heavy load (concurrent tile fan-out, current NFS-mount path)

Harness: `scratchpad/bench_map_raster_heavy.py` — open COG, then fetch EVERY
`/rtile` covering the footprint at a detail zoom, concurrently. Fresh/untouched
band each run (cold).

| scenario                | tiles | wall (s) | tiles/s | p50 (s) | p95 (s) | max (s) | errors |
|-------------------------|------:|---------:|--------:|--------:|--------:|--------:|-------:|
| B02 z12, conc 16        | 225   | 7.62     | 29.5    | 0.099   | 5.47    | 6.58    | 0      |
| B05 z12, conc 32        | 225   | 3.99     | 56.4    | 0.179   | 2.97    | 3.02    | 0      |
| B06 z13, conc 32        | 841   | 12.95    | 64.9    | 0.063   | 1.03    | 11.36   | 0      |

- No mount wedge / errors even at conc 32 (the rebased-main "shared VFS" likely
  helps here). The pain is the **long tail**: individual cold tiles stall 3–11s
  over NFS while p50 stays ~0.1s.
- These are the numbers to beat: after routing reads over `/api/fs/raw`
  (→ direct-to-store parallel range GETs, then serve-cache replay), expect the
  tail (p95/max) and open latency to shrink, and headroom before wedge to grow.

## AFTER (source_url → /vsicurl): regression + root cause

Wiring the map raster path through `source_url` (mirroring duckdb) made cold
concurrent loads **much worse** — B07 z12 c32 timed out (>120s vs baseline B05
20m 3.99s). Root cause, two compounding facts:

1. **The daemon serializes reads.** `_render_rtile` holds `r["lock"]` around every
   WarpedVRT read (rasterio datasets aren't thread-safe), so 225 "concurrent" tile
   requests become 225 *sequential* reads. Concurrency at the HTTP layer buys
   nothing at the read layer — and the map daemon therefore never fans out
   concurrent range reads at all, so it can't wedge the kernel NFS mount the way
   duckdb does. The original wedge motivation largely doesn't apply here.

2. **The server rewrites cold `source_url` → the DIRECT store (S3) URL** (server.py
   ~1597), which is right for duckdb (parallel pooled httpfs) but wrong for a
   serialized reader. Per-read cost, standalone WarpedVRT windowed read:

   | source                         | open  | per-read      |
   |--------------------------------|------:|--------------:|
   | MOUNT (rclone shared full-VFS) | 1.5s  | ~0.01s        |
   | SERVE url via /vsicurl         | 0.9s  | ~0.01s        |
   | DIRECT S3 via /vsicurl         | 3.7s  | **0.5–1.8s**  |

   MOUNT/SERVE are fast because rclone's shared full-VFS (SERVE_VFS_OPT, new main
   71c5896) background-downloads the whole file and caches ranges on disk. DIRECT
   S3 has only GDAL's in-process cache, pays full network latency per range, and
   serialized → catastrophic.

### Conclusion
For the map raster path as architected (serialized reads + rclone shared VFS on
the mount), routing through `/vsicurl` is **not worth it and is harmful if it
lands on the direct store**. Two viable directions:
- **Do nothing** — the mount path already serializes (no wedge risk) and gets the
  shared-VFS cache; it's fine.
- **Route via the SERVE url** (not direct store) — matches mount read speed AND
  keeps reads off the kernel NFS mount (marginal robustness). Requires a
  server-side tweak so this reader isn't sent through the cold→direct rewrite,
  since templates must not know serve URLs.

## Caveats / notes
- This is a **light, single-file, low-concurrency** load — it does NOT reproduce the
  many-concurrent-range-reads fan-out that wedges the macOS NFS client. The change's
  main wins (robustness under load, cold-store parallelism) will show more under a
  heavier RGB / multi-tile / multi-file scenario. Consider adding a heavy-load bench.
- `origin/main` @ 71c5896 already **shares one VFS between the nfsmount and the HTTP
  serve**, so the two paths now share a cache — factor this into before/after.
- After the change, re-run the SAME harness with `file=` pointing at the
  `/api/fs/raw?path=…` (→ `/vsicurl`) locator; compare cold under matched cache state.
