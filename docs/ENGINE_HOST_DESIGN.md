# Template engines as server-managed subprocesses, served through :1777

**Status: implemented and generalized** (`server/engine_host.py`,
`server/routers/engines.py`). This began as a fix for the map template's
"Loading visible tiles… forever" failure — moving the tile engine's *ownership*
into the fused-render server (`:1777`) and serving tiles through the stable
server origin instead of the daemon's ephemeral port — and was then lifted into
a **generic, template-agnostic engine host** so the server carries no
map-specific code.

**Current shape (separation of concerns).** The server owns a reusable
subsystem keyed by an opaque `engine_id`:

- `server/engine_host.py` — spawn/status-poll/reap/kill/restart of one child per
  engine id; validates the interpreter (home venv store) and daemon path
  (`<templates-root>/<engine_id>/daemon.py`); replays opaque *reinit* requests on
  restart. Knows nothing about tiles or descriptors.
- `server/routers/engines.py` — `POST /api/engines/{id}/ensure`,
  `/reinit`, `/forget`, and an opaque `ANY /api/engines/{id}/proxy/{path...}`
  passthrough (heal-on-failure, cancel-on-disconnect; proxied POST needs X-Fused,
  GET is an open read).

All map-specific knowledge lives in the template: `templates/map/map_render.py`
posts to `/api/engines/map/…`, rewrites its own descriptor URLs to
`/api/engines/map/proxy/…` paths, and registers each describe as a reinit for
replay. The only place `"map"` appears on the server is as an id the template
passes in. The next tile daemons (geotiff, zarr_aoi, netcdf, pyramid) adopt the
host by picking an engine id — no server change.

The sections below are the original map-framed design record; read
`engine_host`/`engines` for `map_engine`/`map_tiles` and
`/api/engines/map/proxy/…` for `/api/map/…`.

---

## The bug, as observed

Opening a NASA HLS scene on Azure
(`hls2euwest.blob.core.windows.net/.../HLS.L30.T55GEP...B01.tif`) in the map
template sits on **"Loading visible tiles…"** forever. Traced end to end on
2026-08-21:

1. Anonymous `GET` → `409 PublicAccessNotPermitted` (expected; the storage
   account has public access off).
2. The browser-side COG reader correctly declines — the blob endpoint has CORS
   off, so the fetch is blocked (`No Access-Control-Allow-Origin`), and the band
   is `int16`/single-band anyway.
3. It falls back to the Python engine, which **succeeds**: `blob_tokens.py`
   learns the 409, fetches a Planetary Computer SAS token, and `/describe`
   returns a real descriptor (`EPSG:32655`, `int16`, `nodata −9999`,
   `native_minzoom 8`, `warnings: ["opened with a temporary read token from
   Microsoft Planetary Computer"]`). The extent box you see is real. A standalone
   rio-tiler read confirms the data is fine: open 0.98 s, tile 0.66 s.
4. **Tiles never arrive.** The descriptor points MapLibre at the daemon's
   ephemeral port (`http://127.0.0.1:62818/tiles/...`). The browser fires the
   `z9` tile request and it hangs with no response, forever. A first curl to that
   same URL returned `200` in 1.25 s; every request after wedged.
5. The daemon's port stays `LISTEN`ing (the process is alive) but `/ping` and
   `/tiles/...` stop answering (`curl` → `HTTP 000` in ~2 s). Alongside it, **18
   orphaned `daemon.py` processes**, spawned in venv+payload pairs at identical
   seconds — the documented spawn storm.

The data path, the SAS signing, the `int16` rendering — all correct. The load
stalls purely on **daemon reachability**: the browser holds a URL bound to an
ephemeral port on a process that has wedged, and nothing heals it.

---

## Root causes

Three are architectural; the fourth is the proximate wedge.

**RC1 — Tile URLs are bound to an ephemeral port.** The browser holds
`http://127.0.0.1:<random>/tiles/...`. Any daemon death, wedge,
version-supersession, or restart permanently breaks every tile the page holds,
and it is indistinguishable from an ordinary tile error, so the UI just spins.
(This is the same conclusion the earlier investigation reached as "finding F".)

**RC2 — Nobody owns the daemon's lifecycle.** It is spawned *detached* from a
short-lived `runPython` worker (`map_render.py::_spawn_daemon`). No supervisor
health-checks it or restarts it; "recovery" is a client-side re-`describe` hack
(`recoverTiles` in `template.html`). Orphans accumulate because detached spawns +
a version-hashed state file + worktrees sharing `~/.fused-render` defeat the
hand-rolled `_retire_superseded` / `_already_serving` guards.

**RC3 — Tiles do not go through the app's stable origin.** Everything the app
controls lives on `:1777`; the tiles are the one thing on a random port outside
it. So browser caching can't survive a restart, and the daemon's death is not
something the app can paper over.

**RC4 — The child HTTP server wedges under exactly the load a viewport
produces.** `daemon.py`'s `ThreadingHTTPServer` runs `protocol_version =
"HTTP/1.1"` with `timeout = 65`, so every browser keep-alive connection parks a
worker thread for up to 65 s idle; `request_queue_size` is the stdlib default
**5**; there is no cap on concurrent tile renders and no cancel-on-disconnect. A
burst of viewport tiles against a slow remote source accumulates threads/parked
connections faster than they drain — the listen backlog overflows (new
connections, `/ping` included, get refused → `HTTP 000`) and, if
`Thread.start()` ever fails under the pressure, the exception escapes
`serve_forever`'s request loop and the accept loop dies while the socket stays
bound. Either way: "listening but not answering", which is what was measured.

*As built, the proximate wedge turned out to be sharper than the load theory.*
Implementing the fix reproduced it deterministically with a 12-line script: on
Windows, **a thread that has done GDAL `/vsicurl/` work deadlocks the whole
process the moment it exits** — its DLL thread-detach ends up holding the
loader lock against a native thread that holds the GIL, so the very next
`Thread.start()` (which socketserver runs per connection) blocks forever.
py-spy shows the accept loop parked inside `Thread.start()` and one unnamed
native thread `active+gil`. Every remote describe tripped it twice: the
per-connection handler thread that ran the describe, and the ephemeral
`_prepare` preview/optimize thread `raster_engine._start_preparation` spawned.
The fix is confinement: all describe/tile work runs on a persistent
`RENDER_POOL` (`ThreadPoolExecutor`, CPU-width — which also supplies RC4's
concurrency bound), and preparations run on the engine's one persistent
`prepare_pool` thread. Handler threads never touch the geo stack, so their
exits are harmless. Verified: Azure HLS and Maxar describes + tiles, then
`/ping`, all answer where they previously froze the process.

---

## Why this is not a map-only accident

A survey of every tile-serving template found the map template is doing what the
whole family does:

| template | daemon? | tile/data URL origin | lifecycle owner |
|---|---|---|---|
| **map** | Yes, long-lived, detached | **ephemeral port** `…:<rand>/tiles/…` | self (state file, no idle exit) |
| **geotiff** | Yes, long-lived, detached | **ephemeral port** `…:<rand>/tile/…` | self (state file + 30-min idle reaper) |
| **zarr_aoi** | Yes, long-lived, detached | **ephemeral port** `…:<rand>/tile/…` | self (idem) |
| **netcdf** | Yes, long-lived, detached | **ephemeral port** `…:<rand>/tile/…` | self (idem) |
| **pyramid** | reuses geotiff's daemon | **ephemeral port** `…:<rand>/ltile/…` | shared geotiff daemon |
| **las** | no — one-shot worker | **:1777** runPython bridge (base64 arrays) | n/a |
| **vector** | no — one-shot worker | **:1777** runPython bridge (inline GeoJSON) | n/a |

No template serves tiles through `:1777` with a server-side proxy. `las` and
`vector` route everything through `:1777` only because they inline all data in
one `runPython` response — which does not scale to XYZ tiles. So the fix
introduces a **new pattern** for this repo. That is why the design is written to
be reusable: the map engine is the first tile daemon to adopt it; the other four
are the obvious follow-ups (out of scope here).

---

## The fix

The app already ships **both halves** of what is needed, used elsewhere. The map
engine adopts both.

### Half A — the server owns the subprocess (model: `fused_render/ai/supervisor.py`)

`ai/supervisor.py` is the proven precedent: the server starts a child that binds
`:0` and publishes `{port, token}` to a status file (no reserve-then-exec race),
health-polls it, keeps the `Popen` handle so it can **reap** (via `poll()`, not
`os.kill(pid,0)` — which is fatal on Windows) and **restart** it, kills the whole
tree cross-platform (`_kill_tree`: Windows `CTRL_BREAK` then `taskkill /T /F`;
POSIX `killpg` with a leader guard), and tears everything down from a server
`shutdown` event (`unload_all()`), so nothing outlives the app.

The map engine gets a smaller sibling of this: **one supervisor in the `:1777`
process holding exactly one `templates/map/daemon.py` child.**

### Half B — tiles flow through :1777 (model: `fused_render/server/proxy.py`)

`proxy.py` already forwards a `GET`/`HEAD` with its `Range` header to a localhost
subprocess and streams the answer back with a filtered header set. The new router
does the same for map tiles, plus a restart-and-retry on a dead child.

### Flow after the change

```
describe:  template.html ──POST /api/map/describe──▶ :1777 router
                                                       │ ensure() child, proxy /describe
                                                       ▼
                                                     child daemon (managed)
                                                       │ heavy geo work, returns descriptor
           router rewrites tile_url ◀──────────────────┘
             = "/api/map/tiles/{source}/{z}/{x}/{y}.png"   (stable :1777 path)

tile:      MapLibre ──GET /api/map/tiles/…──▶ :1777 router ──proxy──▶ child /tiles/…
                                               │ on child-dead/wedge:
                                               │   supervisor.restart(); retry once
                                               ▼
                                             PNG streamed back (browser never sees the port/token)
```

The browser only ever talks to `:1777`. The child's port/token stay server-side.
A wedge or restart is **invisible**: the tile URL the page holds is stable, and
the router heals the child underneath it.

---

## File-by-file plan

**New — `fused_render/server/map_engine.py`** (the supervisor)
- Module-level singleton: one lock, one `Child` dataclass (`proc`, `port`,
  `token`, `version`, `started_at`).
- `ensure() -> Child`: return the live child if `/ping` (≤2 s) matches the
  backend `VERSION`; otherwise spawn one under the lock. *As built, the spawn
  interpreter is the caller's, not the server's*: per D276 the geo stack lives
  in the map template's project venv (`<home>/venvs/<hash>`), which is where
  `map_render.py` itself runs, so it hands its own `sys.executable` (plus the
  daemon path, VERSION and cache dir) over `POST /api/map/ensure`. The server
  validates before spawning — the python must be under `home_dir()/venvs` or be
  the server's own interpreter (the builtin executor owns no venv machinery and
  runs templates on the app python), and the daemon must sit inside a known
  templates root. Child binds `:0`, writes `{port, token, pid, version}` to a
  status file, supervisor reads it back (the no-race `_spawn`/status-poll from
  `ai/supervisor.py::_spawn`).
- `remember(source_id, request)` / replay: a fresh child has an empty source
  registry, so a bare restart would 404 every tile URL the pages hold. The
  supervisor keeps the describe request that registered each source (bounded)
  and `restart()` replays them into the new child before any retry — this is
  what actually makes a daemon death invisible.
- `alive() -> bool` via `proc.poll()`. `restart()` = kill + ensure.
- `stop()` = `_kill_tree` (lift the cross-platform version from
  `ai/supervisor.py`), called from an `app.py` `shutdown` event.
- `VERSION` = the existing `_backend_version()` hash over the backend files, so a
  code change still forces a fresh child.

**New — `fused_render/server/routers/map_tiles.py`** (the proxy)
- `GET /api/map/tiles/{source}/{z}/{x}/{y}.png`
- `GET /api/map/vtiles/{source}/{z}/{x}/{y}.pbf`
- `POST /api/map/describe`
- `GET /api/map/jobs/{source}`, `POST /api/map/optimize/{source}`
- Each: `child = map_engine.ensure()`, forward to
  `http://127.0.0.1:{child.port}{path}?t={child.token}` (streaming, `Range`
  passthrough — reuse `proxy.py::_proxy_raw`'s shape), return the response.
- **Heal-on-failure**: on a connection error / `HTTP 000` / timeout from the
  child, call `map_engine.restart()` and retry **once**; only then surface an
  error. This is the piece that makes RC1/RC2/RC4 invisible to the page.
- CORS is no longer needed (same origin as the page) — drop the `*` allowance.

**Edit — `fused_render/server/app.py`**
- `app.include_router(map_tiles_router)`.
- Add `@app.on_event("shutdown") async def _shutdown_map_engine(): map_engine.stop()`
  next to `_shutdown_ai_workers` — the child now dies with the app, by the same
  mechanism as the AI workers.

**Edit — `fused_render/templates/map/map_render.py`** (thin it out)
- `_describe` now posts to `{FUSED_RENDER_ORIGIN}/api/map/describe` (the origin is
  already exported to every child, see `app.py::set_server_origin_env`) instead
  of spawning and pinging its own daemon.
- Delete `_spawn_daemon`, `_ensure_service`, `_retire`, `_retire_superseded`,
  `_wait_for_service`, `_ping`, the `START_LOCK`/`STATE` dance, and the
  ephemeral-port `_service_url`. The descriptor's `tile_url` / `vtile_url` /
  `job_url` / `optimize_url` become server-relative `/api/map/...` paths (the
  server rewrites them; `map_render` just stops emitting ephemeral ports).
- Keep `_run_oneshot` as the last-ditch fallback for when the server route itself
  is unreachable (very unlikely — it *is* the server the page is talking to).

**Edit — `fused_render/templates/map/daemon.py`** (own less, harden the rest)
- Delete `_already_serving`, `_idle_monitor`, `IDLE_TIMEOUT`, and the
  self-registration state-file logic that existed to let peers find it — the
  supervisor is the only client now and hands it its status path.
- Keep the engines (`RasterEngine`/`VectorEngine`/`MultidimEngine`) and the
  route handlers unchanged.
- **Harden the transport (RC4):** raise `request_queue_size` to 128; run every
  describe/tile render on the persistent `RENDER_POOL` sized to CPU count (the
  pool is both the concurrency bound and the fix for the thread-exit deadlock —
  see RC4 *as built*); keep dropping `wfile.write` cleanly on client
  disconnect. The token check stays (defence in depth even behind the proxy).

**Edit — `fused_render/templates/map/template.html`**
- Point sources at the descriptor's now-`:1777` `tile_url` (no code change if it
  already just uses `descriptor.data.tile_url`).
- **Delete `recoverTiles`** and the ephemeral-port rebind logic — healing is the
  server's job now, and the URL never goes stale.

**Deletions net-out:** `map_render.py` loses ~150 lines of daemon
plumbing; `daemon.py` loses the singleton/idle/retire logic; `template.html`
loses the recover hack. The fragile machinery is *removed*, not added to.

---

## Test plan

Following the template's existing `tests/` + Playwright convention (TDD, tests
first in `templates/map/tests/`):

- **`tests/test_map_engine.py`** (new, server-side): `ensure()` reuses a live
  child; a version bump forces a new one; `restart()` produces a working child;
  `stop()` leaves no process; a wedged child (simulated: handler that never
  answers) is detected and restarted by the router's retry.
- **`tests/test_map_tiles_router.py`** (new): a tile request returns PNG bytes
  through `:1777`; killing the child mid-session and re-requesting the same URL
  heals transparently (the assertion the current architecture cannot pass).
- **Retire the now-moot tests**: `test_daemon_singleton.py`,
  `test_daemon_retire.py`, `test_daemon_lifetime.py` describe behaviour that no
  longer exists; fold what's still meaningful into the engine test.
- **`tests/test_map_e2e.py`** (extend): open the HLS Azure URL against the real
  `:1777`; assert tiles are fetched from `/api/map/tiles/...` (not an ephemeral
  port), that actual pixels paint, and that after a forced child restart the map
  still paints with no page error. This is the exact scenario that fails today.

---

## Rejected alternatives

- **Client-side COG for Azure** — can't: the blob endpoint has CORS off, and the
  band is `int16` (the browser reader is 8-bit). Server path is mandatory here.
- **Keep the ephemeral daemon, just auto-restart it client-side** — that is
  today's `recoverTiles`, and it can't fix a URL already bound to a dead port
  without a re-`describe` round trip per breakage; it also does nothing for the
  spawn storm or the app-exit orphaning.
- **Bake the daemon port into a longer-lived state and reuse it** — the port is
  still ephemeral and the process still dies with the app's Job Object; no state
  file makes an ephemeral URL durable.
- **Migrate all five tile daemons in this change** — considered and declined by
  the requester: prove the pattern on map first, then generalize.

---

## Open decisions — resolved as built

1. **Describe transport.** `/describe` goes through `:1777`
   (`POST /api/map/describe`); `map_render.py` first POSTs
   `/api/map/ensure` with its interpreter/daemon/version/cache, then the
   describe request, both with the `X-Fused: 1` guard. `runPython` remains only
   the thin bridge that carries the page's arguments into `map_render.py`.
2. **Warm at startup vs. lazy.** Lazy: `ensure()` on the first
   describe (the template's existing warmup ping triggers it early anyway).
3. **Generalization shape.** `map_engine.py` is map-specific, with the
   supervisor half (spawn/status-poll/reap/kill/restart/replay) written as the
   liftable seam for geotiff/zarr_aoi/netcdf later.

---

## Vector tile performance

A 3.58 GB / 13.8M-hexagon GeoPackage rendered z6 tiles in 16s, z8 in 5.6s,
z10-z12 in 1.1-2.2s. Two root causes, measured phase-by-phase:

- **The dense overview re-scanned the RTree per tile.** `_overview_frame`'s
  GROUP BY walks every rtree row in the tile's bbox (~1.3µs/row): a z6 tile
  over 12.5M features = 16.7s, every time, for every low-zoom tile.
- **GDAL's MVT driver costs ~0.9s per tile flat.** It builds a whole tileset
  (temp dataset, scratch sqlite, per-call directory) to emit one tile.

What shipped (`vector_engine.py`, `mvt_encode.py`):

- **RTree node summary** (`_node_summary`): the rtree's internal nodes are
  read once per source straight from the `<rtree>_node` shadow table (blob
  format: 2-byte depth on the root, 2-byte cell count, `{i64 id, 4×f32}`
  cells). Walking the internal levels yields every leaf-node bbox
  (~24 features each) in ~0.4s where the virtual-table scan took 16s. Dense
  tiles whose bbox holds more than `OVERVIEW_EXACT_MAX` (400k) features bin
  those node bboxes into the occupancy grid with a difference-array + 2D
  prefix sum (no holes, slight bbox overfill); smaller dense bboxes keep the
  exact SQL GROUP BY (sub-second at that size, exact per-cell counts). Any
  surprise in the undocumented blob format falls back to the exact SQL path.
- **Direct MVT encoding** (`mvt_encode.py`): the protobuf is written by hand
  for both the overview quads and the detail path (shapely → tile units →
  `clip_by_rect` → `simplify` → quantize → winding-corrected rings). GDAL's
  MVT writer, its tempdirs, and the encode semaphore are gone.
- One **persistent read-only sqlite connection per source** replaces
  per-query `sqlite3.connect`, and doubles as the cancellation hook.

Measured on the same file, same tiles (engine-direct / through `/api/map`):
z6 16.97s → 0.67s first tile (0.83s HTTP, includes the one-time summary walk),
z8 5.02s → 0.28s (0.41s HTTP), z10 1.32s → 0.72s, z11 1.15s → 0.66s,
z12 1.10s → 0.32s, z13 (detail path) → 0.45s, z14 → 0.17s.

Deferred alternatives: a one-time static PMTiles/mbtiles pyramid (best
steady-state, but minutes of build before first paint and a stale-cache
lifecycle); a precomputed generalized low-zoom table inside a sidecar
(same trade); parallel vtile workers (measured slower — file contention).

## Tile cancellation

Nothing cancelled abandoned work: pan/zoom away and the daemon kept
computing the old viewport's tiles; the browser's six connections per origin
stayed parked on them (which also blocked a second tab entirely), and
VTILE_POOL=1 head-of-line-blocked every other vector tile behind dead work.

- **Server proxy** (`server/routers/map_tiles.py`): the tile/jobs/optimize
  routes are async. The child fetch runs in a thread; a second task awaits
  `request.receive()` until `http.disconnect`. (A polled
  `request.is_disconnected()` does NOT work here: uvicorn's h11 protocol
  pauses reading once the request is complete, so the FIN is never seen;
  a persistently-awaited receive resumes reading.) On hangup the child
  socket is `shutdown()` — not just `close()`, which is deferred by the
  response's makefile io-ref and never actually reaches the wire.
- **Daemon** (`daemon.py`): tile handlers poll the future and watch their
  own socket (`select` + `MSG_PEEK`); a hung-up client sets a cancel event,
  `future.cancel()`s queued work, and closes. The vector engine checks the
  event between phases and aborts in-flight sqlite queries with a progress
  handler (`TileCancelled`); results of cancelled tiles are never cached.

Verified end-to-end with the exact path forced slow: an abandoned z6
(~17s of work) is cancelled and a fresh z8 answers in 2.2s (was 21.8-28.9s
before the fix); the server and the daemon's handler threads answer other
requests while a tile renders.

## Antimeridian rasters

A MODIS sinusoidal granule near 180° describes with west > east
([172.62, -20, -168.45, -10]). Three client-side breaks in `template.html`:

- **The raster never painted at all**: `bounds: safeBounds(...)` evaluated to
  an explicit `bounds: undefined`, which fails MapLibre's style validation —
  the source is silently never added, so not one tile was requested.
  `safeBounds` now returns a spread fragment (`{bounds}` or `{}`).
- **No auto-zoom**: `fitTo` now carries east past 180 (`e += 360`) for a
  crossing extent; MapLibre's `fitBounds` handles that natively (verified:
  camera lands on lat -15, lng 182 from a South America start).
- **World-spanning dashed outline**: `boundsFeature` shifts east by +360 for
  a crossing extent, so the outline frames the real ~19° footprint.

Verified in a real browser (Playwright + screenshots): the layer auto-zooms
onto the data, imagery paints west of 180 (the eastern tiles are genuinely
empty — sinusoidal earth-edge fill), the outline is correct, and
`test_an_antimeridian_raster_fits_to_its_data_and_paints` keeps it that way.
