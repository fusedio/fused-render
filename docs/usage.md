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

- **Appearance** — System (follows your desktop), Light or Dark. Applies
  immediately, and is remembered per browser profile (so a browser tab and the
  desktop window each keep their own choice).
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
- **Call log** — whether the app records the API calls your pages make, how
  much of each run's parameters it keeps, and how long records are kept. See
  [Call log](#call-log) below.
- **Template registry** — the merged extension → templates bindings (built-in
  plus your own overrides), read-only.

## Export for hosted serving

The **Deploy** button (see [Deploy to a hosted URL](../README.md#deploy-to-a-hosted-url)
in the README) exports and publishes a page for you. For scripting, the running
server also exposes a programmatic export (`POST /api/export`) that packs a page
and its `runPython`/`rawUrl` dependencies into a portable bundle a hosting layer
can serve — see [EXPORT.md](EXPORT.md) for the bundle format and rules.

## Call log

The app records every API call your pages make — each `fused.runPython`,
`readFile`, `stat` and `writeFile` — with its duration, result size, the
`print()` output, and any traceback. It answers the questions a page can't
otherwise: which of your `.py` files is slow, whether a run errored while you
weren't looking, and whether the page is quietly re-running Python far more
often than you thought.

**Where to see it.** Open a page (or a `.py`) that has recorded calls and pick
the **Calls** mode from the view switcher — charts of call volume, duration
(p50/p95 over the individual calls), and response size, a per-target table, and
the recent calls with each one's full record a click away. The mode only appears
for files that actually have records, so it never clutters a file you haven't
run.

**From a terminal:**

```
fused-render calls                          # the last hour, digested
fused-render calls --page ~/views/sine.html # one page
fused-render calls --failed --since 24h     # only what broke
fused-render calls --json                   # machine-readable
fused-render calls --follow                 # wait for the next calls, then print
```

The digest is a per-target rollup plus any failures in full — including **page
errors**, the page's own JavaScript failing, which is what you have when a page
made *no* calls at all. `--since-cursor <id>` (the `cursor` printed at the end)
returns only what is new since a previous run, which is handy in a loop or for
an agent checking its own work.

**Superseded calls.** Dragging a slider fires a call per tick, and each one
cancels the last — so most of a drag is work nobody waited for. Those calls are
recorded and counted as **stale**, but deliberately left out of the duration
percentiles: including them would report a dozen slow calls for what you
experienced as one. A large stale count next to a small ok count is the sign a
page is re-running Python far more than it needs to (a slider with no debounce,
or an `onChange` handler that retriggers itself).

**Getting to it from Preferences.** **Browse call logs** opens the store in the
explorer, which is how you reach the viewer: click any `.calls.jsonl` and it
renders in the same Calls view.

Records also carry a normal log `level` (INFO for a healthy call, ERROR for a
failure, DEBUG for a superseded one), so the generic **Log studio** view works on
these files too — level facets, filtering, and its volume-by-level histogram.
Its Tail button and the Calls view's Follow both switch auto-reload off while
engaged, so watching a live file polls instead of reloading.

Opening a log file in any view is safe. Nothing watches a log file for changes,
so a view of one never reloads itself — which matters because reading a log *is*
a recorded call, and a watcher would reload, re-read and append forever. The
reads themselves are kept (what a viewer costs on a big log is worth seeing);
the Calls view's own polling is the one thing excluded, since it would otherwise
inflate the numbers you are reading.

**Where it lives.** `~/.fused-render/logs/` as newline-delimited JSON, one file
per day per server process, rolled to a new part every 32 MB. (This is not the
app's own log: that one is disposable diagnostic output and lives in the system
temp dir — **Preferences → Logs** points at it. `~/.fused-render/logs/` holds
only the durable call records described here.) Records are capped
(a long traceback or a big parameter is truncated, and marked as such),
rate-limited per page, and pruned after 14 days or once the directory passes
200 MB — whichever comes first. Today's files are never pruned, since a running
server is appending to them. The files are ordinary JSONL, so `tail`, `jq`, and
the built-in `duckdb` view all work on them.

**A note on parameters.** A run's parameters are recorded by default: they are
usually the whole reproduction, and they are already visible in the URL. If a
page passes something sensitive as a parameter, switch **Preferences → Call log
→ Parameters** to *names only* (or off). `FUSED_RENDER_CALLS=0` turns capture
off entirely for a run, and `FUSED_RENDER_CALLS_RETENTION_DAYS` overrides the
retention window.

**What it does not see:** requests a page makes with its own `fetch()` to a
third party, the map templates' tile daemons (they serve the browser directly),
and `fused.rawUrl()` used as an `<img>`/`<embed>` source — a plain URL has
nowhere to carry the attribution header. It is a diagnostic for your own pages,
not an audit trail.
