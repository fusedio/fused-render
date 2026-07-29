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
  immediately — to the app and to every open built-in view, with no reload —
  and is remembered per browser profile (so a browser tab and the desktop
  window each keep their own choice). A few built-in views are exceptions: the
  document-like ones (maps, slides, PDF, docs) are always light, the media and
  geospatial ones are always dark, and Excel and Tableau keep their own in-view
  buttons. **Your own `.html` views are never touched** — their CSS stays
  entirely yours; see the authoring skill for how to follow the desktop
  preference if you want to.
- **Deploy to Fused account** — the opt-in toggle for the preview header's
  Deploy button.
- **Call log** — whether the app records the API calls your pages make, how
  much of each run's parameters it keeps, and how long records are kept. See
  [Call log](#call-log) below.
- **Template registry** — the merged extension → templates bindings (built-in
  plus your own overrides), read-only.
- **Execution engine** — switch `fused.runPython` between the built-in
  executor (fresh subprocess per call) and the fused engine (PEP 723 inline
  requirements in cached venvs). Applied to the next run, no restart; setting
  `FUSED_RENDER_ENGINE` pins the engine and locks the switch.

Execution engine sits last, since builtin suits almost everyone. The guided tour
is not on this page — it runs itself the first time you open the app.

The **server's own log** is not a preference, so it is not on that page. It
exists for debugging: when something goes wrong (an "Internal Server Error" in
the browser, or a misbehaving file-open) it has the traceback. Each run writes
its own file in your system temp directory — the CLI prints the path on
startup, and the packaged app reveals it from **menu bar → Open app logs**. It's
disposable: it rotates so it can't grow without bound, and living in temp means
the OS reclaims it. Set `FUSED_RENDER_LOG_DIR` to keep logs somewhere
persistent instead. (Not to be confused with the **call log** below, which is
durable, has settings, and lives in `~/.fused-render/logs/`.)

## AI calls (`fused.ai`)

Pages can ask an AI model with `fused.ai(prompt, opts?)`. The server runs the
call through the **`claude` (Claude Code) CLI** on your machine — your existing
Claude Code login is the credential; there is no proxy or API key to configure.
The binary is found on `PATH`; set `FUSED_RENDER_CLAUDE_BIN` to point at a
specific binary per process. If the CLI isn't installed, calls reject with an
`ai_unavailable` error saying what to install or set.
`fused.ai` is local-only: exported/hosted pages can't use it (see
[EXPORT.md](EXPORT.md)). A working example ships in `examples_seed/ai_demo/`.

## Export for hosted serving

The **Deploy** button (see [Deploy to a hosted URL](../README.md#deploy-to-a-hosted-url)
in the README) exports and publishes a page for you. For scripting, the running
server also exposes a programmatic export (`POST /api/export`) that packs a page
and its `runPython`/`rawUrl` dependencies into a portable bundle a hosting layer
can serve — see [EXPORT.md](EXPORT.md) for the bundle format and rules.

## Call log

The app records every API call your pages make — each `fused.runPython`,
`readFile`, `stat`, `writeFile` and `ai` — with its duration, result size, the
`print()` output, and any traceback. It answers the questions a page can't
otherwise: which of your `.py` files is slow, whether a run errored while you
weren't looking, and whether the page is quietly re-running Python far more
often than you thought.

**Where to see it.** Two surfaces today — a dedicated in-app view is coming in a
later release. Open any `.calls.jsonl` file from the store and it renders in
**Log studio**: records carry a conventional log `level`, so its level facets,
query filter and volume histogram work on them, and expanding a row shows the
record as fields rather than a wall of JSON. For anything scripted, or for an
agent checking its own work, use the CLI below.

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
explorer: one directory per app, and clicking any `.calls.jsonl` opens it in
**Log studio**.

Records carry a normal log `level` (INFO for a healthy call, ERROR for a
failure, DEBUG for a superseded one), which is what makes Log studio a real
viewer for them — level facets, filtering, and its volume-by-level histogram.
Expanding a row there shows the record as **fields** rather than one long line of
JSON: nested objects indented under their key, a traceback or output tail kept as
a block so its newlines survive, and any value clickable to filter the log by it.
**Raw JSON** switches that row back to the exact text on disk. A plain-text log
line is untouched — it still shows as itself. **Show context** fetches the lines
immediately before and after that one straight from the file, which is how you
see a line's neighbours when a query or a level filter has hidden them (a
traceback split across lines, or the request that preceded a failure). The level
facets list only the levels the file actually contains, so a log that only writes
INFO doesn't offer six filters that match nothing. Its Tail button switches
auto-reload off while engaged, so watching a live file polls instead of
reloading, and a poll updates only the lines that changed — a row you have
expanded stays expanded and the scroll stays put. Log studio follows
**Preferences → Appearance** like the rest of the app: System, Light or Dark,
applied as you change it, with no button of its own.

Opening a log file in any view is safe. Nothing watches a log file for changes,
so a view of one never reloads itself — which matters because reading a log *is*
a recorded call, and a watcher would reload, re-read and append forever. The
reads themselves are kept — what a viewer costs on a big log is worth seeing.

**Where it lives.** `~/.fused-render/logs/<app>/` as newline-delimited JSON —
one directory per app (named for the page's folder, e.g. `sine-3f9a1c2b8d7e6f50`,
with `index.json` at the root mapping names back to folders), one file per day
per server process inside it, rolled to a new part every 32 MB. (This is not
the app's own log: that one is disposable diagnostic output and lives in the
system temp dir, reached from **menu bar → Open app logs**. `~/.fused-render/logs/`
holds only the durable call records described here.) Records are capped
(a long traceback or a big parameter is truncated, and marked as such),
rate-limited per page, and pruned after 14 days or once the store passes
200 MB — whichever comes first, with the largest app's oldest files trimmed
first so a chatty app cannot evict a quiet one's history. Today's files are
never pruned, since a running server is appending to them. Renaming or moving
an app's folder starts a fresh directory; the old one ages out on its own. The
files are ordinary JSONL, so `tail`, `jq` (one app's history is one
directory), and the built-in `duckdb` view all work on them — a whole-store
query globs `logs/*/*.calls.jsonl`.

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
