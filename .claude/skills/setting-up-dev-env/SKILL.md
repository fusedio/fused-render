---
name: setting-up-dev-env
description: Use when setting up a fused-render checkout or git worktree for the first time — before running pytest, the dev server, or any daemon — so tests and the server actually work instead of silently failing on missing deps or an unbuilt React shell.
---

# Setting Up the Dev Env

## Overview

Every fresh checkout/worktree needs a **3.12 venv** and a **built React shell** — both are gitignored, so they don't carry into a worktree. Without them, `pytest`/`python` silently run against the wrong interpreter or fail on missing deps, and every server test dies with `RuntimeError: React shell not built`.

## Running the Dev Server

Use `scripts/dev.sh` — never launch `python -m fused_render.cli` (or uvicorn) directly. It does almost all the setup for you: bootstraps a repo-local `.venv` (with `[fused,bundled]`) if missing, `npm install`s the frontend, builds `shell-dist/`, then runs `vite build --watch` + the server with Python auto-reload and per-worktree port/state isolation.

```bash
scripts/dev.sh                 # defaults
scripts/dev.sh --port 9000     # extra args pass through to the server
```

## Setup for Tests

`dev.sh` covers the server, but its auto-venv has `[fused,bundled]` only — **not the `dev` extra** (pytest/xdist). And tests need a built `shell-dist/` or `create_app()` refuses to start. So the venv install is the one manual step:

```bash
uv venv --python 3.12 .venv                                         # 3.14 lacks duckdb/rasterio wheels
uv pip install --python .venv/bin/python -e ".[dev,bundled,fused]"  # dev extra now includes pytest-xdist
```

For `shell-dist/`: if you've run `scripts/dev.sh`, it's already built. If you'll *only* run tests and never start the server, build it once directly (repo uses npm, per `package-lock.json`):

```bash
cd frontend && npm install && npm run build && cd ..
```

Verify: `ls fused_render/static/shell-dist/index.html` and `.venv/bin/python -m pytest -q` (~1170 pass).

## Reference

| Item | Why |
|------|-----|
| Python 3.12 | duckdb/rasterio have no 3.14 wheels; fused wheel needs ≥3.11 |
| `dev` extra | pytest + httpx (backs TestClient) |
| `bundled` extra | duckdb, rasterio, zarr, pandas, geopandas… (templates + daemons) |
| `fused` extra | compute-engine wheel for `/api/run` |
| frontend build | `create_app()` refuses to start without `shell-dist/` |
| `ensurepip` | only for the Deploy one-click path, not the suite |

Parallel tests: the suite is xdist-safe (process isolation), so `pytest -n auto` works (~4.5x faster). Process-based only — `os.chdir` + `importlib.reload` would race under a threaded runner.
