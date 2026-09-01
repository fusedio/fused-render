# Engines: server-managed child processes, served through :1777

**Status: implemented** (`server/engine_host.py`, `server/engine_forward.py`,
`server/routers/engines.py`).
A folder or template that needs a long-lived worker hands its ownership to the
fused-render server and serves its bytes through the stable `:1777` origin, so
a daemon death or restart never invalidates a URL the page holds. Two kinds of
child share this one supervisor:

- **`kind="template"`** — a built-in template's own tile daemon (the map
  viewer's is the first). Resident for the life of the server; never
  idle-reaped.
- **`kind="background"`** — a folder's declared daemon
  (`[tool.fused-render.app]` in its `pyproject.toml`, SPEC.md §46). A folder
  declares its protocol with exactly one of two keys:
  - `daemon = "server.py"` — the author's own HTTP surface, reached through
    the proxy at whatever paths the author's script defines. Resident by
    default (`idle_timeout_s=0`, never reaped) since the author's own process
    owns its lifetime.
  - `main = "compute.py"` — the shipped `fused_render/engine_worker.py`
    (`DEFAULT_DAEMON`) imports it once and answers `POST /call` by splatting
    the request body into `compute.py`'s `main(**params)`, keeping module-level
    imports and globals warm between calls. Reaped after `idle_timeout_s`
    (default 900s) of no calls; the next call re-warms it.

  `idle_timeout_s` is a plain float on `Child`, not derived from protocol or
  kind — a `daemon =` author may set it to opt into idle retirement, and a
  `main =` author may set it to `0` to stay resident. `0` means "never
  reaped."

## Current shape (separation of concerns)

The server owns a reusable subsystem keyed by an opaque `engine_id`:

- `server/engine_host.py` — spawn / status-poll / reap / kill / restart of one
  child per engine id; validates the interpreter (home venv store) and daemon
  path (`<templates-root>/<engine_id>/daemon.py` for a template child, the
  folder's own declared path — or `DEFAULT_DAEMON` for a `main =` folder — for
  a background child); replays opaque *reinit* requests on restart.
  `reap_idle_children` (woken by a sweeper thread, started the first time any
  child is brought up with `idle_timeout_s > 0`) retires any child whose
  `idle_timeout_s` has elapsed since its last call and which isn't mid-call
  (`mark_busy`/`mark_idle` bookkeeping, keyed by engine_id, protects an
  in-flight call from being reaped out from under it). Knows nothing about
  tiles, descriptors, or what a folder's daemon does.
- `server/engine_forward.py` — forwards one request to a child: the per-child
  keep-alive pool, heal-on-failure retry, cancel-on-disconnect, and the per-call
  budget (`engine_host.CALL_TIMEOUT_S`, 60s, applied only to a bounded child —
  `idle_timeout_s > 0` — never to a resident one). Shared by every proxied
  call, template or background, so nothing reaches into anything else.
- `server/routers/engines.py` — `POST /api/engines/{id}/ensure`, `/reinit`,
  `/forget`, and an opaque `ANY /api/engines/{id}/proxy/{path...}` passthrough
  (forwarded via `engine_forward`; proxied POST needs `X-Fused`, GET is an open
  read; `..`/backslash path segments are rejected). A proxied POST to a
  non-template child is sent `at_most_once`, since a heal-restart must not
  silently re-send a call that may already have run side-effecting code.
- `server/routers/background_apps.py` — the folder-facing surface:
  start/stop/restart/autostart/status for a folder's own declared daemon, on
  top of `ensure_background`/`background_apps.bring_up_args` for the actual
  bring-up. `fused_render/background_apps.py` owns the manifest (`Manifest`,
  `load_manifest`, `interpreter_for`, `engine_id_for`) and the persisted
  autostart store.

A `main =` folder's interpreter falls back to `sys.executable` when its
declared project venv isn't built yet, rather than blocking — the same
`/api/run` fallback the builtin engine takes.

### The `fused.daemon` bridge (`static/runtime.js`)

`fused.daemon` is the browser control surface for a folder's own declared
daemon — `status`, `start`, `stop`, `restart`, `setAutostart`, `call(path,
body)`, `run(params)`, `watch(callback)`. `call` proxies to the daemon's own
routes through `/api/engines/<id>/proxy/<path>` for a `daemon =` author;
`run` is the `main =` convenience over it — `call("/call", params)` with the
`{ok, result, error, stdout, resolved_py}` envelope unwrapped the way
`runPython` unwraps `/api/run`'s. Run state and autostart are independent
(D511): `start`/`stop`/`restart` never touch the persisted autostart flag, and
`setAutostart` never starts or stops anything. A page rendering as a card
preview thumbnail is refused on every method but `status` (D507/D508) — a
sandboxed preview iframe must not be able to start, stop, or resurrect a
real user's daemon just by being hovered or scrolled past.

All map-specific knowledge lives in the template: `templates/map/map_render.py`
posts to `/api/engines/map/…`, rewrites its own descriptor URLs to
`/api/engines/map/proxy/…` paths, and registers each describe as a reinit for
replay. The only place `"map"` appears on the server is as an id the template
passes in. The next tile daemons (geotiff, zarr_aoi, netcdf, pyramid) adopt the
host by picking an engine id — no server change.

## No public tile face — templates own their own vocabulary

`/api/engines/*` is a generic primitive: child supervision, health-check,
reinit replay, proxy, disconnect-cancel, with nothing here that knows what a
tile is. There is deliberately **no** `fused.tiles` bridge method or
`/api/tiles/*` router: a geospatial face on the core server would put tile-URL
shape, descriptor kinds and styling knobs (`colormap`, `rescale`, `stretch`,
`renderMode`) into shared code, duplicating knowledge that already lives in
`map_render.py` and `template.html`. It also buys nothing — the descriptor
already hands the page stable, self-healing `/api/engines/map/proxy/…` URLs to
point MapLibre at. Nothing geospatial belongs in `fused_render/server/`.

On generalizing further: once a second template wants supervision, have the
template *declare* its engine (entry point, health path) as data the host reads,
so a new engine is a manifest rather than a new router. Not built yet — the
caller-supplied `ensure` payload is simpler and correct until there's a second
caller.

## Windows thread-exit deadlock (why the pools are persistent)

On Windows, a thread that has done GDAL `/vsicurl/` work deadlocks the whole
process the moment it exits: its DLL thread-detach holds the loader lock against
a native thread holding the GIL, so the next `Thread.start()` blocks forever. So
all describe/tile work runs on persistent pools (`daemon.RENDER_POOL`,
`VTILE_POOL`, and `RasterEngine.prepare_pool`) whose threads never exit per-call;
socketserver's per-connection handler threads never touch the geo stack.

## Vector tile performance

A 3.58 GB / 13.8M-hexagon GeoPackage rendered z6 tiles in ~17s. Two causes: the
dense overview re-scanned the RTree per tile, and GDAL's MVT driver costs ~0.9s
per tile flat (it builds a whole tileset to emit one tile). What shipped
(`vector_engine.py`, `mvt_encode.py`):

- **RTree node summary** (`_node_summary`): the rtree's internal nodes are read
  once per source straight from the `<rtree>_node` shadow table (blob: 2-byte
  depth, 2-byte cell count, `{i64 id, 4×f32}` cells). Dense bboxes bin those
  node bboxes into an occupancy grid; smaller dense bboxes keep the exact SQL
  GROUP BY. Any surprise in the undocumented blob format falls back to SQL.
- **Direct MVT encoding** (`mvt_encode.py`): the protobuf is written by hand for
  both the overview quads and the detail path — no GDAL MVT driver, tempdirs, or
  encode semaphore.
- One **persistent read-only sqlite connection per source**, which doubles as
  the cancellation hook.

Result on the same file: z6 16.97s → 0.67s, z8 5.02s → 0.28s, z10-z12 ~1.1-2.2s
→ ~0.3-0.7s (engine-direct, one-time summary walk included).

## Tile cancellation

Abandoned work used to keep computing: pan/zoom away and the daemon finished the
old viewport, parking the browser's six connections and head-of-line-blocking
`VTILE_POOL`.

- **Server proxy** (`server/engine_forward.py`): the proxy is async; the child
  fetch runs in a thread while a second task awaits `request.receive()` until
  `http.disconnect`. (A polled `request.is_disconnected()` never fires — uvicorn's
  h11 pauses reading once the request is complete; an awaited receive resumes it.)
  On hangup the child socket is `shutdown()`, not just `close()`.
- **Daemon** (`daemon.py`): tile handlers watch their own socket, set a cancel
  event on hangup, `future.cancel()` queued work, and the vector engine aborts
  in-flight sqlite with a progress handler (`TileCancelled`); cancelled tiles are
  never cached.

## Antimeridian rasters

A MODIS sinusoidal granule near 180° describes with west > east
(e.g. [172.62, -20, -168.45, -10]). Handled on both sides:

- **Server** (`raster_engine.py`): `crosses_antimeridian` sources read each tile
  through `_crossing_tile` — the tile's Web Mercator bounds are shifted by whole
  worlds (PROJ `+over`, `+nadgrids=@null` stripped) into the dataset's unwrapped
  domain, so the hemisphere east of 180 paints. maxzoom is estimated off the
  unwrapped extent (`_crossing_maxzoom`) because rio-tiler/GDAL reproject the
  crossing grid to a world-spanning box and cap it far short of native.
- **Client** (`template.html`): `fitTo` and `boundsFeature` carry east past 180;
  `safeBounds` drops a west > east `bounds` (MapLibre renders nothing for one,
  and can't express a crossing bounds), letting both hemispheres fetch.
