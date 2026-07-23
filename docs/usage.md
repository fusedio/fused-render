# Usage & configuration

Reference for fused-render's runtime features and settings. For installing and
running, see the [README](../README.md); for building and development, see
[CONTRIBUTING](../CONTRIBUTING.md).

## Execution engine

Python runs in a fresh subprocess per call through the built-in runner by
default. Opt into fused's local compute backend — which resolves PEP 723
`# /// script` inline requirements into cached venvs — with
`FUSED_RENDER_ENGINE=auto` (use it when `fused` is importable, else the builtin)
or `=fused` (require it); `pip install "fused-render[fused]"` first. Under the
fused engine a file may also expose a `@fused.udf`-decorated function or assign
`result = ...` directly instead of defining `main()`. You can also switch the
engine in [Preferences](#preferences).

## Remote storage (mounts)

The cloud icon at the sidebar's bottom-left opens **Mounts**: remote storage —
S3-compatible object stores, Google Drive, and anything else
[rclone](https://rclone.org) speaks — mounted as local folders under
`~/.fused-render/mounts/`. Everything downstream (previews, readers, tile
servers) sees ordinary local paths.

- **No setup on macOS:** the packaged app bundles rclone itself — no
  install, nothing on PATH. Running from source, or on Linux, still needs
  rclone (`brew install rclone` / your distro's package). macOS mounts via
  the built-in NFS client — no macFUSE; Linux uses FUSE. Windows is not
  supported yet.
- **Credentials never touch fused-render** — they live in rclone's own
  config. S3-compatible remotes can be created from the page; for Google
  Drive and other sign-in backends, run `rclone config` in a terminal once.
- **Mount narrow prefixes** (`bucket/prefix`), not whole buckets — every
  folder listed inside a mount is a remote API call, and search inside a
  mount is capped for the same reason.
- **First open is slow, repeats are fast**: the first read of a large remote
  file downloads what it needs; a local cache (24h retention) makes repeat
  opens near-instant. How slow the first open is depends on the file's
  layout — cloud-optimized formats (COGs, small parquet row groups) behave
  far better than monolithic files.
- Mounts stay up until you unmount them — including across app restarts, and
  every mount is automatically remounted when the server starts.

## Preferences

The gear at the sidebar's bottom-left opens **Preferences**:

- **Execution engine** — switch `fused.runPython` between the built-in
  executor (fresh subprocess per call) and the fused engine (PEP 723 inline
  requirements in cached venvs). Applied to the next run, no restart; setting
  `FUSED_RENDER_ENGINE` pins the engine and locks the switch.
- **Deploy to Fused account** — the opt-in toggle for the preview header's
  Deploy button.
- **Logs** — the path to this run's log file, with an action to reveal it. The
  server writes this log for debugging: when something goes wrong (an "Internal
  Server Error" in the browser, or a misbehaving file-open) it has the traceback.
  Each run writes its own file in your system temp directory (the CLI also prints
  the path on startup; the packaged app reveals it from **menu bar → Open
  logs**). It's disposable — it rotates so it can't grow without bound, and
  living in temp means the OS reclaims it; set `FUSED_RENDER_LOG_DIR` to keep
  logs somewhere persistent instead.
- **Template registry** — the merged extension → templates bindings (built-in
  plus your own overrides), read-only.

## Export for hosted serving

The **Deploy** button (see [Deploy to a hosted URL](../README.md#deploy-to-a-hosted-url)
in the README) exports and publishes a page for you. For scripting, the running
server also exposes a programmatic export (`POST /api/export`) that packs a page
and its `runPython`/`rawUrl` dependencies into a portable bundle a hosting layer
can serve — see [EXPORT.md](EXPORT.md) for the bundle format and rules.
