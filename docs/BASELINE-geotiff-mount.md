# Baseline: geotiff template daemon-tile path, opening a COG from a mount

Measured on branch `map-raster-http-range` (worktree), dev server via
`scripts/dev.sh --port 9099` under a uv venv (Python 3.12, `.[bundled]`), geotiff
daemon under its own uv venv (`~/.cache/fused-render-geotiff-v2/venv`, numpy +
pyproj + imagecodecs).

## Why geotiff differs from the map path

The two tile daemons have **opposite** read-concurrency models (see
memory `raster-daemon-read-concurrency`):

- **map** `vector_tile_server._render_rtile` holds `r["lock"]` around the whole
  WarpedVRT read → reads are fully serialized → never fans out → can't wedge the
  kernel NFS mount. Routing it off the mount gave no benefit (reverted, see
  `BASELINE-map-raster-mount.md`).
- **geotiff** `tile_server.get_chunk` holds `f["lock"]` only for the chunk-cache
  check/store; the mmap byte read `f["buf"][off:off+count]` (a page fault → NFS
  read) and the decode run OUTSIDE the lock, under `ThreadingHTTPServer`. So N
  concurrent `/tile` requests DO fan out N concurrent mmap-over-NFS page faults.

`_tiff_core.parse_header` mmaps the file (`mmap.mmap(fileno, 0, ACCESS_READ)`),
so every chunk slice is a lazy page fault serviced by the macOS kernel NFS
client against the rclone mount.

## Setup
- Test data: `sentinel` mount, scene `S2A_13SDV_20230607_0_L2A` (real Sentinel-2
  L2A COGs, tiled, deflate, with overviews). Live rclone HTTP serve + shared VFS.
- Harness: `scratchpad/bench_geotiff_heavy.py` — runPython `tile_server.py` →
  `{port}`; GET `/meta` (confirms `supported`, gives bounds); then fetch EVERY
  `/tile/{z}/{x}/{y}.png` covering the footprint at a detail zoom, concurrently.
- Each **cold** run uses a fresh band the daemon has never opened.

## Numbers — COLD (fresh band, current NFS-mmap path)

| scenario                    | tiles | wall (s) | tiles/s | p50 (s) | p95 (s) | max (s) | errors |
|-----------------------------|------:|---------:|--------:|--------:|--------:|--------:|-------:|
| B02 z12, conc 16            |   225 |    0.77  |  291    | 0.054   | 0.067   | 0.117   | 0      |
| B03 z12, conc 32            |   225 |    0.81  |  278    | 0.110   | 0.170   | 0.257   | 0      |
| **B08 z13, conc 32**        |   841 |  **21.1**|   40    | 0.110   | **7.78**| **17.6**| **18** |
| **B04 z14, conc 32 (full)** |  3249 |  **57.3**|   57    | 0.109   | 1.73    | **30.4**| **40** |

Errors are all `URLError: [Errno 60] Operation timed out` — the NFS read stalling
so long the daemon thread never responds and the client socket times out.

## Numbers — WARM (same bands re-run immediately: daemon cache + rclone VFS hot)

| scenario                    | tiles | wall (s) | tiles/s | p95 (s) | max (s) | errors |
|-----------------------------|------:|---------:|--------:|--------:|--------:|-------:|
| B08 z13, conc 32 WARM       |   841 |    2.81  |  299    | 0.139   | 0.449   | 0      |
| B04 z14, conc 32 WARM       |  3249 |   10.8   |  302    | 0.135   | 0.676   | 0      |

## Conclusion

The problem is **real and reproducible**, and it is the *opposite* of the map
finding:

- At shallow zoom (z12) the cold load is fine — few, low-overview chunks; rclone
  VFS keeps up.
- At detail zoom (z13/z14) where many genuinely-cold full-res chunks are read
  concurrently, the fan-out of concurrent mmap page faults **wedges the kernel
  NFS mount**: 18–40 hard timeouts, tail latency 17–30s, throughput collapses
  from ~300 tiles/s (warm) to 40–57 tiles/s.
- Warm re-run of the identical load is clean (0 errors, max <0.7s), proving the
  cause is cold concurrent mmap-over-NFS, not the decoder or CPU.

This is exactly the case where routing reads off the kernel NFS mount is
justified — unlike map. The fix: give the geotiff daemon a byte source that,
for mount-backed files, does HTTP **range** reads against the server's
`/api/fs/raw` instead of mmap-over-NFS. Only the CONCURRENT chunk reads
(`get_chunk`) route through it; the single-threaded header parse (`parse_header`,
brief, never wedges) still rides an mmap that is then closed for remote files
(dropping the EBUSY pin). Warm numbers are the target the change must preserve;
cold z13/z14 timeouts + tail are what it must eliminate.

## THE CHANGE (implemented)

- `tile_server.py`:
  - `_RangeReader` — HTTP range GET against `/api/fs/raw`.
  - `open_file(path, src)` — for a mount-backed file attaches the reader and
    **closes the mmap** (drops the EBUSY pin); the single-threaded header parse
    still rode the mmap briefly (never wedges).
  - `get_chunk` split into fetch + `_decode_chunk`; reads via the reader when
    present, else the original mmap slice (local path byte-for-byte unchanged).
  - `_warm_chunks` — **coalesces** contiguous chunks a window needs into one
    range read (COG tiles are stored contiguously) and fetches runs
    concurrently. Turns latency-bound per-chunk reads into bandwidth-bound bulk
    reads — the thing rclone/kernel readahead did for mmap.
  - `_prefetch_overviews` — on remote open, a background thread bulk-warms every
    reduced-resolution level (skips the full-res base). The browser's ~6-conn
    cap limits tile *requests*, not this daemon-side fetch, so the overviews are
    pulled as a few big GETs and pan/zoom then hits the cache.
- `template.html`: when the shell sets `_remote=1`, pass `src=<origin>/api/fs/raw?
  path=<file>` on the `/meta` and `/tile` queries. Local files pass no `src`.

`/api/fs/raw` behaviour used (unchanged, duckdb-tuned): a cold ranged GET without
`Sec-Fetch-Mode` 307-redirects to the **store** (parallel S3, no wedge) while a
background whole-file prefetch lands in the serve VFS; once prefetched, reads
proxy from the serve's on-disk cache. urllib follows the 307 and preserves the
`Range` header (verified). Connection pooling was tested and did **not** help —
per-read cost is S3 *latency* (~1.28s), not TLS setup; coalescing is the lever.

Correctness verified byte-for-byte: HTTP reads == mmap reads (14/14 sample
chunks), and coalesced-read + slice == individual chunks (6/6 across a 13.4MB
span). `_decode_chunk` is shared/unchanged, so rendered tiles are identical.

## AFTER, B08 z13 cold, fresh scene (per-read latency ≈ 1.28s on this box)

| path                               | conc | t_meta | tiles | total | errors |
|------------------------------------|-----:|-------:|------:|------:|-------:|
| NFS-mmap baseline (clean)          |    6 | 15.1s  |  8.2s | ~23s  | 0      |
| NFS-mmap baseline                  |   32 |  0.8s  | 21.1s | ~22s  | **18** |
| HTTP naive (per-chunk, no prefetch)|   32 | 19.8s  | 40.6s | ~60s  | 0      |
| HTTP + parallel + coalesce         |   32 |  6.3s  | 23.1s | ~29s  | 0      |
| **HTTP + overview prefetch**       |    6 |  5.7s  |  8.6s | **~14s** | 0   |
| **HTTP + overview prefetch**       |   32 |  4.7s  |  6.7s | **~11s** | 0   |
| HTTP warm re-run                   |   32 |  0.9s  |  2.8s | ~4s   | 0      |

- **Wedge eliminated**: 0 errors vs the mmap path's 18 hard `Errno 60` timeouts
  at c32; tile p50 drops to **0.022s** (prefetched cache hits).
- **Cold is now FASTER than the mmap baseline** (~11–14s vs ~22–23s), reversing
  the initial naive-HTTP regression (~60s). The overview prefetch converts the
  screenful of latency-bound per-tile reads into a few bandwidth-bound bulk GETs,
  server-side, off the browser's connection budget.
- **Warm steady-state** identical to mmap (~2.8s tiles, 0 errors).

### Conclusion
Removes the concurrent mmap-over-NFS mount wedge (and the EBUSY pin), renders
byte-identical tiles, is faster than the old mmap path even cold, and leaves the
local-file path unchanged. Net win.
