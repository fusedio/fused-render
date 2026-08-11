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
