# RCA: ~2-minute first launch after install (Windows)

## Symptom

The **first** launch of the desktop app after a fresh install takes ~2 minutes
before the UI is usable — whether started by the installer's "launch on finish"
or by the user opening it manually. Every launch after that is fast. Not
observed on macOS/Linux.

## Root cause

The first `GET /api/config` — which the shell fires on first paint — resolves
the execution engine, and that resolution **imported the whole `fused` compute
backend synchronously on the request thread**:

```
GET /api/config
  → prefs.effective_engine()
  → prefs.fused_engine_available()
  → engine.available()
  → from fused.agent_core.backends.local import python_compute   # the heavy import
     (fused 2.9.3 → numpy, pandas, pyarrow, duckdb, geopandas, rasterio, aiohttp, …)
```

That import is cheap once warm (~3s) but on a **fresh install** it pays two
one-time costs, both cached after the first run:

1. **Bytecode compilation** of the ~1500-module dependency tree. The installer
   shipped **source-only** — `build_windows_installer.ps1` deleted every
   `__pycache__` after installing packages — so the first import compiled the
   entire tree to `.pyc`.
2. **Windows Defender on-access scanning** of the hundreds of never-before-seen
   native `.pyd`/`.dll` files (numpy/pandas/pyarrow/duckdb/rasterio/GDAL …) as
   they load for the first time. Real-time + on-access protection is on;
   Defender caches file reputation after the first scan.

Because the import holds the GIL and ran on the request thread, it froze the
shell (`/api/config` blocked for the whole duration) instead of the cost being
paid in the background. macOS/Linux have no equivalent on-access AV scan, so the
effect is far milder there.

## Evidence

First-launch server log (`~/.fused-render/logs/fused-render-<pid>.log`), 0.4.3:

```
11:05:34 boot: fused-render=0.4.3 …
11:05:35 serving at http://127.0.0.1:1777/          # server up in <1s
11:05:39 GET /api/desktop/ready -> 200 (62 ms)      # readiness fine (PR #431)
11:06:01 numexpr defaulting to 8 threads            # heavy import chain grinding
   …71-second gap with no log lines (GIL held)…
11:07:17 GET /api/config -> 200 (90938 ms)          # one request stalled 91s
11:07:2x  … everything instant thereafter …
```

Controlled measurements (installed 0.4.3 interpreter):

| Probe | Result | Meaning |
|---|---|---|
| bundled `.pyc` timestamps | all 1456 stamped in the first-launch window | installer ships no precompiled bytecode |
| `import fused` warm | 3.1s | steady-state (2nd launch) cost |
| `import fused` cold (forced recompile, Defender warm) | 15.8s | bytecode-compile slice of the import |
| `compileall -f` over bundled site-packages | 92s | full first-import compile magnitude |
| Defender `RealTimeProtection` / `OnAccessProtection` | True / True | native modules scanned on first load |

The later `POST /api/run -> 200 (~5–6s)` entries are a **secondary** first-run
cost (showcase UDF venv bootstrap via `uv`), ~10–20s total — real but minor next
to the 91s import.

## Fix (Windows)

1. **Precompile bytecode into the payload** — `build_windows_installer.ps1` now
   runs `compileall … --invalidation-mode unchecked-hash` over `Lib` instead of
   deleting `__pycache__`. `unchecked-hash` pyc are trusted without re-stat, so
   the installer's file copy never invalidates them and the runtime never
   recompiles. Removes the compile slice from every first launch. *(Mac/Linux
   builders left untouched by request — they don't exhibit the problem.)*

2. **Warm the engine off the request path** — `engine.warm_in_background()` runs
   the import once in a startup daemon thread (`app.py` startup hook, next to the
   AI prewarm). `engine.available_nonblocking()` answers the request path from a
   cached result or a cheap `find_spec` check, so `/api/config` never triggers
   the cold import. The shell is responsive immediately; the engine warms in the
   background.

### Not changed, by decision

Windows Defender's on-access scan of the native modules — the larger slice — is
left in place (adding an install-dir AV exclusion was declined as a security
tradeoff). Precompile + warm-up remove the compile cost and, crucially, move the
remaining native-load cost **off the first request** so the UI no longer freezes;
the engine simply finishes warming a bit later in the background.

## Instrumentation

`engine.warm()` logs its duration on every start — the clearest per-user signal
of this cost:

```
INFO fused_render.engine: engine warm-up: fused backend ready (90.1s)   # cold first launch
INFO fused_render.engine: engine warm-up: fused backend ready (3.0s)    # every launch after
```

## Tests

- `tests/test_engine.py`: `available_nonblocking()` never calls the cold
  `available()`; reads `find_spec`/cache; `warm()` caches a positive, leaves a
  negative uncached (mid-session install still seen live), and logs its duration.
- `tests/test_server_engine.py`: `/api/config` returns without importing the
  backend on the request thread (the cold import is monkeypatched to fail; the
  request still succeeds).

## Verification (measured)

Ran the freshly-staged payload (my code, bytecode-cold) as a real server and hit
`/api/config` repeatedly while the engine warmed in the background:

| Metric | Before (real first-launch log) | After (staged server) |
|---|---|---|
| first `/api/config` | 90,938 ms (blocked) | 0.57 s worst, ~0.02 s typical |
| UI usable | ~2 min frozen | immediately (server ready 6.4 s) |
| engine warm-up | (implicit, on request thread) | logged: `engine warm-up: fused backend ready (3.8s)`, in the background |
| engine field | fused | fused (resolved via the cheap check) |

`3.8s` is this machine's Defender-warm cost; a truly Defender-cold fresh install
pays more, but now in the background — the UI no longer waits on it.

### Real fresh-install measurement (rebuilt installer)

Uninstalled + silently installed the rebuilt installer (10,428 pyc baked in),
then timed a genuine first launch and a second launch:

| Metric | First launch (Defender-cold) | Second launch (warm) |
|---|---|---|
| launcher → server ready | ~64 s | **9.6 s** |
| first `/api/config` | **157 ms** (was 90,938 ms) | 172 ms |
| engine warm-up (background) | 37.2 s (logged) | ~3 s |

The `/api/config` freeze is gone and the engine warms in the background — the
primary fix is confirmed end-to-end on a real install.

### Residual first-launch cost (not this fix)

The real install exposed a **second, independent** first-launch cost: the *base*
server startup (Python + `fused_render` + FastAPI/uvicorn/pydantic native
extensions) is Defender-scanned on first load, before the HTTP server is up. On
a fresh install that exceeds the supervisor's 20 s readiness budget, so the
supervisor kills and retries the child ~2–3× (`supervisor.log`: repeated
`Python server did not become ready`) until Defender warms enough for a child to
serve within the budget — ~64 s total. Second launch: 9.6 s (all cached).

This is the same on-access AV scan, in the base startup rather than the engine
import; precompiled pyc and the engine warm-up don't touch it.

**Fix: warm-import at install time.** `installer.iss` now runs
`python -I -m fused_render._warm` as a `[Run]` step (after the payload is
activated, before the first launch): it imports `fused_render.server.app` and
`fused`, so the OS pays that one-time on-access scan of the native extensions
**during the installer's progress bar** rather than on the first launch. No AV
exclusion (rejected — a silent, per-user weakening of real-time protection) and
no elevation. The install runs longer by roughly the scan cost; the first launch
is correspondingly warm.

## Every launch (warm): where the seconds go

Every launch — not just the first — spends several seconds before the shell is
usable, because a fresh Python process must rebuild the whole server before it
can serve. Measured against the installed 0.4.3 interpreter (Defender-warm):

| Phase | Warm cost | Notes |
|---|---|---|
| interpreter start + `import fused_render.cli` | ~0.5 s | before the `boot:` log line |
| `import fused_render.server` (the app graph) | **~2.0 s** | 634 module imports; **no single culprit** — top self-times are `fused_render.executor` 127 ms, `server.templates` 125 ms, `fastapi.openapi.models` 90 ms, then a long tail of pydantic/fastapi/router modules |
| `create_app()` body + `sync_user_skills()` | ~0.05 s | cheap; not a factor |
| lifespan startup → uvicorn accepts | ~1–3 s | background threads (rcd spawn + mount attach, engine warm, AI prewarm, index scan) all fire at once and contend while uvicorn finishes startup |
| supervisor readiness poll (100 ms interval) | ≤0.1 s | not a factor |

So a warm launch is ~5–7 s, and the ~2 s app-graph import is the floor — it is
inherent to a FastAPI app of this size and can't be removed without lazily
loading routers (out of scope; high risk for route registration). The
backgrounded `fused` import (2.8 s) does **not** block readiness and, measured,
does **not** slow the main-thread import via GIL contention. Notably **no
pandas / numpy / duckdb / pyarrow / rasterio is in the server import graph** —
the cost is the fastapi/pydantic/starlette framework floor (~0.6 s) plus
`fused_render`'s own route modules, not heavy data libraries.

One deferrable slice was found but **left for a focused follow-up** (it touches
the content-gate in `core_templates.py`): `ensure_core_templates()` sha256-hashes
the whole 15.6 MB packaged templates tree, and it is called at module import from
**both** `server/templates.py` and `executor.py` — two full-tree hashes per
launch (~165–200 ms each), the second pure waste. It can't be memoized naively
(the gate's tests mutate the tree and re-hash within one process, and executor's
result feeds the module-level in-process allowlist), so it needs its own change.

### The occasional *much* longer launch (the "sometimes ~2 min")

`_start_ready_server` gives the child a `_READY_TIMEOUT_S = 20 s` budget and
retries up to **3×**. When base startup exceeds 20 s — a Defender-cold first
launch, or heavy startup contention — the supervisor kills the child and
respawns it (`supervisor.log`: repeated `Python server did not become ready`),
turning one ~7 s launch into 20–60 s. The warm-import-at-install step above is
what keeps the cold first launch under the budget so this retry loop is not
entered.

**Instrumentation.** `_start_ready_server` now logs the real launcher→ready time
and, crucially, the attempt number: `server ready in 6.4s (attempt 1)` on a
clean launch, or `start attempt 1 failed after 20.0s: …` + a higher final
attempt number whenever the kill-retry loop was entered — the single clearest
signal in `supervisor.log` of *which* launches paid the retry penalty.
