# Server API

> **Status — shipped.** This file owns the **HTTP surface** over the index and the
> **startup scan scheduler**. Implementing module:
> `fused_render/server/routers/index.py`. The engine behind it is `scan.md` (writes)
> and `query.md` (reads); the store is `index-store.md`.

## 1. Routes

Mutating routes carry the standard `X-Fused: 1` guard (a POST can be fired blind
cross-origin; a custom header forces a preflight that fails there). Reads are
unguarded like every other read endpoint and none of them can write.

| Route | Guard | Purpose |
|---|---|---|
| `POST /api/index/scan` `{root?, full?}` | X-Fused | start a detached scan; `{run_id, root}`. No `root` means the first configured root (§3). A non-directory or mount-backed root is a 400. |
| `POST /api/index/cancel` `{run_id}` | X-Fused | touch the run's cancel flag |
| `GET /api/index/status?run_id=&since=` | — | scan state (§2) |
| `GET /api/index/runs` | — | the 20 most recent runs with their folded state |
| `GET /api/index/stats?root=` | — | totals + per-extension breakdown (`query.md §2`) |
| `GET /api/index/lookup?q=&limit=&offset=&sort=` | — | path lookup (`query.md §3`) |
| `GET /api/index/search?root=&q=&limit=` | — | the explorer's in-folder corpus (`query.md §6`) |
| `GET /api/index/config` · `POST /api/index/config` | X-Fused on write | scan roots + ignore list (§3) |

Every route resolves its `IndexConfig` from disk **per request**. That is what
de-globalizing the engine bought: an ignore-list edit applies to the next scan with no
restart, and a test's redirected home is honoured by the same process that served the
previous test.

**There is no route that runs SQL against the index** — see `query.md §5`.

## 2. Status, and the first-boot window

`GET /api/index/status` answers flat, render-ready state: `running`, `phase`, `dirs`,
`files`, `reused`, `current`, `summary`, `cancelled`, `error`, plus `run_id`, `root`,
`indexed` (has the index ever been compacted) and `updated` (when).

**Without a `run_id` it reports the most recent run.** That is the case a page that just
loaded is actually in: it has no run id, but the startup scan may well be in flight, and
the UI needs to be able to say "building index… N files" rather than pretending nothing
is happening. With a `run_id` it additionally returns `events[since:]` and a `cursor`,
so a poller fetches only what it has not seen (`scan.md §3`).

**The first scan is not a blocking state.** A whole-home scan takes seconds, and during
it — and during every incremental rescan on later boots — the explorer's in-folder
search simply falls back to the live walk. "No index yet", "this root is not covered
yet" and "a scan is running" are the *same* condition to the search seam: fall back
silently, never surface an error (`query.md §6`).

## 3. Configuration

`<index>/config.json` holds the two user-editable knobs:

- **`roots`** — what the startup scheduler scans. Empty (the default) means **the
  user's home directory**. Home rather than the project root because a whole-home scan
  with the default ignore rules costs seconds, and an index covering one project
  answers almost none of the searches the explorer wants to ask. `start_dir` is
  deliberately NOT a fallback: an index that followed whichever folder the app was
  opened on would give different answers per window.
- **`ignore`** — the prune list (`scan-ignore.md`). `GET` also returns `defaults` so a
  UI can offer "restore defaults".

## 4. The startup scheduler

`create_app` registers one startup hook. It runs off the event loop and can never
delay serving: the scan itself is a detached worker, so the hook is a `Popen` per root.

1. **Reclaim** run directories beyond the newest `KEEP_RUNS` (`scan.md §2`).
2. For each root: skip it if it does not exist, or if a scan **started** within
   `SCAN_DEBOUNCE_S` (15 min). The debounce reads the start time, not the finish time,
   so a scan still running suppresses the next one just as firmly.
3. Otherwise start an incremental scan. Later boots are cheap by construction — the
   directory-mtime rule (`scan-incremental.md §2`) turns an unchanged tree into one
   stat per directory, and on macOS the FSEvents journal skips even that.

Nothing here raises: a bad root is logged and the remaining roots still run, and a
failure to read the config aborts the scheduling rather than the server.

**In tests** the hook is patched out wholesale (`tests/conftest.py`
`_no_startup_index_scan`) — a suite that ran it would spawn a worker over the
developer's real home. The scheduler's own tests call `run_startup_scan()` directly.

## Non-goals

- **Watching the filesystem** — there is no daemon. Freshness comes from the startup
  scan plus on-demand scans; a watcher is a later project.
- **Indexing remote mounts** — refused structurally (`scan-ignore.md §7`).
- **AI/SQL access to the index** — `query.md §5`.

## See also

- `scan.md` — the lifecycle §1 and §4 drive.
- `query.md` — what §1's read routes return.
- `scan-ignore.md` — the list §3 edits.
