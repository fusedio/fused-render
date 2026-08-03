# Call Log — observability for a fused-render app

**Status: phase 1 implemented, MINUS the in-app view** (D136, SPEC §31) —
`fused_render/calls.py`, the middleware write point, the runtime's attribution
headers and `window.onerror` hook, the `fused-render calls` CLI, Preferences
controls, `tests/test_calls.py`. The `calls` view template was written and then
pulled from the change: it failed in practice (a worker could not import the
package, PY-6a) and a viewer is a big enough surface to deserve its own review
rather than one wedged into the change that defines the record. Reading works
without it — the CLI runs in-process with no worker at all, and `.calls.jsonl`
is bound to **log_studio**, which now renders each record as fields. This file stays the design record:
the rationale, the alternatives that were rejected, and the phases still open.

Two changes of scope from the plan: the CLI moved **into** phase 1 (it is the
agent's only read surface, §9.2) and `page-error` records were added to it
(§9.2a — the runtime had no `window.onerror` hook, so the most informative record
was uncapturable). **One §6 decision was wrong and has been reversed** — 6.2's
deferral of client-side supersession reporting, which turned out to be the whole
of CL-5 rather than a refinement of it (D137; the defects fifteen rounds of review
found are in §9.5, with D138–D150 the follow-up rounds). Deviations from the
design as written are marked **[shipped]** inline.
Phases 2 and 3 are not started. **Deferred with the view:** everything in §5.1
below (the template, its gate, its reader ops as a *view* surface) and, when it
returns, the question of cross-app scoping — the store is partitioned per app
(§4.7) and a view is opened on a page, so "this application's calls" is the
axis it is organised around; a cross-app surface is a different one. `query()`
keeps `scope: mine|templates` either way; only a control is missing.

> **The ask.** "Give me a log of the API calls my app makes. Me or my agent can
> see what the call was, and its stdout/stderr/error/result size. Give me graphs
> of response time, call count over time, response size."

An app here is the ordinary fused-render unit: an `.html` page plus its sibling
`.py` data files. Its "API calls" are the `window.fused.*` calls the page makes
through the injected runtime — overwhelmingly `runPython`, plus the file IO
helpers. Today those calls are invisible: a `runPython` failure paints the red
overlay (D17) and the page's `print()` lands in the browser console prefixed
`[python]`, and that is the whole story. Nothing accumulates, nothing is
queryable, nothing is comparable across runs. "Is this page slow, or is this
one `.py` slow, and was it always?" has no answer.

This document proposes the missing layer: **one durable, bounded, structured
record per call, written by the server, read back by an ordinary view
template.**

---

## 1. Why this is cheap here (the seams already exist)

Nearly every piece of this feature has a precedent in the tree already. The
work is mostly joining things that exist, not inventing a subsystem.

| Piece needed | Already in the tree |
|---|---|
| A single chokepoint every app call flows through | `static/runtime.js` — `runPython`/`rawUrl`/`stat`/`readFile`/`writeFile` are the whole app-facing API (SPEC RH-*). One file, five functions. |
| Per-request timing + status | `server.py` `no_cache_and_log` middleware already logs `<method> <path> -> <status> (<ms>)` per request (SV-3, D68), skipping the static mounts. |
| Rich per-run detail | `/api/run` (`server.py:3729`) already has `stdout`, `error{type,message,traceback}`, `resolved_py`, `params`; the fused engine additionally returns `stderr` + `duration_ms` (`engine.py:384`). |
| A structured record contract for exactly this data | The fused repo's `spec/serve/error-reporting.md` record: `version`, `occurred_at`, `entrypoint`, `kind`, `duration_ms`, `params`, `stdout_tail`, `stderr_tail`, `error`, `truncated` — with named caps and a fail-open rule. **Shipped**, on both serve backends. |
| A viewer for a high-volume time-series log | `templates/log_studio/` — 1.9k-line template + 817-line reader: level facets, query filter, time-bounded paging, a canvas **histogram over time**, pattern mining, detail pane, context expansion. Bound to `.log` today. |
| A per-file history surface | `templates/history/` (D96, SPEC §24) — renders the `<file>.json` sidecar per-key with an inline schema validator, unknown keys tolerated (HV-5), rows deep-link back into the target view (HV-7). |
| A way to show a mode only when it is relevant | `condition.py` gates (CT-12) — a template folder's gate marks the mode `conditional`, the shell resolves it in the background via `/api/fs/conditions` and joins it to the switcher only on a true verdict. |
| Fast, warm, first-party readers | `INPROCESS_HELPERS` (`executor.py:66`, D72) — trusted first-party readers run in the server process instead of paying ~700 ms subprocess spawn per call. |
| Durable, atomic user-data storage | `shell/storage.py` — `home_dir()` (`FUSED_RENDER_HOME`, branch-aware) + atomic `read_json`/`write_json`, the base every shell resource sits on. |
| An owner-only diagnostics viewer in the shell | `components/DeploymentErrors.tsx` + `/api/deploy/errors` — newest-first list, click a row to fetch the full record, copyable CLI equivalent. The exact UI shape this feature wants. |

The one genuinely new thing is a **write path with volume**. Every existing
persisted store in the repo (bookmarks, recents, prefs, the sidecar) is a
whole-file read-merge-write of a small JSON document. A call log is
append-only and can produce hundreds of records during one slider drag. That
difference drives most of §4.

---

## 2. What counts as a call

Not everything is worth a record. The taxonomy, with the value of logging each:

| `route` | Runtime entry | Log it? | Why |
|---|---|---|---|
| `POST /api/run` | `fused.runPython` | **yes, full detail** | The interesting one: params in, duration, result size, stdout, stderr, traceback. This *is* the feature. |
| `POST /api/fs/write` | `fused.writeFile` | **yes** | Mutating, low volume, and "what did my app overwrite" is a real question. Log path + bytes + conflict outcome, never content. |
| `POST /api/fs/upload` | `fused.uploadFile` | **yes** | The binary write (a pasted or dropped image/video, SPEC MD-23). Same rule as `/api/fs/write` — path + byte count, never the payload — so pasted media is not the one mutation that leaves no trace. |
| `GET /api/fs/stat` | `fused.stat` | yes, thin | Cheap, but a page that stats in a loop is a real bug and only a log shows it. |
| `GET /api/fs/raw` | `fused.readFile`, `fused.rawUrl` | yes, thin | High volume (every `<img>`, every ranged read). Log status/bytes/range — and see the sampling note in §6.3. |
| `GET /api/fs/list`, `/walk`, `/conditions` | shell, not the app | no | Shell chrome, not the app's own calls. Attribution (§4.3) excludes them naturally. |
| tile-daemon GETs (`geotiff`, `map`, `zarr_aoi`, `pyramid`) | template `fetch` direct to a loopback daemon | **not in v1** | They bypass the server entirely by design (D122 — that is the point of the daemons). Logging them means teaching each daemon to report, or proxying them. Real value ("why is panning slow"), real cost. §9.6. |
| a page's own bare `fetch()` to a third party | — | no | Out of scope; the runtime never sees it. Note it in the docs so the log isn't mistaken for complete. |
| deployed (`_run` / `_asset` routes) | hosted runtime | **phase 3** | Requires serve-plane work in the `fused` repo. §7.3. |

**The one that will surprise people.** `runPython` has default latest-wins
cancellation (D114/RH-9, `runtime.js:347`): a new call for the same `.py`
aborts the prior in-flight one, and the superseded promise **never settles**.
So a slider scrub of 40 ticks issues 40 calls of which 1 completes. An honest
call log must mark superseded/aborted calls as such, or every chart lies —
"40 calls, p95 3.2s" when the user made one meaningful request. This is the
single most important field in the record and it is only knowable *because*
this codebase works this way:

- Client side: `runtime.js` knows `_supersededByKey` exactly.
- Server side: the socket closes; the endpoint can detect disconnect.

Recommendation: record `outcome: "ok" | "error" | "superseded" | "aborted" |
"disconnected"`, and have every chart default to `outcome in (ok, error)` with
superseded calls shown as a separate, dimmer series. The dropped work is worth
seeing — it is exactly the "my page is hammering Python" signal — but it must
never be summed into latency percentiles.

---

## 3. The record

Deliberately **the serve plane's error record, widened to successes**. Same
field names, same units, same caps, additive `version`. That is not tidiness
for its own sake: it means the local log and a deployed app's records render in
the same viewer with one formatter, and a page debugged locally produces
comparable numbers when deployed.

```jsonc
{
  "version": 1,
  "call_id": "01JX...",          // sortable id; the join key for detail lookups
  "occurred_at": "2026-07-24T18:42:07.921Z",
  "page": "/Users/you/views/sine.html",   // the APP — see attribution, §4.3
  "target_file": null,           // the iframe's `_file`, when the page is a template (§4.6)
  "first_party": false,          // page is a shipped/user template, not the user's own app (§4.6)
  "route": "/api/run",
  "http_method": "POST",
  "status": 200,

  "entrypoint": "/Users/you/views/sine.py",  // resolved_py for /api/run; the target path otherwise
  "entrypoint_name": "sine.py",              // display convenience
  "engine": "builtin",                       // builtin | fused  (prefs, SPEC §20)

  "outcome": "ok",               // ok | error | superseded | aborted | disconnected
  "server_ms": 812,              // measured at the server, both engines
  "client_ms": 838,              // optional, from runtime.js — includes queueing/parse
  "queued_ms": null,             // reserved: threadpool wait, if we ever measure it

  "params": {"freq": "2.4"},     // capped 2 KiB; params_truncated on cut
  "result_bytes": 14203,         // serialized size of the JSON response body
  "result_kind": "list",         // list | dict | str | number | bool | null | base64
  "result_rows": 80,             // len() when list, or result.rows/total_rows when present
  "stdout_tail": "...",          // capped 4 KiB, TAIL not head
  "stderr_tail": "...",          // capped 4 KiB (fused engine only today — see §6.5)
  "error": null,                 // {type, message, traceback} — traceback capped 16 KiB
  "err_id": null,                // set on a 500, joins the existing unhandled-error log line

  "truncated": false
}
```

Bounds copied verbatim from `error-reporting.md` §1.2: `error` ≤ 16 KiB,
`stdout_tail`/`stderr_tail` ≤ 4 KiB each, `params` ≤ 2 KiB serialized, whole
record ≤ ~32 KiB, truncation marked in the record and never grown.

**Never stored:** file contents (only `result_bytes` for a raw read), the
`X-Fused` header or any request headers, anything from a `writeFile` body
beyond its byte count.

**`params` are stored** — capped — and that is a deliberate, named trade-off
inherited from the serve spec: params are the inputs the author's own code
already received and are usually the whole repro. Locally this is a
single-user tool on the user's own machine (D3), so the exposure is the same as
the params already sitting in the URL bar. §6.4 offers a redaction knob anyway,
because a page whose param is an API key exists.

---

## 4. Storage and the write path

### 4.1 Where it lives

`~/.fused-render/logs/YYYY-MM-DD.jsonl` — one JSON object per line, append
only, daily files, under the branch-aware `storage.home_dir()`.

Why not the alternatives:

- **Not the sidecar** (`<page>.html.json`, D82–D84). Tempting — it is
  per-page, it travels with the file, `history/` already renders it. But every
  writer there does a whole-file read-merge-write (`annotate.py:_load_sidecar`
  → `_save_sidecar`). At call volume that is O(n²) rewrites and a lost-update
  race between concurrent runs. The sidecar is right for *low-frequency*
  history (sessions, comments, bookmarks); it is the wrong shape for a firehose.
- **Not the app log** (`logs.py`, temp, per-pid rotating text). That file is
  disposable by design (D68 chose temp precisely because nothing prunes it) and
  it is unparseable prose. A call log must survive a reboot to graph
  "last week vs this week."
- **Not SQLite.** It would query beautifully and it is stdlib. But it adds a
  concurrency story (two `fused-render` instances on different ports, the
  supervisor, WAL files) to a repo whose entire persistence posture is
  "atomic whole-file writes, last write wins, single local user." JSONL keeps
  that posture and stays greppable by an agent with no tooling. Revisit only if
  §9.2 (cross-app rollups over months) becomes the primary use.

JSONL also buys two things free: `.jsonl` is already bound to the `duckdb`
template in the registry, so `SELECT ... FROM calls` works today with zero new
code; and `duckdb` reads a directory glob, so "all of last week" is one query.

**Retention is explicit, mirroring the serve plane's 14-day lifecycle rule:**
on startup and once a day, delete `calls/*.jsonl` older than
`FUSED_RENDER_CALLS_RETENTION_DAYS` (default 14) and, as a hard backstop, trim
the oldest files while the directory exceeds a size cap (default 200 MB). D68's
"nothing prunes the directory" objection is answered by code, not by choosing
temp storage.

### 4.2 Who writes

`fused_render/calls.py` — a new module, an `APIRouter` for its own read
endpoints, `record(...)` for the write. It must **not** import `server.py`
(`server` imports it; same acyclic rule as `shell/bookmarks.py` and
`deploy.py`, and it duplicates the local `X-Fused` guard the same way).

Writes go through a single **background writer thread** with a bounded queue
(`queue.Queue(maxsize=...)`):

- The request path does `queue.put_nowait(record)` and returns. A full queue
  **drops the record and counts the drop** — never blocks a run, never raises.
  This is the `fail-open` rule from `error-reporting.md` restated: logging must
  never fail, or meaningfully delay, the thing it observes.
- The writer opens the day's file in append mode and `write()`s one line per
  record, flushing on a small batch or a short timer. Append-of-a-single-line
  is atomic enough for a single writer; two concurrent server processes each
  hold their own fd, so a per-pid suffix (`YYYY-MM-DD-<pid>.jsonl`) removes the
  interleaving question entirely — the same reasoning `logs.py:log_path()`
  already applies, and the reader globs the day anyway.
- A **rate cap** per page (say 600 records/min, tunable) drops the excess and
  emits one summary record noting the drop, so a runaway render loop cannot
  fill the disk. Size caps × rate caps × retention bound the footprint
  structurally.

### 4.3 Attribution — which app made the call

This is the one real design problem. The middleware sees the route but not the
app.

`runPython` already sends the calling page (`html` in the body, read from the
iframe's own `path` query param at `runtime.js:378`). The IO helpers do not —
`stat`/`readFile`/`writeFile` send only the target path.

**Proposal:** `runtime.js` adds one header to every call it issues:

```
X-Fused-Page: <the iframe's own `path` query param>
```

One line at each of the five call sites (or one shared `_fetch` wrapper —
worth the small refactor: it is also where `client_ms` and the
`X-Fused-Call` correlation id go). Consequences, all good:

- The server learns the app for free, in the middleware, for every route.
- Shell-issued calls (`Listing`'s `/api/fs/list`, the conditions probe) carry
  no header and are therefore excluded from the app log by construction —
  exactly the filter §2 wanted.
- `X-Fused-Page` is a custom header, so like `X-Fused` it forces a CORS
  preflight; it changes no security posture (D3/D36 stand — this is not auth).
- Both path headers are **percent-encoded** (`calls.begin` decodes them): a
  header value is ISO-8859-1 only, and `fetch` throws rather than sending one
  that is not.
- In panel/tab mode each pane's iframe has its own `path`, so per-pane
  attribution is automatic.

The client also mints `X-Fused-Call: <call_id>` so the client-side outcome
(superseded, aborted, `client_ms`) can be **reconciled** onto the server's
record instead of producing a second, duplicate one. That needs a tiny
`POST /api/calls/outcome` batch endpoint (fire-and-forget, coalesced,
`navigator.sendBeacon` on `pagehide` — `runtime.js:311` already hooks
`pagehide`). If that reconciliation is judged not worth the complexity, the
server can infer `disconnected` from the closed socket and lose only the
ok-but-superseded distinction; see §6.2.

### 4.4 What each site contributes

| Site | Adds |
|---|---|
| `server.py` middleware (`no_cache_and_log`) | The base record for every `X-Fused-Page`-bearing request: route, method, status, `server_ms`, `page`. One `calls.record()` call beside the existing `logger.info`. |
| `server.py` `/api/run` | Enriches its record: `entrypoint` (`resolved_py`), `params`, `engine`, `outcome`, `stdout_tail`, `stderr_tail`, `error`, `result_bytes`/`result_kind`/`result_rows`. Sets a request-scoped record the middleware then flushes, so there is exactly one record per request. |
| `server.py` `/api/fs/write` | `bytes_written`, conflict/readonly outcome. |
| `executor.py` (builtin path) | Should start returning `duration_ms` and a `stderr` tail so the two engines produce comparable records (§6.5). Small, independently useful change. |
| `runtime.js` | `X-Fused-Page`, `X-Fused-Call`, `client_ms`, terminal client outcome. |

### 4.5 Where in the call chain the write happens

Six levels can see a call. Each knows a different subset, and no single one
knows everything:

```
1  page JS            fused.runPython(...)        page identity, client latency, SUPERSESSION
2  runtime.js         shared _fetch wrapper       ditto, uniformly for all five helpers
3  ASGI middleware    no_cache_and_log            route, method, status, wall time, headers
4  route handler      /api/run, /api/fs/write     params, resolved_py, stdout/stderr, traceback, result
5  engine/executor    engine.py, executor.py      duration_ms, engine identity — no page, no HTTP
6  worker process     _child.py, user main()      only what user code itself did
```

**The record is written at level 3, enriched from level 4, annotated from
level 2.** Concretely:

- The **middleware** creates a request-scoped record on the way in
  (`request.state.call`), and on the way out fills status + `server_ms` and
  makes the **single** `calls.record()` call. It is the only caller of
  `record()` — that invariant is what guarantees one record per request and one
  place where fail-open has to be correct.
- **Route handlers enrich** that same dict in place; they never write. A handler
  that adds nothing still produces a valid thin record, so new endpoints get
  logged by default rather than by remembering to instrument them.
- `calls.record()` itself only does `queue.put_nowait()`. The **background
  writer thread** (level "outside the request") does the append. Nothing on the
  request path touches the filesystem.
- **`runtime.js` annotates but does not report** (in phase 1): it contributes
  `X-Fused-Page` and `X-Fused-Call` as headers on the way in, so attribution and
  the correlation id ride the request the server is already logging. The
  client-side terminal outcome (`superseded`, `client_ms`) is a separate,
  coalesced POST — deferred to phase 2 (§6.2).

**Why not deeper.** Level 5 cannot see the page, the route, or the HTTP status,
and it is shared by both engines and by the shell's own uses — it would need
the page threaded down to it purely for logging. Level 6 is the user's own
subprocess: it cannot log the case you most want logged (the run that died or
timed out), and making user code responsible for its own observability is
backwards.

**Why not shallower.** Level 2 alone would be a client-side log: it misses
runs whose page navigated away mid-call, it cannot see stdout/stderr for
anything but `/api/run`, and it puts the durable record behind the very thing
being debugged. It contributes what only it knows, and nothing more.

Three consequences worth stating plainly, since they follow from choosing
level 3:

- **`server_ms` is time-to-response-object, not time-to-last-byte.** The
  middleware resumes when `call_next` returns a `Response`; a `FileResponse` or
  the pooled mount proxy has not streamed its body yet. For `/api/run` (a
  `JSONResponse`, fully materialised) the two are the same. For `/api/fs/raw`
  they are not, and `result_bytes` there comes from `Content-Length`, not from
  measuring the stream. The existing access line (SV-3) already has exactly
  this property, so this inherits a known limitation rather than adding one.
- **500s are recorded from the `except` branch.** `@app.exception_handler(Exception)`
  runs in `ServerErrorMiddleware`, *outside* user middleware, so an unhandled
  exception escapes `call_next`. The existing middleware already duplicates its
  log line there before re-raising; the record write joins it, and `err_id`
  must be minted somewhere both can see it (`request.state` again) so the
  record and the response body carry the same id.
- **`request.state` is a new pattern here.** The repo uses `app.state` today
  (the pooled httpx client) but never `request.state`. It is stock Starlette
  and the right tool for request-scoped data; worth naming because it is new.

### 4.6 Template readers are apps too

A consequence of attributing by the iframe's own path: when you preview a
parquet, the `duckdb` template's page calls `runPython('./reader.py')` through
the same runtime and the same `/api/run`. Those are real calls and they get
real records, attributed to `page = <package>/templates/duckdb/template.html`.

That is correct — "how slow is the duckdb reader on this file" is a question
worth answering, and §10.3 depends on it — but it means "my app's calls" needs
a deliberate filter, not an accident. Two additions handle it:

- The record carries **`target_file`** (the iframe's `_file` param) alongside
  `page`. For a user's own page they are unrelated; for a template, `_file` is
  the identity that matters — the record says "duckdb reader, on *this*
  parquet."
- A **`first_party`** boolean (page path is under the installed package's
  `templates/` dir, or under `~/.fused-render/templates/`) so the default view
  can show *your* pages and let template calls be one toggle away.

### 4.7 Partitioning the store by app **[shipped, second PR]**

The flat directory of §4.1 had three problems, in increasing severity: a quiet
app's reads paid for a chatty app's volume (every walk tails every file); the
size cap let one app evict another's history (trimming was oldest-first across
the whole store); and the gate produced **false negatives** — its bounded probe
(`MAX_FILES` newest files) filled up with the busy app's files, so a quiet
page's Calls mode silently never appeared. The third is the same defect class
as D140–D144 (a verification tool reporting no activity when there was
activity), with the flat layout as a fourth cause.

**The partition is the page's containing directory.** An app is an `.html`
plus its sibling `.py` files, so the folder is the unit that makes both the
page's and the data file's lookups land in the same place — which the `.py`
case requires, because a `.py` is never a record's `page` (D139/D147) and any
per-page scheme would strand it.

```
~/.fused-render/logs/
  index.json                          # advisory: partition dir -> app dir
  sine-3f9a1c2b8d7e6f50/              # <slug ≤24>-<blake2b-16hex of the app dir>
    2026-07-27-41234-001.calls.jsonl  # file naming unchanged (CL-7)
  _unattributed/                      # records with no resolvable page
```

Decisions, and what was rejected:

- **One resolver owns the name.** `partition_name(app_dir)` hashes
  `canonical_fs_path(normcase(realpath(app_dir)))` — symlinks resolved, case
  folded where the platform folds it, one separator form (D147's lesson,
  applied before the bug this time). The gate duplicates it (stdlib-only
  standalone copy, like `_store_dir`) and a test pins the two across a table of
  paths. The slug is for the human running `jq`; the hash is the identity.
  `realpath` costs a syscall chain on the hot path, so it sits behind a small
  LRU — a symlink retargeted mid-process keeps its old partition until restart,
  accepted and documented.
- **Cursors stay bare `call_id`s, and D140–D146 semantics carry over
  verbatim.** The tempting design was a composite `<time>:<id>` cursor so each
  partition could stop independently — rejected as solving a problem the read
  path does not have. The walk stays **merged**: `store_files()` collects
  every partition's files and `_day_groups` merges them by append time exactly
  as before, so the identity stop, the shown-id guarantee, `cursor_missing`,
  and the scan budget all behave identically. Partitioning changes where files
  LIVE, never how the walk works.
- **`query()` does NOT narrow to a partition — implementation proved it must
  not.** The tempting second win was `query(page=X)` reading only
  `partition(dirname(X))`. The existing filter test failed immediately, and it
  was right: "this file" is a THREE-role match (`page` or `target_file` or
  `entrypoint`, D139/CL-16), and a template rendering your file records under
  the *template's* partition with `target_file` pointing at yours — the exact
  activity the Calls view on a data file exists to show. Any partition-narrowed
  walk silently loses it, and no candidate-set scheme closes the hole (the
  test's "template" page is not even under a templates dir). So the read path
  stays global-merged; its cost was already bounded by the window file-skip and
  the cursor's identity stop (D140), which is what made this cheap to give up.
  The gate is different: its probe was always a bounded *heuristic* with misses,
  so partition-first-then-global-fallback strictly improves it — the fallback
  IS the flat behaviour. The known blind spot stays only there: a `.py` borrowed
  by an app in another folder logs under the borrower, and the borrowed file's
  gate finds it only through the fallback window, as before.
- **The size cap trims the largest partition first.** Age-based deletion stays
  per-file; while the store exceeds the global cap, the oldest non-today file
  of the currently-largest partition goes. This is what stops the chatty app
  evicting the quiet one, and the chatty app's own tail is the least
  informative data in the store. Rejected: per-partition quotas (`cap / N`) —
  they waste the cap in the common case of one active app.
- **`sweep()` reaps.** A partition whose files have all aged out is an empty
  directory forever otherwise; the sweep removes empty partition dirs and
  their `index.json` entries. `index.json` itself is advisory and rebuildable —
  written only when a partition is *created* (never per record: a whole-file
  read-merge-write on the hot path is what §4.1 rejected the sidecar for), and
  a lost or corrupt index costs the slug lookup, nothing else.
- **Renames truncate.** Moving an app's folder changes its partition; the old
  history sits under the old name until retention reaps it. Accepted by the
  owner over a rename-chain in the index — the chain is bookkeeping that can
  drift, for an event (renaming a live app mid-investigation) that is rare.
- **No migration.** A flat store cannot be partitioned by moving files — a day
  file interleaves every app's records — and the call log has never shipped,
  so the only flat stores are development ones. The reader ignores any stray
  root-level `*.calls.jsonl`.

What deliberately does **not** change: the record shape, the `.calls.jsonl`
suffix (the registry binding, CT-3), the per-pid-per-day file naming, the
bare-id cursor contract, and the CLI surface. `duckdb` whole-store queries now
need a two-level glob (`logs/*/*.calls.jsonl`), noted in usage.md.

---

## 5. Reading it back

### 5.1 The primary surface: a `calls` view template

`fused_render/templates/calls/` — `template.html` + `reader.py` +
`condition.py` + `icon.svg`. Built as a **template, not shell code**, following
the containment posture the repo has held since D78/HV-1: all the logic lives
in the template, the shell learns nothing, and a user can fork it into
`~/.fused-render/templates/calls/` and change every chart.

Registry bindings (`templates/registry.json`):

- append `"calls"` to `".html"` and `".py"` — so a page and a data file each
  offer their own call history as a mode;
- bind the store itself: `".calls.jsonl": ["calls", "log_studio", "code"]`, so
  opening a raw log file lands in the same viewer.

`condition.py` (CT-12) gates the mode on **"this file appears in the log"** —
a cheap tail-scan of today's file with an early exit. A page nobody has run
does not grow a dead mode; the moment it has calls, the mode joins the switcher
in the background without a reload. This is precisely what CT-12 was built for.

`reader.py` follows `log_studio/reader.py`'s op shape — one helper, several
ops, everything paged and time-bounded so no op ever loads the whole log:

| op | Returns |
|---|---|
| `overview` | file span, total records, per-outcome counts, distinct entrypoints, first/last timestamp |
| `page` | a page of records, newest-first, filtered by `page`/`entrypoint`/`route`/`outcome`/time window/text query |
| `series` | **pre-bucketed** series for the charts: `{bucket_ms, points: [{t, count_ok, count_err, count_superseded, p50, p95, max, bytes_sum}]}`. Bucketing server-side is the whole reason the charts stay fast — the template never sees 100k records. |
| `targets` | the per-`.py` rollup: count, p50/p95/max, error rate, bytes, last-seen — the "which call is slow" table |
| `detail` | one full record by `call_id` |
| `tail` | records after a cursor, for live-follow |

Put `calls/reader.py` on the **`INPROCESS_HELPERS` allowlist** (D72). It
qualifies exactly as the existing members do — first-party, trusted, bounded
reads, never imports or executes user code — and it removes the ~700 ms
subprocess spawn from every poll, which is what makes live-tail viable at all.

### 5.2 The charts

Hand-rolled `<canvas>`, no dependency, no build step — the `log_studio`
histogram (`template.html:919`) is the working precedent and the style
constraint (ARCHITECTURE §10: template JS has no dependencies).

Four charts answer the four questions actually asked:

1. **Calls over time** — stacked bars per bucket: ok / error / superseded.
   Reveals render loops and error bursts. Brushing a range filters the table.
2. **Duration over time** — p50 and p95 lines with a faint per-call scatter
   behind them. The scatter matters: an average hides the one 12-second cold
   run that made the user think the page was broken.
3. **Response size over time** — `result_bytes`, plus a `result_rows` axis when
   present. Catches "my reader stopped paging and now returns 400k rows."
4. **Per-target table** — sortable: count, p50, p95, error rate, total bytes.
   The single highest-value view, and the one a human reads first.

Plus a **live tail** strip: newest calls streaming in as the page next to it
re-runs, each row expandable to the full record (stdout, stderr, traceback,
params) — the `DeploymentErrors.tsx` interaction, applied locally.

### 5.3 Second surface: the history view already knows how to do this

`history/` (D96) is the per-file "everything this file has accumulated" view.
Give it a **Calls** section: an inline schema entry, a sparkline plus
"142 calls, 3 errors, p95 1.2s, last run 4 minutes ago", and a row that
deep-links into `_mode=calls`. It must not read the JSONL directly (that would
duplicate the reader); it reads a small precomputed summary — see §6.1 for
where that summary lives, which is the one place these two designs interact.

### 5.4 Third surface (small, high value): a header chip

The preview header already carries mode icons and the Deploy affordance. A
small chip — `⚡ 142 · p95 1.2s`, red when the last call errored — is the
cheapest possible "your app is being watched" signal, and clicking it opens the
`calls` mode. Shell code, so phase 2, after the template earns its keep.

### 5.5 For agents and the terminal

The store is JSONL under a stable path. That is already an agent-legible
interface — `tail`, `grep`, `jq`, or the `duckdb` template with no new code.
Add two conveniences:

- `GET /api/calls/config` → `{dir, today, enabled, retention_days}` so a page
  or an agent can find the store without hardcoding the path.
- `fused-render calls [--page P] [--since 1h] [--failed] [--json]` — a CLI
  subcommand printing the same rollup the template shows. This is the surface
  a coding agent will actually reach for, and it costs ~80 lines on top of the
  reader.

---

## 6. Decisions (resolved — all shipped as recommended unless noted)

> **[shipped]** Resolved as recommended below, except where marked. 6.1 is
> **deferred with phase 2** (nothing consumes a summary yet — the header chip
> and the history section are the consumers, and both are phase 2). 6.3 is
> **not yet needed**: `/api/fs/raw` is logged plainly for now, since only
> `readFile` is attributed (an element `src` cannot carry a header), which
> turned out to cap the volume on its own. Revisit if a ranged reader proves
> otherwise.

**6.1 Where does the per-page summary live?** The header chip (§5.4) and the
history section (§5.3) both want "142 calls, p95 1.2s" without scanning the
log. Options: (a) compute it on demand in the reader — simple, but a scan per
render; (b) maintain `~/.fused-render/logs/summary.json`, a small
page→counters map updated by the writer thread — fast, one more file to keep
consistent; (c) write the summary into the page's **sidecar** under a
`callsSummary` key — puts it exactly where `history/` already looks and travels
with the file, at the cost of a sidecar read-merge-write on a timer. My
recommendation: **(b)**, with the sidecar left alone. Sidecar writes on a
firehose is the thing §4.1 rejected, and a debounced version of it is the same
bug with a longer fuse.

**6.2 Is client-side reconciliation worth it?** **[shipped — and the original
recommendation here was WRONG, corrected in D137.]** Deferring it traded away
CL-5, the feature's central guarantee: the server-side substitute this decision
assumed (`asyncio.CancelledError` out of `call_next`) **never fires**, so every
abandoned call was recorded as an ordinary success and counted in the
percentiles. The client now reports abandoned `call_id`s and `finish()` stamps
the outcome. It turned out cheap because of a timing accident the original
analysis missed — the client knows at abort time, ~1 s before the still-running
handler finishes, so the report lands before the record is written and no
append-only mutation is needed. Exact `client_ms` remains unshipped; that part
really is a nicety.

*Original text:* The `X-Fused-Call` +
`/api/calls/outcome` round trip (§4.3) is what makes `superseded` and
`client_ms` exact. Without it the server infers `disconnected` from a closed
socket, which catches most supersessions but conflates them with a closed tab
and loses client-perceived latency. Recommendation: **ship without it in phase
1** (server-only records, `outcome: disconnected`), add it in phase 2 once the
charts prove which distinction people actually miss.

**6.3 Do thin routes get sampled?** `/api/fs/raw` under a ranged reader can be
thousands of requests for one preview. Options: log all (honest, noisy,
retention burns fast), sample 1-in-N above a per-page rate, or **coalesce** —
one record per (page, path) per second carrying `count`, `bytes_sum`,
`status_set`. Recommendation: **coalesce**. It keeps the "my page read this
file 4000 times" signal, which is the actual bug, without 4000 rows.

**6.4 Params: full, keys-only, or off?** Recommendation: a three-way pref
defaulting to **full** (matching the serve spec's named trade-off and D3's
local-single-user posture), with `keys-only` one click away in Preferences and
documented in `docs/usage.md` next to the log location.

**6.5 Does the builtin executor gain `duration_ms`/`stderr`?** **[shipped in
phase 1, as recommended.]** `run_python` now times the call, and the subprocess
path attaches a `proc.stderr` tail — `_child.py` captures only stdout (to keep
its result protocol clean), so a warning printed by a run was otherwise lost.

*Original text:* Today only the
fused engine returns them (`engine.py:384`); the builtin path returns
`{ok, result, stdout}` (`executor.py:137`). The middleware can time the request
either way, so this is not blocking — but without it, `stderr_tail` is
mysteriously empty for the default engine, and a user comparing engines sees a
gap that looks like a bug. Recommendation: **do it in phase 1**; it is a small
additive change to `_child.py`'s envelope and independently useful.

---

## 7. Phasing

### Phase 1 — the log and the viewer (the whole ask, locally) — **DONE**

| File | Change |
|---|---|
| `fused_render/calls.py` | **new.** Record shape, caps, background writer thread, retention sweep, rate cap, `record()`, `GET /api/calls/config`. |
| `fused_render/server.py` | Include the router; one `calls.record()` in the middleware; enrich in `/api/run` and `/api/fs/write`. |
| `fused_render/static/runtime.js` | Shared `_fetch` wrapper adding `X-Fused-Page`; a `window.onerror` hook feeding `page-error` records (§9.2a). |
| `fused-render calls` CLI | Cursor-based reads, digest-by-default `--json`, `--follow` (§9.2b–d). Phase 1, not 2 — it is the agent's only surface. |
| `fused_render/executor.py`, `_child.py` | Return `duration_ms` + `stderr` tail (§6.5). |
| `fused_render/templates/calls/` | **new.** `template.html` (charts, tail, table, detail), `reader.py` (the ops in §5.1), `condition.py` (CT-12 gate), `icon.svg`. |
| `fused_render/templates/registry.json` | `calls` appended to `.html`/`.py`; `.calls.jsonl` key. |
| `fused_render/executor.py` | `calls/reader.py` onto `INPROCESS_HELPERS`. |
| `fused_render/shell/prefs.py` | `calls_enabled` (default on), `calls_params` (`full`/`keys`/`off`), retention days; surfaced on the Preferences page. |
| `tests/test_calls.py` | **new.** Record caps and truncation markers; fail-open on an unwritable dir and on a full queue; retention sweep; attribution present/absent; reader ops paging + bucketing; the `condition.py` gate. |
| `SPEC.md` §31, `DECISIONS.md` D136 | The spec section (CL-1..CL-n) and the decision row with rationale. |
| `docs/usage.md`, `skills/fused-render-authoring/SKILL.md` | Where the log lives, how to read it, the params-redaction knob. Authoring skill gains "how to check what your page is doing." |

Deliberately **not** in phase 1: shell UI, the header chip, deployed apps,
tile daemons.

### Phase 2 — make it ambient (not started)

Header chip (§5.4); the history-view Calls section (§5.3); client-side
reconciliation (§6.2); brush-to-filter
between chart and table.

### Phase 3 — deployed apps (the `fused` repo) (not started)

The serve plane already captures **failures** into
`errors/<env>/<token>/<rev-ts>-<err_id>.json` with a 14-day lifecycle, exposed
by `fused share errors` and rendered by `DeploymentErrors.tsx`. Widening that
to *all* calls is the same idea at a very different volume, so it is its own
design, not an afterthought:

- Per-call records for anonymous internet traffic cannot be unbounded — the
  existing 30-per-5-min rate cap exists because a caller controls the request
  rate and the owner pays for the storage. The answer is almost certainly
  **aggregate-by-default**: pre-bucketed counters per (mount, entrypoint,
  minute) — count, error count, duration histogram buckets, byte totals — plus
  full records for failures (already built) and for a **sampled** slice of
  successes.
- `share calls TOKEN [--since] [--json]` mirrors `share errors`, and
  `/api/deploy/calls` + a `DeploymentCalls.tsx` mirror the existing pair.
- The payoff is the one thing local logging cannot give: **the same charts for
  the deployed page, over real traffic**, in the same viewer, because the
  record shape was kept identical (§3).

This phase should not gate phases 1–2. Local-first is the right order — it is
where authoring happens, and it is entirely within this repo.

---

## 8. What it enables — for human users

- **"Why is my page slow?"** becomes a table read instead of a guess. The
  per-target rollup names the `.py`, the p95 vs p50 gap says whether it is
  always slow or occasionally awful, and the scatter shows the cold-start
  outlier that a mean would hide.
- **Regression detection without a benchmark.** The duration chart spans days.
  Edit a reader, re-run, and the step change is visible on the same axes as
  last week — no harness, no baseline file.
- **Catching the render loop.** A page that re-runs on every `onChange`
  without a diff guard produces an unmistakable dense bar in the calls chart.
  This class of bug is currently invisible: it works, it is just quietly
  running Python 40 times a second.
- **Seeing the cost of a slider.** The superseded series makes the D114
  cancellation visible — how much work a scrub throws away, and whether
  `opts.key` needs tuning.
- **The error you missed.** A `runPython` failure the page caught itself
  (author-handled rejection) shows nothing today, by design (`runtime.js:658`).
  In the log it is a row with the full traceback. Errors swallowed by a
  `.catch(() => {})` stop being invisible.
- **Result-size surprises.** "My table view got sluggish" resolves to
  "your reader stopped honouring `limit` and now returns 8 MB" in one glance
  at the size chart.
- **A real bug report.** "Zip me that file" (D68's whole thesis) gets much
  better: not just a traceback, but the sequence of calls, params, and timings
  that produced it.
- **Deployed vs local, compared.** Phase 3, but the reason the record shape is
  shared: the same page's numbers side by side.

## 9. What it enables — for AI agents

This is where the leverage is, and it is worth being concrete about *why*.
An agent authoring a fused-render page today has a blind spot exactly where a
human would use their eyes: it writes the `.html` and the `.py`, and then has
no idea what happened when the page ran. It cannot see the browser console, it
cannot see the overlay, and a page that renders blank looks identical to a page
that works. The call log closes that loop with a file it can read.

- **A verification loop that actually verifies.** Write the page → ask the user
  to open it (or open it via the deep link) → read the last N records. "Three
  calls, all ok, 80 rows, 40 ms" is *proof it works*. "One call,
  `KeyError: 'freq'`, traceback here" is a fix with a line number. Today the
  agent's only honest report is "I wrote the files; try it."
- **Error-driven iteration without the human relaying.** The traceback,
  stdout, the exact params, and the engine are all in the record. The user
  stops being a copy-paste conduit for their own console.
- **Self-benchmarking.** An agent optimizing a reader can read p95 before and
  after from the same store, on real invocations, rather than reasoning about
  which version *ought* to be faster.
- **Finding the bug class it can't see.** The stat-in-a-loop, the missing diff
  guard, the un-cancelled poll — these are structural mistakes that agents make
  routinely and that produce *working* pages. The log names them.
- **A grep-able, tool-free interface.** JSONL at a stable path means no MCP
  tool, no API client, no schema negotiation: `tail -5`, `jq`, or the `duckdb`
  template. The `fused-render calls --failed --json` subcommand (§5.5) is a
  one-line health check an agent can run unprompted.
- **Working inside the app.** The `claude/` chat template already runs
  Claude Code with cwd set to the target file's directory
  (`templates/claude/agent.py`). Point its system prompt at the call log and the
  in-app agent can answer "why did that just fail?" about the very page it is
  sitting next to — with the record, not with speculation.
- **Honest handoffs.** "I couldn't reproduce it" becomes "the log shows zero
  calls to that `.py` — the page never invoked it," which is a different and
  much more useful sentence.

The general shape: **the log turns authoring from open-loop code generation
into a closed loop with an observable.** That is worth more to an agent than to
a human, because a human already has a browser.

### 9.1 What an agent can already do, and what it actually gains

Worth being exact, so this isn't oversold. An agent can **already** verify the
Python half today with no new feature: `POST /api/run` with `X-Fused: 1` returns
`{ok, result, stdout, error}` synchronously. Testing a `.py` is a solved
problem.

What no agent can see today, by any means, is **what the page did** — which
calls its JS actually issued, in what order, how many times, with which params.
That is the gap this closes, and every agent workflow below lives in it.

| The agent's question | What answers it |
|---|---|
| Did my page work when it was opened? | records exist for `page`, all `outcome: ok` |
| **Did my page call Python at all?** | **zero records — the JS died before `runPython`** |
| Why did it fail? | `error.traceback` + the exact `params` that produced it |
| Is my page pathological? | calls/sec, and a high `superseded` count = work thrown away |
| Did my optimisation help? | the `targets` rollup, p50/p95 before vs after |
| What did the human actually try? | recorded `params` across their real interactions |
| Which of these 40 views are broken? | `--failed --since 24h`, all pages |

**The zero-calls signal is the single most valuable one.** A blank page that
made zero calls and a blank page whose reader raised look *identical* to an
agent today. They are completely different bugs. Separating them is most of the
diagnostic value here.

### 9.2 Five additions that make it usable by an agent

The log alone is necessary but not sufficient. Each of these is small, and
without them an agent burns turns guessing.

**(a) `page-error` records — the highest-value item in this whole design.**
The plan as written logs API *calls*. For an agent the most informative record
is the one where **no call happened because the page threw**. `runtime.js`
already hooks `unhandledrejection` (line 658, for the overlay) but has **no
`window.onerror`**. Add one, plus a `kind: "page-error"` record carrying
`message`, `source`, `line`, `col`, and the stack. That turns "zero calls,
cause unknown" into `TypeError: freq is not defined at sine.html:42` — a fix
with a line number. ~15 lines in `runtime.js` and one route. It is not an API
call, so it is a distinct `kind` in the same store, excluded from latency
charts.

**(b) A cursor, not a wall-clock guess.** "Since when?" is the whole problem in
a loop. `--since 2m` makes the agent guess how long the human took. Every read
surface should accept `--since-cursor <call_id>` and print the newest cursor on
exit, so the agent's next read is exactly "everything new." The `tail` op
(§5.1) already has the cursor; the CLI must expose it.

**(c) Digest by default, full records on failure.** An agent reading 400 raw
JSONL lines burns its context for no gain. `fused-render calls --json` should
emit a **rollup** — per-entrypoint counts, percentiles, outcome tallies — plus
full records only for failures, with `--verbose` to get everything. Context is
the scarce resource; pre-aggregating server-side is the whole reason the
`series`/`targets` ops exist, and the agent surface should default to them.

**(d) `--follow --timeout 60s`.** An agent cannot execute the page's JS: it can
run the `.py` directly, but nothing renders the HTML, and the deep-link scheme
is `fused-render://open?git=` (repo clone, D110/§26) — there is no
"open this local file" trigger. So phase 1's loop is genuinely
human-in-the-loop: *"I've written the page — open it and I'll check."* Make the
waiting ergonomic rather than pretending otherwise: a blocking follow that
returns as soon as records appear for a page (or times out) turns two round
trips into one. Headless rendering would close the loop fully, but Playwright
is a dependency this repo does not have and should not take on for this.

**(e) A line in the authoring skill.** `skills/fused-render-authoring/SKILL.md`
is what teaches an agent the `runPython`/params contract. If it does not name
the verification command, no agent will know the log exists and none of the
above happens. A short "check your work" section is the difference between a
feature and a used feature.

### 9.3 The loop, concretely

```
1. agent writes sine.html + sine.py
2. agent runs the .py directly            POST /api/run          → Python is sound
3. agent asks the human to open the page  (one message)
4. agent blocks on new records            calls --page sine.html --follow --json
5. reads the digest:
     0 records          → the JS never called Python; check page-error records (9.2a)
     1 record, error    → traceback + params; fix and go to 3
     3 records, all ok  → verified; report the timings
     40 records/2s      → render loop; add the diff guard
```

Step 2 is available today. Steps 4–5 are what this feature adds, and they are
the difference between "I wrote the files, try it" and "verified: three calls,
80 rows, 40 ms — and your slider is throwing away 37 runs per drag."

---

## 9.4 What implementation changed about the design

Three things building it revealed, and four more that only adversarial review
did (D137) — the four are in §9.5 because they are a different lesson.

From building it:

- **Built-in templates run from a staged copy**, not the package dir
  (`core_templates.py` stages them into `~/.fused-render/.core-templates/`).
  Both the self-read guard (§10 below) and `first_party` (§4.6) had to match
  all three locations — packaged, staged, and a user fork. Pinning the package
  path silently missed the copy that actually runs.
- **The viewer observes itself.** The `calls` view reads the log through
  `runPython`, which is an attributed app call — so the reader appeared as the
  busiest target in every page's rollup, and with Follow on, each poll's reads
  showed up in the next poll's results: a feedback loop that inflates the
  counts it reports forever. Reading the log now never appends to it (CL-10),
  matched by shape rather than by path for the reason above.
- **`fused.params.get()` returns `undefined`, not `null`**, for an absent key.
  A `!== null` guard therefore clobbers defaults with `undefined` —
  `Number(undefined) || 0` silently turned the default time window into "All",
  and the filter box rendered the literal string "undefined". Worth knowing for
  any template that reads params with a default.

## 9.5 What review revealed, after "done"

The defects found by asking "what doesn't work?" of a feature whose tests
were green. Recorded because the *shape* of each is more instructive than the
fix:

- **A guarantee with no producer.** CL-5's exclusion of superseded calls was
  implemented, specced, and tested — and nothing ever set the outcome it filters
  on. The test wrote `outcome="superseded"` records by hand and asserted the
  rollup dropped them: it tested the consumer, and the producer did not exist.
  Both halves correct, empty seam. The lesson is that a test which constructs
  its own input cannot tell you the input ever occurs.
- **A mode that passes its own gate and shows nothing.** The `.py` binding's
  `condition.py` correctly confirmed a data file had history, then the default
  scope filtered on a field a `.py` never occupies. Two correct components,
  contradictory together — and only visible by opening the thing.
- **A bound that measured the wrong dimension.** "Pre-aggregated so the charts
  stay fast" was true of the *response* and false of the *work*: a one-hour
  window parsed the whole retention window. Bounding what you send is not
  bounding what you read.
- **A cleanup that destroyed live data.** The directory cap deleted the file the
  writer had open — the safety mechanism doing the damage, in precisely the
  scenario it existed for.

Common thread: every one of them sat in the gap *between* two pieces that were
each individually correct and individually tested. None would have been found by
more unit tests of either side; three of the four needed a real browser or a
real socket, and the fourth needed a benchmark.

Two more from the follow-up round (D138), both the same shape as the first four:

- **A field name that reads as data.** Every *successful* record rendered as
  ERROR in `log_studio`, because a generic log viewer infers a level by sniffing
  the raw line for level words and the record carried `"error": null`. The
  record was right; the thing displaying it was fooled by a key name. Fixed by
  omitting null keys, which also shrank records ~20%.
- **A green spike that hid the failure case, twice.** Client-disconnect
  detection was verified with a body-less route, worked, and then hung every
  real request — `is_disconnected()` peeks by *consuming* a receive-channel
  message, starving the route of its body. The first round's lesson was "test
  the producer, not just the consumer"; this one's is **spike the negative
  case** — the request that should still succeed — not only the one you are
  trying to make work. The regression guard for it is kept in the suite because
  the trap is re-approachable.

Two more from the fourth round, and both are the sharpest kind: a comment in the
code stated the very fact that the code next to it ignored.

- **The right fact, applied in one place out of two.** `_iter_records` carried
  the note *"files are only skipped, never stopped at, because same-day files
  from different processes interleave in time"* — correct, and load-bearing for
  the `since` bound. But the same interleaving makes "the last file by name" not
  "the newest records", and the walk still drained one file before the next. With
  two live servers — the case per-pid files exist for — `query` returned a stale
  process's tail as the newest page, and `--follow` waited out its full timeout
  with the live server writing in front of it. Compounded by pid order being
  *lexical*: pid 8000 sorts after pid 12345, so it needs no restart and no
  wraparound, just two servers whose pids differ in digit count. Fixed by merging
  each day's files on append time. The lesson: when you write down *why* a bound
  is shaped a certain way, go re-audit every other place that fact bears on —
  the comment is evidence you understood the hazard and stopped one line short of
  it.
- **A repair that skipped its own repair path.** The oversized-record shrink
  serialized its output directly instead of re-running the prune, so records over
  the cap still carried explicit nulls and no `level` or `recorded_at` — the
  `"error": null` ERROR misread from the round above, alive again on exactly the
  records most likely to be worth reading (a huge payload usually means something
  went wrong). Two fixes, one applied on the main path only. The lesson: a fix
  belongs at the chokepoint every path passes through, and if there are two exits,
  the test has to take both.

And one from the fifth round, which is the third distinct way the same primitive
broke:

- **A key computed from the wrong scope.** `query` took its returned cursor from
  the first record in the store walk, *before* applying the caller's filters — so
  the cursor was the newest id in the store rather than the newest id the caller
  was shown. `--follow --page X` therefore woke on another page's traffic and
  then reported no calls for X. Note what this shares with the two rounds before
  it: three separate defects (append-vs-start time, file-vs-merge order, and now
  store-vs-filter scope) all surfaced as *the follower reporting no activity when
  there was activity*. The lesson is that a false negative in a verification tool
  is a single symptom with many causes, and finding one cause is no evidence the
  others are absent — after fixing the first two I did not go looking for a third.

Then a sixth round found the **fourth** cause of that same false negative, in the
one place I had not looked — not the store or the read, but the CLI's *wait
condition*. `--follow` waited for the store tip to move past the pre-wait
baseline even when an explicit `--since-cursor` already had unseen records behind
it. That is the normal agent race: ask a human to open a page, the calls land, and
only then run follow — which then timed out holding exactly the records it was
waiting for. Reproduced with two records sitting on disk and a 6 s timeout fully
consumed. So the sweep I had just prescribed, I then performed too narrowly:
I re-audited the store and read paths and never questioned the predicate that
decides whether to wait at all. Knowing the right method is not the same as
applying it widely enough.

The same round also found a **confident claim about something never checked**:
`cursor_missing` was reported as "purged by retention, or wrong" even when the
seeking walk had simply hit its scan budget without reaching the cursor. A valid
deep cursor read as a dead one, and the skipped gap went unmentioned.
`scan_truncated` now separates the guess from the proof.

An eleventh round found one, and it is about **duplication**, which is a different
failure mode from every round before it. `condition.py` has to work as a standalone
copy in the user template dir, so it re-implements the branch-aware store path
instead of importing it — and it re-implemented it *wrong*, joining the raw
`FUSED_RENDER_BRANCH` value. That is wrong three ways at once: a default-branch ref
(`main`) is a baseline opt-out and must not nest at all; refs are sanitised; refs are
truncated to 12 characters. This very branch writes to `branches/claude-fused/` while
the gate probed `branches/claude/fused-api-call-logging-d97w88/`. Every case sends the
probe somewhere nothing writes, so the gate fails closed and the Calls mode never
appears on a page full of records.

Two things worth keeping. First, the correct version already existed **one template
folder over** — `zarr_aoi/tile_server.py` inlines the same resolution and its docstring
says to keep it in lockstep with `_branch.sanitize`, a precedent from an earlier review
round that I never looked for while writing the second copy. **When you deliberately
duplicate a rule, the first question is who else already duplicated it.** Second, and
more durable: a duplicate justified by a *comment* is a duplicate that will drift. The
fix is not a better comment, it is a test asserting the two implementations agree —
`condition._store_dir() == calls.store_dir()` over a table of refs. That test, not the
three individual rules, is the deliverable. Which cases did the raw join get right?
Baseline, and an already-canonical ref: exactly the two anyone would try by hand.

A tenth round found two, and the first is the ninth round's own promise breaking one
branch over. D144 added "a lost cursor is never silent" — and `--follow`'s timeout
branch returns before *both* output sites, so the flag reached the caller only when
records happened to arrive. It hardcoded `"cursor_missing": False` and printed a bare
"no new calls within Ns". That withholds the flag in exactly the case it explains: an
agent holding a ghost id usually has nothing arriving either, and "nothing ran" and
"I could not tell you what is new" are different answers to act on. **A flag that is
only reported when it does not matter is worse than an absent one, because its
silence reads as "fine".** The test I wrote for D144 called this very path and
asserted only `timed_out is True`. The habit that catches this class: when you add a
value, check every *exit* that consumes it, not just the one you were looking at.
Carrying `scan_truncated` alongside it needed the same care — on the follow path it
has to come from the probe, because once the cursor is replaced by the baseline the
post-wait read is no longer looking for the caller's id at all, so a valid-but-deep
cursor was getting the confident "purged" wording on every follow.

The round's second finding was the feature's first UI-concurrency bug, and it needed
a harness the repo did not have. The Calls view's `load()` was single-flight by
`if (inflight) return;` — which *drops* the newer request. The in-flight read is
already committed to the filters it started with, so changing scope, window, query or
Failed mid-read rendered the previous filters while the controls and the URL showed
the new ones, and nothing reloaded until the user pressed Refresh. A pending flag with
the re-run in `finally` fixes it and keeps the property that mattered (one re-run, so
a burst of clicks costs two reads rather than N). The template's JS had only
source-grep assertions until now; this is tested by **extracting the real `load()`
from `template.html`** and driving it under node with stubbed ops. Extracted rather
than copied on purpose — a copied function keeps passing after the shipping one
regresses, which is the single thing a test of a concurrency guard must not do.

A ninth round found two more, and the first completes a pattern worth naming: for
three rounds running, the state that broke was one my *previous fix had just made
reachable*. D143 correctly sent an unfindable cursor to the wait — and the post-wait
read then used that ghost id, which `query` answers with "the newest page", so a
follow that waited *successfully* reported history as arrivals. The timeout path was
already empty and correct, which is why it read as done. **When a change makes a
previously-unreachable path reachable, that path is the one to test.** Fixed by
resuming from the pre-wait baseline, which is exactly "everything up to the moment
the wait began". The round's second finding was the feature's first real concurrency
bug: `_prefs_snapshot` reads `prefs.json` outside its lock (deliberately — holding a
lock across file I/O would serialize every logged call behind one disk read) and then
stored the result unconditionally, so an invalidation landing mid-read was overwritten
and the pre-toggle snapshot served for the whole 1 s TTL. A generation counter closes
it. The 1 s window looks benign until you notice it is exactly the window a user
toggling the preference is watching.

An eighth round found a **regression in the D141 fix itself**, and it is the
sharpest lesson of the lot. I had written that "a careless version of this fix
answers instantly for every follow and quietly deletes the feature", and tested two
cursor shapes against exactly that — a cursor at the tip, and no cursor. I did not
test a **third** shape: a cursor from a *different* read. Because the early-answer
test was `cursor != baseline`, which asks "is this the current tip", a cursor from a
broader read (the ordinary agent pattern: take a global cursor from `calls --json`,
then `--follow --page X`) is never the narrower tip — so follow skipped the wait,
matched nothing, and reported "no calls recorded" in 1 s of a 5 s timeout. Fixed by
asking the actual question with a bounded read: *are any matching records newer than
this cursor?* The lesson is about the shape of negative-case testing: I enumerated
the negative cases I had thought of while writing the fix, which is the same
blind spot as testing the consumer you just wrote. Enumerate the *input space* —
here, "where did this cursor come from" had three answers and I covered two.

A seventh round then found the *same* name-order belief in a second place: the
`condition.py` gate's bounded "newest files" probe was also reverse-name order, so
with a few same-day files its whole window could be stale and the Calls mode never
appeared on a page that had records (reproduced: gate `False` with the page's
records sitting in the live file). Both now order by mtime. Three rounds running,
the finding has been *the fix was not propagated widely enough* — first across the
layer, then across the symptom, now across the codebase. The cheap habit that would
have caught all three: after fixing a wrong belief, grep for the belief, not for the
bug. Doing that here also turned up the size trim asserting "name-sorted is
oldest-first" in a comment — harmless in practice, since same-day files are the same
age and today's are excluded, but it was the same false belief written down as
justification, so it is now mtime too.

Sweeping the *symptom* rather than the line — the thing that round says to do —
also turned up a cause that is still open, now documented at `overview()`:
`dropped` counts what the **calling process** dropped to the rate cap or a full
queue, so the in-server view reports it (the reader runs in-process, D72) while
the CLI, a separate process, always reads 0. An agent that follows, times out and
sees `dropped: 0` cannot distinguish "nothing ran" from "the rate cap ate it".
Closing it means persisting a periodic drop marker into the store, which adds a
record kind (CL-2) — a feature, not a review fix, so it is recorded here rather
than smuggled in.

**Two spellings of the same path (D147).** The twelfth round is the eleventh's
class of bug one layer down. D146 was two spellings of a *directory*; this is two
spellings of a *file path*: on Windows the store held `page` forward-slashed
(headers, and what a `/view` URL decodes to) and `entrypoint` backslashed for a
relative `/api/run` target (`os.path.normpath`), while the CLI's `--page` filter
ran through `os.path.abspath` and came back backslashed as well. Exact-match
filters then matched nothing — `calls --page` found not even the page the caller
was standing on. Two lessons beyond the fix. First, **the fix belonged at the
boundary, not at the three reported call sites**: there was one broken writer and
one broken filter, and canonicalizing in `record()` repaired the third reader —
the gate — without touching it, where patching each reader would have left the
store holding two forms and the next reader broken by default. Second, and
sharper: **the platform I do not run on is the one the invariant breaks on.** The
two forms are identical on POSIX, so every test and every manual check was blind
to this by construction — which is why the regressions simulate Windows with
`ntpath` and drive-shaped literals instead of sitting behind a `sys.platform`
guard that would only ever run where the bug cannot happen. After D146 I had
verified the gate and the writer agreed about the *directory*; I never asked
whether they agreed about the *paths inside it*.

**Two directories, two lifetimes (D148).** The thirteenth round is the smallest
and the tidiest: `Browse call logs` navigated to the store directory, which the
writer creates on its first append — so before any page had made a logged call
the button opened a stat error. The fix worth noting is the one I did *not*
make: `makedirs` in the prefs handler would have worked and been wrong twice, a
read that provisions storage plus an empty store conjured for someone who never
records a call. Reporting `dir_exists` instead keeps the lazy create where it
belongs and lets the UI say *why* — and "no calls recorded yet" is the same
answer as "why has no page got a Calls mode?". What makes it instructive is the
asymmetry it explains rather than hides: `log.dir` sits two lines above
`calls.dir` in the same payload and needs no flag, because logging creates its
directory at startup. Two paths in one payload with two different lifetimes, and
the UI had been treating them as one kind of thing.

**Reporting the stored value instead of the effective one (D149).** The
fourteenth round is the same class as D148 — a payload telling the UI something
other than what the system is doing — but the precedent was sitting two lines
above it. `FUSED_RENDER_CALLS` and `FUSED_RENDER_CALLS_RETENTION_DAYS` beat the
stored prefs inside the resolvers, so Preferences showed *capture on, keep 90
days* while the process had *capture off, keep 1 day*. The engine block
immediately above in the same payload exists to prevent exactly this and says so
in its docstring: "the SAME resolver the server's dispatch uses, so the page
never reports a different running engine." Two things to keep. First, the fix is
a **call, not a copy** — `effective_*` comes from `calls.enabled()` /
`calls.retention_days()`, and a test pins it across the spellings the real
resolver accepts (`off`/`no`/`false`/`0`), which a re-derived `== "0"` check
would fail; that is D146/D147's lesson applied before the drift rather than
after. Second, **an existing pattern in the same file is the cheapest
correctness check available, and I hadn't looked** — the question "does anything
here already solve this?" would have found `engine_state()` in one grep.

**Fixing half of a function (D150).** The fifteenth round found the *other half*
of D149, in the function D149 had just written. The `effective_*` values were
taken from the writer's resolvers, exactly as intended — and the `*_forced_by`
flags beside them were `os.environ.get(...)`, a presence check, which is the same
second copy of the precedence rule the paragraph above is about. It survived
review because presence and force coincide for `FUSED_RENDER_CALLS`, where every
set value decides something, so half the payload was right by luck. For retention
they diverge: `retention_days()` honours only an integer, so `=abc` or `=` left
the writer on the stored 30-day pref while the page greyed out the retention
select and captioned it *locked by `FUSED_RENDER_CALLS_RETENTION_DAYS=abc`* — a
window the user could then change neither from the page nor by editing a variable
that was never in force. The question "does this variable win?" now has one owner,
`calls.retention_days_override()`, with `retention_days()` expressed in terms of
it, and one reader, prefs' `_forced_by`. The lesson is narrower than D149's and
sharper: **when a fix is "ask the writer instead of re-deriving", it has to be
applied to every value in the function, not the one the bug report named.** A
report is evidence of a class, not an inventory of it — D147 taught that about
call sites and this taught it about the lines of a single return statement.

**A fix has a class, and the class has other members in the same diff (Follow's
rebuild).** The sixteenth defect was reported by the owner, not Bugbot: while
Following, the whole view refreshed on every 2 s poll. The render was one
`innerHTML` replacement — scroll position, the open detail row, and any text
selection thrown away four times a sentence, in exactly the mode whose purpose
is *reading the log as it grows*. What stings is that I had just fixed this
precise bug in log_studio's Tail, in the same working session, and did not turn
around and ask which other template polls and rebuilds — this one, two files
away, written by me, with rows keyed by array index so nothing could be matched
across a poll even in principle. The view even contained both halves of the
lesson already: the row-click path refused to redraw the charts "for no
reason", and the Follow handler disabled auto-reload specifically to protect
scroll — then reset scroll itself. The fix is the same shape as log_studio's
(keyed in-place reconcile, charts only on changed data, scroll pinned-or-held),
with one difference worth noting: records already carry identity (`call_id`),
so no synthetic key was needed. D145 taught that a report names an instance of
a class, not its inventory, about call sites; D150 taught it about the lines of
one function; this round teaches it about *templates* — when a fix lands in one
template, grep the others for the pattern before calling it done.

Across all sixteen rounds the pattern never changed: each defect lived in a seam
between individually-correct, individually-tested parts — a rule and its own
copy, two spellings of one value, two directories with different lifetimes, a
stored setting and the effective one. What
did change is where the seams were — the later ones were *inside* code I had
already reviewed and documented, which is the argument for adversarial review
outliving "done".

## 10. Other uses this unlocks

Ordered by value-per-unit-of-work, from the same store.

**10.1 A `.py` performance profile, for free.** The per-target rollup is
already a profile at call granularity. Add nothing and a user can see which of
their data files carry the page. Add optional in-reader spans later
(`fused.mark("query")`) and it becomes a flame-ish breakdown — but the 80% is
free.

**10.2 Cache and memoization, decided by data.** `PY-9` guarantees a fresh
execution every call, and `engine.py` explicitly disables result caching. The
call log is the evidence base for revisiting that per-page: "this `.py` was
called 400 times with 6 distinct param sets, p95 900 ms" is a cache hit rate
of 98.5% waiting to happen. Log first, then decide — and the log also measures
whether the cache helped.

**10.3 Regression gates for template authors.** The built-in templates are
themselves pages that call readers. A recorded run of the template suite
becomes a latency baseline; a PR that makes `duckdb/reader.py` 3× slower shows
up as a number. Cheap CI value from a feature built for users.

**10.4 Replay and repro.** Every record holds the entrypoint plus the exact
params. "Re-run this call" is a button on the detail pane, and
`fused-render calls replay <call_id>` is a repro command that fits in a bug
report. Since params are already the whole input contract, this is nearly free
once the log exists.

**10.5 A session transcript for the history view.** `history/` (D96) already
tells the story of a file: who chatted about it, what was commented, what
params it last had. "And here is every time it ran" completes that narrative,
and the deep-link plumbing (HV-7) exists.

**10.6 Tile-daemon visibility.** The sci templates' loopback daemons
(`geotiff`, `map`, `zarr_aoi`, `pyramid`) serve the interaction users most
often call slow — panning a map. They are outside the log by construction
(D122). Teaching them to append to the same store (they already own a state
file and a token) would make "why is panning slow" answerable with the same
charts. Meaningful work, high payoff, naturally a follow-up.

**10.7 Mount-aware diagnostics.** Records carry the entrypoint path, and
`shell/mounts.py` knows which paths are mount-backed with a cold/warm prefetch
state. Joining them answers the most common confusing slowness in the product:
"this call took 14 s because it was a cold read over a remote mount," not
"your code is slow." A `mount`/`cold` flag on the record is a small addition
with a large explanatory payoff.

**10.8 Export/deploy readiness.** `export.py` statically scans a page for
literal `runPython`/`rawUrl` calls and fails loudly on a computed target it
cannot see (`docs/EXPORT.md`). The call log is the *dynamic* truth: the set of
targets actually invoked. Diffing the two turns a class of deploy-time
surprise ("hosted page 404s on a file the scan missed") into a pre-deploy
warning in the Deploy modal — a genuinely novel use, and one only possible
because both halves live in this repo.

**10.9 Usage-shaped answers about your own tools.** Across pages, the log says
which views you actually open and run — the honest input to "which of my 40
views matter," and a better Recents than recency alone.

**10.10 A worked example of the platform.** The `calls` template is a
fused-render app that reads a data file, charts it, and filters by URL params.
It ships as the reference implementation for the pattern it documents.

---

## 11. Non-goals and risks

- **Not a security or audit log.** D3 stands: this is a local single-user tool
  with no auth layer. The call log is a diagnostic, not an attestation, and
  nothing should be built on it as if it were tamper-evident.
- **Not complete.** It sees calls the runtime makes to this server. Not a
  page's own `fetch()` to a third party, not the tile daemons (§10.6), not
  anything a `.py` does internally.
- **The privacy surface is real, and small.** Params can carry secrets. The
  store is under the user's home dir, on their machine, alongside their
  bookmarks — but it is now a file that persists what used to only be in a URL.
  §6.4's knob plus a line in `docs/usage.md` is the honest treatment.
- **Disk.** Answered structurally by three independent bounds (per-record
  caps, per-page rate cap, retention + size sweep). A logging feature that
  fills the disk would be a worse bug than the one it was built to find; each
  bound must be tested, not asserted.
- **Overhead must be unmeasurable.** A `put_nowait` onto a bounded queue plus
  a background append. Fail-open everywhere: an unwritable directory, a full
  queue, a serialization failure — each drops the record and never touches the
  response. Worth an explicit test that a broken log directory leaves
  `/api/run` behaving byte-identically.
- **Chart honesty.** Superseded calls excluded from percentiles by default
  (§2), tails not heads on truncation, and no smoothing that hides an outlier.
  A dishonest observability feature is worse than none, because people trust it.
