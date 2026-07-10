# Connectors prototype — findings

**Question:** Can fused-render treat rclone-managed remote mounts (Google
Drive, S3-compatible) as ordinary local paths, with the app owning the mount
lifecycle, while everything downstream stays "local only"?

**Verdict: yes — feasible, with two caveats (deep-zoom latency, unmount-while-open).**

Prototype files (all throwaway):
- `fused_render/shell/connectors_prototype.py` — /api/connectors backend
- `frontend/src/views/Connectors.tsx` — /view/_connectors page
- small wiring edits: server.py (router include), App.tsx (sentinel),
  Sidebar.tsx (footer button), lib/api.ts (wrappers)

## What was verified (macOS 26.4, rclone v1.74.4, public S3 sentinel-cogs bucket)

1. **No macFUSE needed.** `rclone nfsmount` uses macOS's built-in NFS client;
   mounts appear in ~15s worst case, `os.path.ismount` detects them.
2. **Zero downstream changes.** `/api/fs/list`, `/api/fs/stat`, template
   resolution, and the geotiff tile-server daemon all worked unmodified
   against a 204 MB COG living in S3.
3. **S3 remotes are creatable non-interactively** (`rclone config create …
   s3 …`), including anonymous access to public buckets. Credentials live in
   rclone's config; the app stores none.
4. **Google Drive**: wired via rclone's own OAuth browser flow
   (`rclone config create <name> drive`); needs a human to approve, so it was
   not machine-verified here. UI notice tells the user a browser tab opens.

## Measured behavior (the caveats)

- COG /meta (header parse): **6.4s cold**.
- Overview-zoom tiles (z9): **~10ms** after meta.
- Deep-zoom tile (z12) cold: **107–134s** — the tiff reader does bulk
  native-resolution strip reads; harmless locally, massive over the network.
  rclone vfs tuning (`full` cache, 2M chunked reads) does NOT fix the first
  read, but makes every later read of the touched region **~20ms** (sparse
  file cache). A real feature would need either reader-level windowed reads
  or an explicit "download on first preview" affordance for large rasters.
- **Unmount fails EBUSY while a tile-server daemon holds a file open.**
  Quitting the daemon frees it. Prototype answers with `umount -f` fallback;
  a real feature should ask daemons to release (they already expose /quit).

## Provider desktop clients — explored, then removed

Round 2 tried "local" connectors: detecting vendor desktop apps' synced
folders (macOS `~/Library/CloudStorage`) and registering them mount-free.
It worked (verified against live Google Drive Desktop), but was REMOVED in
round 3: detection paths and install guidance are macOS-specific (Google
Drive and OneDrive have no official Linux clients at all), and the two-tier
model complicated the connector concept. Decision: rclone-only — one
OS-agnostic mechanism for every backend, consumer clouds included.
(Users can still just browse/bookmark ~/Library/CloudStorage paths directly;
that needs no feature.)

## DuckDB-over-mount benchmark (362MB Ookla parquet, 12 row groups)

Question: can the duckdb reader's first load be made faster over a mount?
Answer: **no reader change helps — the floor is the file's row-group size.**
Measured cold (vfs cache wiped each round):
  * DESCRIBE ≈ 2-10s (footer), COUNT(*) ≈ 0s (parquet metadata only).
  * First page ≈ 15-22s regardless of query shape: the row_number() window
    vs a direct LIMIT made no difference, and threads=1 was slower. LIMIT
    100 must fetch+decode row group 0 ≈ 31MB compressed across 11 columns;
    that's pure network throughput.
  * WARM repeat: 0.01s — rclone's sparse read cache absorbs everything.
So the lever is cache retention, not SQL: raised --vfs-cache-max-age to
24h (default 1h evicted chunks within the hour), chunk sizes to 8M/64M.
--vfs-read-ahead 128M was measured a net LOSS (slower footer read, wasted
bytes) and left out. Deep-narrow row groups (or remote-optimized layouts
like partitioned datasets) are the real fix, and that's the data's problem,
not the viewer's.

## Notes for a real implementation

- Mount lifecycle is in-memory + atexit, but measured: a pkill'd server
  ORPHANS its rclone child and the NFS mount survives. The next server
  re-detects it via os.path.ismount and can still unmount (umount by path,
  not by tracked process). A real feature should adopt orphaned mounts
  deliberately (or manage rclone via its rc API) rather than rely on atexit.
- `walk` over a mount works but each dir listing is an S3 LIST round-trip;
  fine at prefix scope, dangerous at bucket root. Consider depth limits.
- "Local only, forever" (DECISIONS D2/D3) survives reframed: the app still
  only ever sees local absolute paths; remoteness lives in the mount.
